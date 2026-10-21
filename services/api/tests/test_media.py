"""Audio and video files: accepted when they really are media, played in place, described
to the model, and given to tools."""

import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.services.files import media
from app.services.files.sniff import FileRejected, detect
from tests import files_fixtures as make
from tests.fakes import FakeLlama, drain_jobs, drain_runs
from tests.test_attachments import user_message
from tests.test_chat import new_conversation, send
from tests.test_files import upload, uploaded

needs_ffmpeg = pytest.mark.skipif(not media.available(), reason="ffmpeg is not installed")


def _encode(tmp: Path, name: str, *args: str) -> bytes:
    out = tmp / name
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args, str(out)], check=True, capture_output=True
    )
    return out.read_bytes()


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, bytes]:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    tmp = tmp_path_factory.mktemp("media")
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    picture = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2"]
    return {
        "wav": _encode(tmp, "a.wav", *tone),
        "flac": _encode(tmp, "a.flac", *tone),
        "ogg": _encode(tmp, "a.ogg", *tone, "-c:a", "libopus"),
        "m4a": _encode(tmp, "a.m4a", *tone, "-c:a", "aac"),
        "mp3": _encode(tmp, "a.mp3", *tone, "-c:a", "libmp3lame"),
        "mp4": _encode(tmp, "v.mp4", *picture, *tone, "-c:v", "mpeg4", "-c:a", "aac"),
        "webm": _encode(tmp, "v.webm", *picture, "-c:v", "libvpx", "-b:v", "200k")
        if "libvpx"
        in subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
        ).stdout
        else b"",
        "mkv": _encode(tmp, "v.mkv", *picture, "-c:v", "mpeg4"),
        "avi": _encode(tmp, "v.avi", *picture, "-c:v", "mpeg4"),
        "mov": _encode(tmp, "v.mov", *picture, "-c:v", "mpeg4"),
    }


# ---------------------------------------------------------------------- what is accepted


@pytest.mark.parametrize(
    ("ext", "kind", "media_type"),
    [
        ("wav", "audio", "audio/wav"),
        ("flac", "audio", "audio/flac"),
        ("ogg", "audio", "audio/ogg"),
        ("m4a", "audio", "audio/mp4"),
        ("mp3", "audio", "audio/mpeg"),
        ("mp4", "video", "video/mp4"),
        ("webm", "video", "video/webm"),
        ("mkv", "video", "video/x-matroska"),
        ("avi", "video", "video/x-msvideo"),
        ("mov", "video", "video/quicktime"),
    ],
)
def test_media_is_recognised_from_its_bytes(clips, tmp_path, ext, kind, media_type):
    if not clips[ext]:
        pytest.skip(f"this ffmpeg can't write .{ext}")
    path = tmp_path / f"clip.{ext}"
    path.write_bytes(clips[ext])
    detected = detect(path, f"clip.{ext}")
    assert (detected.kind.value, detected.media_type) == (kind, media_type)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("song.mp3", b"just some text pretending to be music\n" * 20),
        ("film.mp4", make.png(10, 10)),
        ("voice.wav", b"RIFF\x00\x00\x00\x00AVI LIST"),  # an AVI isn't a WAV
        ("clip.webm", b"%PDF-1.7\n"),
    ],
)
def test_anything_else_with_a_media_name_is_refused(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    with pytest.raises(FileRejected) as caught:
        detect(path, name)
    assert caught.value.code == "file_type_mismatch"


async def test_media_has_its_own_size_limit(client, auth_headers, settings):
    settings.max_upload_bytes = 1000
    settings.max_media_upload_bytes = 1_000_000
    usage = (await client.get("/api/v1/files/usage", headers=auth_headers)).json()
    assert usage["max_media_upload_bytes"] == 1_000_000

    def create(name: str, size: int):
        return client.post(
            "/api/v1/files/uploads", json={"filename": name, "size": size}, headers=auth_headers
        )

    assert (await create("talk.mp3", 500_000)).status_code == 201
    assert (await create("notes.txt", 500_000)).status_code == 413
    too_big = await create("film.mp4", 2_000_000)
    assert too_big.status_code == 413
    assert too_big.json()["error"]["max_upload_bytes"] == 1_000_000


async def test_a_text_file_renamed_to_mp4_is_refused_at_the_end(client, auth_headers, settings):
    settings.max_upload_bytes = 100
    response = await upload(client, auth_headers, "huge.mp4", b"a" * 1000)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "file_type_mismatch"


# ---------------------------------------------------------------------- processing


@needs_ffmpeg
async def test_audio_gets_its_duration_and_codec(
    client, auth_headers, sessionmaker, settings, clips
):
    file = await uploaded(client, auth_headers, sessionmaker, settings, "talk.mp3", clips["mp3"])
    assert file["status"] == "ready" and file["kind"] == "audio"
    assert file["media_type"] == "audio/mpeg"
    assert file["meta"]["audio_codec"] == "mp3"
    assert 1.9 <= file["meta"]["duration_seconds"] <= 2.2
    assert "video_codec" not in file["meta"] and file["has_thumbnail"] is False


@needs_ffmpeg
async def test_video_gets_its_size_and_a_still(client, auth_headers, sessionmaker, settings, clips):
    file = await uploaded(client, auth_headers, sessionmaker, settings, "clip.mp4", clips["mp4"])
    assert file["status"] == "ready" and file["kind"] == "video"
    meta = file["meta"]
    assert (meta["width"], meta["height"], meta["video_codec"]) == (320, 240, "mpeg4")
    assert meta["audio_codec"] == "aac"
    assert file["has_thumbnail"] is True
    still = await client.get(f"/api/v1/files/{file['id']}/thumbnail", headers=auth_headers)
    image = Image.open(io.BytesIO(still.content))
    assert image.format == "WEBP" and image.size == (320, 240)
    low, high = image.convert("L").getextrema()
    assert low < high  # the test pattern, not a blank frame


@needs_ffmpeg
async def test_media_plays_in_place_and_can_seek(
    client, auth_headers, sessionmaker, settings, clips
):
    file = await uploaded(client, auth_headers, sessionmaker, settings, "clip.mp4", clips["mp4"])
    url = f"/api/v1/files/{file['id']}/download?inline=1"
    whole = await client.get(url, headers=auth_headers)
    assert whole.status_code == 200 and whole.content == clips["mp4"]
    assert whole.headers["content-type"] == "video/mp4"
    assert whole.headers["content-disposition"].startswith("inline")
    assert whole.headers["accept-ranges"] == "bytes"
    part = await client.get(url, headers={**auth_headers, "Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.content == clips["mp4"][100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(clips['mp4'])}"


async def test_unreadable_media_still_uploads(client, auth_headers, sessionmaker, settings):
    """Right container signature, nonsense inside: no details, but the upload works."""
    fake = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 200
    file = await uploaded(client, auth_headers, sessionmaker, settings, "odd.wav", fake)
    assert file["status"] == "ready" and file["kind"] == "audio"
    assert "duration_seconds" not in (file["meta"] or {})


async def test_probing_can_be_switched_off(client, auth_headers, sessionmaker, settings, clips):
    settings.media_probe = False
    file = await uploaded(client, auth_headers, sessionmaker, settings, "clip.mp4", clips["mp4"])
    assert file["status"] == "ready" and file["has_thumbnail"] is False
    assert not file["meta"]


def test_the_probe_is_limited(tmp_path, monkeypatch, clips):
    """ffprobe that blows its limits (or hangs) gives no details, not a crash."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(clips["mp4"])
    monkeypatch.setattr(media, "MEMORY_BYTES", 16 * 1024 * 1024)
    assert media.probe(path) == {}
    assert media.still(path, at_seconds=None) is None


def test_media_is_described_briefly():
    assert media.describe({"duration_seconds": 204.4, "audio_codec": "mp3"}) == "3:24, mp3"
    assert (
        media.describe(
            {
                "duration_seconds": 3730,
                "video_codec": "h264",
                "width": 1920,
                "height": 1080,
                "audio_codec": "aac",
            }
        )
        == "1:02:10, 1920×1080 h264 + aac"
    )
    assert media.describe({}) == "" and media.describe(None) == ""


# ---------------------------------------------------------------------- in a chat


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


@needs_ffmpeg
async def test_the_model_is_told_what_the_media_is(
    client, auth_headers, sessionmaker, settings, gateway, fake, clips
):
    file = await uploaded(client, auth_headers, sessionmaker, settings, "talk.mp3", clips["mp3"])
    conv = await new_conversation(client, auth_headers)
    sent = await send(
        client, auth_headers, conv["id"], content="What's in this?", attachment_ids=[file["id"]]
    )
    assert sent.status_code == 201
    assert sent.json()["message"]["parts"][1]["data"]["kind"] == "audio"
    await drain_jobs(sessionmaker, settings)
    await drain_runs(sessionmaker, gateway, settings)
    content = user_message(fake.requests[0])["content"]
    assert content.startswith('<attachment name="talk.mp3" type="audio">')
    assert "an audio recording (0:02, mp3)" in content
    assert "can't listen to or watch it" in content
