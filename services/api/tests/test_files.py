import asyncio
import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.auth.passwords import hash_password
from app.db.base import utcnow
from app.models import UploadSession, User
from app.services.files.paths import UnsafePath, resolve_within, safe_filename, safe_relative_path
from app.services.files.service import cleanup_expired_uploads
from tests import files_fixtures as make
from tests.fakes import drain_jobs


async def start(client, headers, name, size, **extra):
    return await client.post(
        "/api/v1/files/uploads", json={"filename": name, "size": size, **extra}, headers=headers
    )


async def upload(client, headers, name, data: bytes, **extra):
    """Full upload; returns the /complete response (or the failing step's response)."""
    created = await start(client, headers, name, len(data), **extra)
    if created.status_code != 201:
        return created
    sent = await client.put(created.json()["upload_url"], content=data, headers=headers)
    if sent.status_code != 200:
        return sent
    return await client.post(
        f"/api/v1/files/uploads/{created.json()['id']}/complete", headers=headers
    )


async def usage(client, headers) -> int:
    return (await client.get("/api/v1/files/usage", headers=headers)).json()["used_bytes"]


async def uploaded(client, headers, sessionmaker, settings, name, data, **extra) -> dict:
    response = await upload(client, headers, name, data, **extra)
    assert response.status_code == 201, response.text
    await drain_jobs(sessionmaker, settings)
    return (await client.get(f"/api/v1/files/{response.json()['id']}", headers=headers)).json()


# ---------------------------------------------------------------------- integrity


async def test_upload_download_round_trip_is_byte_identical(
    client, auth_headers, sessionmaker, settings
):
    data = "# Notes\n\nCafé ☕ — ünïcode survives.\n".encode() * 50
    response = await upload(client, auth_headers, "notes.md", data)
    assert response.status_code == 201
    file = response.json()
    assert file["status"] == "processing"
    assert file["sha256"] == hashlib.sha256(data).hexdigest()
    assert file["size_bytes"] == len(data)
    assert file["media_type"] == "text/plain"  # decided from the bytes, not the client

    downloaded = await client.get(f"/api/v1/files/{file['id']}/download", headers=auth_headers)
    assert downloaded.content == data
    assert downloaded.headers["content-disposition"].startswith("attachment")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in downloaded.headers["content-security-policy"]
    assert await usage(client, auth_headers) == len(data)

    await drain_jobs(sessionmaker, settings)
    ready = (await client.get(f"/api/v1/files/{file['id']}", headers=auth_headers)).json()
    assert ready["status"] == "ready" and ready["text_chars"] > 0
    text = (await client.get(f"/api/v1/files/{file['id']}/text", headers=auth_headers)).json()
    assert text["text"] == data.decode()


async def test_binary_round_trip_and_inline_images(client, auth_headers, sessionmaker, settings):
    data = make.png(640, 480)
    file = await uploaded(client, auth_headers, sessionmaker, settings, "photo.png", data)
    assert file["kind"] == "image" and file["status"] == "ready"
    assert file["meta"]["width"] == 640 and file["has_thumbnail"] is True

    inline = await client.get(f"/api/v1/files/{file['id']}/download?inline=1", headers=auth_headers)
    assert inline.content == data
    assert inline.headers["content-type"] == "image/png"
    assert inline.headers["content-disposition"].startswith("inline")
    thumb = await client.get(f"/api/v1/files/{file['id']}/thumbnail", headers=auth_headers)
    assert thumb.headers["content-type"] == "image/webp" and thumb.content[:4] == b"RIFF"


async def test_text_is_never_served_inline(client, auth_headers):
    html = b"<html><script>alert(1)</script></html>"
    file = (await upload(client, auth_headers, "page.html", html)).json()
    response = await client.get(
        f"/api/v1/files/{file['id']}/download?inline=1", headers=auth_headers
    )
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize(
    ("name", "builder", "expected_kind", "expected_text"),
    [
        ("report.pdf", make.pdf, "pdf", "Hello PDF world"),
        ("letter.docx", make.docx, "document", "Hello from Word"),
        ("budget.xlsx", make.xlsx, "spreadsheet", "seeds,12"),
        ("deck.pptx", make.pptx, "presentation", "Hello from slides"),
    ],
)
async def test_documents_are_converted_to_text(
    client, auth_headers, sessionmaker, settings, name, builder, expected_kind, expected_text
):
    file = await uploaded(client, auth_headers, sessionmaker, settings, name, builder())
    assert file["kind"] == expected_kind and file["status"] == "ready", file["error"]
    text = (await client.get(f"/api/v1/files/{file['id']}/text", headers=auth_headers)).json()
    assert expected_text in text["text"]


async def test_complete_is_idempotent(client, auth_headers):
    created = (await start(client, auth_headers, "a.txt", 3)).json()
    await client.put(created["upload_url"], content=b"abc", headers=auth_headers)
    url = f"/api/v1/files/uploads/{created['id']}/complete"
    first = await client.post(url, headers=auth_headers)
    second = await client.post(url, headers=auth_headers)
    assert first.json()["id"] == second.json()["id"]
    assert await usage(client, auth_headers) == 3


# ---------------------------------------------------------------------- type checks


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("holiday.png", b"PK\x03\x04 this is really a zip", "file_type_mismatch"),
        ("notes.txt", make.png(), "file_type_mismatch"),
        ("report.pdf", b"just text pretending", "file_type_mismatch"),
        ("letter.docx", make.zip_of({"a.txt": b"x"}), "file_type_mismatch"),
        ("README", b"\x7fELF\x02\x01\x01\x00binary", "file_type_mismatch"),
    ],
)
async def test_contents_must_match_the_name(client, auth_headers, name, data, code):
    response = await upload(client, auth_headers, name, data)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert await usage(client, auth_headers) == 0  # the reservation was released


async def test_unsupported_extensions_are_refused_up_front(client, auth_headers):
    response = await start(client, auth_headers, "setup.exe", 100)
    assert response.status_code == 415
    assert await usage(client, auth_headers) == 0


# ---------------------------------------------------------------------- path security


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.txt",
        "a/../../b.txt",
        "/etc/passwd",
        "C:\\Windows\\x.txt",
        "a\\..\\..\\b",
        "dir/con.txt",
        "x\x00y.txt",
    ],
)
async def test_unsafe_relative_paths_are_rejected(client, auth_headers, bad):
    response = await start(client, auth_headers, "file.txt", 1, relative_path=bad)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsafe_path"


async def test_folder_paths_are_normalised(client, auth_headers):
    response = await upload(
        client, auth_headers, "main.py", b"print(1)\n", relative_path="./src//app/./main.py"
    )
    assert response.json()["relative_path"] == "src/app/main.py"


def test_filename_and_path_helpers(tmp_path):
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\Users\\me\\report.pdf") == "report.pdf"
    assert safe_filename("a" * 300 + ".txt").endswith(".txt")
    assert len(safe_filename("a" * 300 + ".txt").encode()) <= 255
    with pytest.raises(UnsafePath):
        safe_filename("...")
    assert safe_relative_path(None) is None

    root = tmp_path / "root"
    (root / "inside").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (root / "link").symlink_to(tmp_path / "outside")
    assert resolve_within(root, "inside") == (root / "inside").resolve()
    with pytest.raises(UnsafePath):
        resolve_within(root, "link/secret.txt")  # a symlink may not lead out
    with pytest.raises(UnsafePath):
        resolve_within(root, "../outside")


# ---------------------------------------------------------------------- archives


async def test_safe_archive_is_listed_and_its_text_read(
    client, auth_headers, sessionmaker, settings
):
    archive = make.zip_of(
        {"src/app.py": b"print('hi')\n", "README.md": b"# Read me\n", "logo.png": make.png()}
    )
    file = await uploaded(client, auth_headers, sessionmaker, settings, "project.zip", archive)
    assert file["status"] == "ready"
    assert {e["path"] for e in file["meta"]["entries"]} == {"src/app.py", "README.md", "logo.png"}
    text = (await client.get(f"/api/v1/files/{file['id']}/text", headers=auth_headers)).json()[
        "text"
    ]
    assert "=== src/app.py ===" in text and "print('hi')" in text and "logo.png" not in text


@pytest.mark.parametrize(
    "archive",
    [
        make.zip_of({"../../evil.sh": b"rm -rf /"}),
        make.zip_of({"/etc/cron.d/evil": b"x"}),
        make.zip_of({"ok.txt": b"x"}, symlinks={"link": "/etc/passwd"}),
        make.zip_of({"bomb.txt": b"\0" * (4 * 1024 * 1024)}),  # ratio ≈ 1000:1
    ],
    ids=["traversal", "absolute", "symlink", "bomb"],
)
async def test_hostile_archives_are_rejected(client, auth_headers, archive):
    response = await upload(client, auth_headers, "upload.zip", archive)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "archive_rejected"
    assert await usage(client, auth_headers) == 0


async def test_archive_limits(client, auth_headers, settings):
    settings.archive_max_entries = 3
    many = make.zip_of({f"f{i}.txt": b"x" for i in range(5)})
    assert (await upload(client, auth_headers, "many.zip", many)).json()["error"][
        "code"
    ] == "archive_rejected"
    settings.archive_max_entries = 100
    settings.archive_max_total_bytes = 1000
    big = make.zip_of({"a.txt": b"y" * 2000})
    assert (await upload(client, auth_headers, "big.zip", big)).json()["error"][
        "code"
    ] == "archive_rejected"


async def test_office_files_get_the_same_archive_checks(client, auth_headers):
    hostile = make.zip_of(
        {"[Content_Types].xml": b"<x/>", "word/document.xml": b"<x/>", "../../escape": b"x"}
    )
    response = await upload(client, auth_headers, "letter.docx", hostile)
    assert response.json()["error"]["code"] == "archive_rejected"


# ---------------------------------------------------------------------- quotas and sizes


async def test_quota_is_enforced_and_released(client, auth_headers, settings):
    settings.max_user_storage_bytes = 1000
    first = await upload(client, auth_headers, "a.txt", b"a" * 600)
    assert first.status_code == 201
    second = await start(client, auth_headers, "b.txt", 600)
    assert second.status_code == 413
    error = second.json()["error"]
    assert error["code"] == "quota_exceeded" and error["used_bytes"] == 600

    await client.delete(f"/api/v1/files/{first.json()['id']}", headers=auth_headers)
    assert await usage(client, auth_headers) == 0
    assert (await upload(client, auth_headers, "b.txt", b"b" * 600)).status_code == 201


async def test_concurrent_uploads_cannot_overshoot_the_quota(client, auth_headers, settings):
    settings.max_user_storage_bytes = 1000
    responses = await asyncio.gather(
        *(start(client, auth_headers, f"{i}.txt", 300) for i in range(8))
    )
    accepted = [r for r in responses if r.status_code == 201]
    assert len(accepted) == 3
    assert await usage(client, auth_headers) == 900


async def test_size_limits(client, auth_headers, settings):
    settings.max_upload_bytes = 100
    assert (await start(client, auth_headers, "big.txt", 101)).json()["error"][
        "code"
    ] == "file_too_large"

    created = (await start(client, auth_headers, "x.txt", 10)).json()
    too_much = await client.put(created["upload_url"], content=b"z" * 11, headers=auth_headers)
    assert too_much.json()["error"]["code"] == "upload_too_large"
    await client.put(created["upload_url"], content=b"z" * 5, headers=auth_headers)
    short = await client.post(
        f"/api/v1/files/uploads/{created['id']}/complete", headers=auth_headers
    )
    assert short.json()["error"]["code"] == "size_mismatch"
    # Sending again replaces the earlier attempt.
    await client.put(created["upload_url"], content=b"z" * 10, headers=auth_headers)
    done = await client.post(
        f"/api/v1/files/uploads/{created['id']}/complete", headers=auth_headers
    )
    assert done.status_code == 201


async def test_abandoned_uploads_expire_and_free_their_quota(
    client, auth_headers, sessionmaker, settings
):
    created = (await start(client, auth_headers, "x.txt", 500)).json()
    await client.put(created["upload_url"], content=b"q" * 200, headers=auth_headers)
    assert await usage(client, auth_headers) == 500
    async with sessionmaker() as db:
        await db.execute(update(UploadSession).values(expires_at=utcnow() - timedelta(minutes=1)))
        await db.commit()
    assert await cleanup_expired_uploads(sessionmaker, settings) == 1
    assert await usage(client, auth_headers) == 0
    assert not list(
        (settings_path := __import__("pathlib").Path(settings.storage_root) / "quarantine").glob(
            "*"
        )
    ), settings_path
    late = await client.put(created["upload_url"], content=b"q", headers=auth_headers)
    assert late.status_code == 410


# ---------------------------------------------------------------------- storage


async def test_identical_content_is_stored_once(client, auth_headers, settings):
    data = b"same bytes\n"
    a = (await upload(client, auth_headers, "a.txt", data)).json()
    b = (await upload(client, auth_headers, "b.txt", data)).json()
    blobs = [p for p in (Path(settings.storage_root) / "blobs").rglob("*") if p.is_file()]
    assert len(blobs) == 1 and a["sha256"] == b["sha256"]

    await client.delete(f"/api/v1/files/{a['id']}", headers=auth_headers)
    assert blobs[0].exists()  # still used by b
    gone = await client.get(f"/api/v1/files/{a['id']}/download", headers=auth_headers)
    assert gone.status_code == 404
    await client.delete(f"/api/v1/files/{b['id']}", headers=auth_headers)
    assert not blobs[0].exists()


async def test_files_are_private(client, auth_headers, sessionmaker):
    mine = (await upload(client, auth_headers, "secret.txt", b"secret")).json()
    pending = (await start(client, auth_headers, "later.txt", 5)).json()

    async with sessionmaker() as db:
        db.add(User(username="eve", password_hash=hash_password("eve-password-123")))
        await db.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "eve", "password": "eve-password-123"}
    )
    client.cookies.clear()
    eve = {"Authorization": f"Bearer {login.json()['access_token']}"}

    for path in ("", "/download", "/text", "/thumbnail"):
        assert (
            await client.get(f"/api/v1/files/{mine['id']}{path}", headers=eve)
        ).status_code == 404
    assert (await client.delete(f"/api/v1/files/{mine['id']}", headers=eve)).status_code == 404
    assert (
        await client.put(pending["upload_url"], content=b"12345", headers=eve)
    ).status_code == 404
    assert (await client.get("/api/v1/files", headers=eve)).json() == []
    assert (
        await client.get(f"/api/v1/files/{uuid.uuid4()}", headers=auth_headers)
    ).status_code == 404


async def test_download_requires_authentication(client, auth_headers):
    file = (await upload(client, auth_headers, "a.txt", b"x")).json()
    assert (await client.get(f"/api/v1/files/{file['id']}/download")).status_code == 401


# ---------------------------------------------------------------------- malware scanning


@pytest.fixture
async def clamd():
    """A tiny clamd stand-in speaking INSTREAM."""
    state = {"reply": b"stream: OK\0", "received": 0}

    async def handle(reader, writer):
        assert await reader.readuntil(b"\0") == b"zINSTREAM\0"
        while True:
            size = int.from_bytes(await reader.readexactly(4), "big")
            if size == 0:
                break
            state["received"] += len(await reader.readexactly(size))
        writer.write(state["reply"])
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state["port"] = server.sockets[0].getsockname()[1]
    yield state
    server.close()


async def test_clamav_scanning(client, auth_headers, settings, clamd):
    settings.clamav_host, settings.clamav_port = "127.0.0.1", clamd["port"]
    clean = await upload(client, auth_headers, "clean.txt", b"hello")
    assert clean.status_code == 201 and clamd["received"] == 5

    clamd["reply"] = b"stream: Eicar-Test-Signature FOUND\0"
    infected = await upload(client, auth_headers, "eicar.txt", b"X5O!P%@AP")
    assert infected.status_code == 422
    assert infected.json()["error"]["code"] == "malware_detected"
    assert "Eicar" in infected.json()["error"]["message"]

    settings.clamav_port = 1  # nothing listens there
    unavailable = await upload(client, auth_headers, "later.txt", b"hi")
    assert unavailable.status_code == 503


async def test_jobs_from_a_dead_worker_are_retried_then_failed(
    client, auth_headers, sessionmaker, settings
):
    from app.models import Job
    from app.services.jobs import claim_next_job, recover_stale_jobs

    await upload(client, auth_headers, "a.txt", b"hello")
    for attempt in range(settings.job_max_attempts):
        assert await claim_next_job(sessionmaker, f"dead-{attempt}", settings) is not None
        async with sessionmaker() as db:
            await db.execute(update(Job).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
            await db.commit()
        assert await recover_stale_jobs(sessionmaker, settings) == 1
    async with sessionmaker() as db:
        job = (await db.scalars(select(Job))).one()
    assert job.status == "failed" and "worker stopped" in job.error
