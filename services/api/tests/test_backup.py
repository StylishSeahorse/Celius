"""Backup manifest and restore verification (app.ops.backup). The shell scripts around
them are exercised end to end by the CI clean-deployment job."""

import hashlib
import json

from sqlalchemy import delete

from app.models import Conversation, FileKind, FileStatus, StoredFile
from app.ops.backup import census, check_files, verify_restore, write_manifest
from app.services.files.storage import Storage


async def _stored_file(sessionmaker, settings, user, content=b"hello backup") -> StoredFile:
    sha = hashlib.sha256(content).hexdigest()
    blob = Storage(settings.storage_root).blob(sha)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(content)
    async with sessionmaker() as db:
        row = StoredFile(
            owner_id=user.id,
            filename="notes.txt",
            media_type="text/plain",
            kind=FileKind.text,
            size_bytes=len(content),
            sha256=sha,
            status=FileStatus.ready,
        )
        db.add_all([row, Conversation(owner_id=user.id, title="Kept")])
        await db.commit()
        return row


async def test_manifest_checksums_every_file_and_counts_rows(tmp_path, sessionmaker, user):
    staging = tmp_path / "staging"
    (staging / "config").mkdir(parents=True)
    (staging / "db.dump").write_bytes(b"dump")
    (staging / "config" / ".env").write_text("A=1\n")
    manifest = await write_manifest(staging, "abc123", factory=sessionmaker)

    assert set(manifest["files"]) == {"db.dump", "config/.env"}
    assert manifest["files"]["db.dump"]["sha256"] == hashlib.sha256(b"dump").hexdigest()
    assert manifest["database"]["tables"]["users"] == 1
    assert manifest["git_revision"] == "abc123"
    assert json.loads((staging / "manifest.json").read_text()) == manifest
    assert check_files(staging) == []

    (staging / "db.dump").write_bytes(b"tampered")
    (staging / "config" / ".env").unlink()
    assert sorted(check_files(staging)) == ["config/.env: missing", "db.dump: checksum mismatch"]


async def test_verify_matches_a_faithful_restore(tmp_path, sessionmaker, settings, user):
    await _stored_file(sessionmaker, settings, user)
    manifest = {"database": await census(sessionmaker)}
    assert manifest["database"]["tables"]["conversations"] == 1
    assert (
        await verify_restore(manifest, settings.storage_root, deep=True, factory=sessionmaker) == []
    )


async def test_verify_reports_lost_rows_and_missing_content(tmp_path, sessionmaker, settings, user):
    stored = await _stored_file(sessionmaker, settings, user)
    manifest = {"database": await census(sessionmaker)}
    async with sessionmaker() as db:
        await db.execute(delete(Conversation))
        await db.commit()
    blob = Storage(settings.storage_root).blob(stored.sha256)
    blob.write_bytes(b"other bytes!")  # same length, different content

    problems = await verify_restore(manifest, settings.storage_root, factory=sessionmaker)
    assert problems == ["table conversations: 0 rows, backup had 1"]
    deep = await verify_restore(manifest, settings.storage_root, deep=True, factory=sessionmaker)
    assert f"file {stored.id}: content changed" in deep

    blob.unlink()
    problems = await verify_restore(manifest, settings.storage_root, factory=sessionmaker)
    assert f"file {stored.id}: content missing" in problems


async def test_verify_reports_a_missing_table(sessionmaker, settings, user):
    manifest = {"database": {"tables": {"users": 1, "gone_table": 3}}}
    problems = await verify_restore(manifest, settings.storage_root, factory=sessionmaker)
    assert problems == ["table gone_table: missing"]
