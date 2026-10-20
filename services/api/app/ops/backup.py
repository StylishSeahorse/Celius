"""The parts of backup and restore that need the application (infra/scripts/backup.sh and
restore.sh call these inside the api image):

    python -m app.ops.backup manifest /backup/staging --git-revision abc123
    python -m app.ops.backup check /restore/staging          # checksums only
    python -m app.ops.backup verify /restore/staging/manifest.json [--deep]

The manifest records a checksum for every file in the backup and a census of the
database (rows per table, alembic revision). After a restore, `verify` compares the
restored database with that census and checks that every stored file's content is on
disk (and with --deep, that its hash still matches).
"""

import argparse
import asyncio
import hashlib
import json
import re
import socket
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.db import get_sessionmaker
from app.db.base import Base
from app.services.files.storage import Storage

FORMAT = 1
MANIFEST = "manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


async def census(factory=None) -> dict:
    """Row counts of every application table, and the schema revision."""
    import app.models  # noqa: F401  registers the tables

    factory = factory or get_sessionmaker()
    counts: dict[str, int] = {}
    async with factory() as db:
        for table in Base.metadata.sorted_tables:
            counts[table.name] = await db.scalar(text(f'SELECT count(*) FROM "{table.name}"'))
        try:
            revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
        except Exception:  # noqa: BLE001  no alembic table (tests, create_all)
            await db.rollback()
            revision = None
    return {"tables": counts, "alembic_revision": revision}


def _checksums(directory: Path) -> dict:
    return {
        str(path.relative_to(directory)): {
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != MANIFEST
    }


async def write_manifest(directory: Path, git_revision: str | None = None, factory=None) -> dict:
    files = await asyncio.to_thread(_checksums, directory)
    manifest = {
        "format": FORMAT,
        "created_at": time.time(),
        "host": socket.gethostname(),
        "git_revision": git_revision,
        "database": await census(factory),
        "files": files,
    }
    await asyncio.to_thread((directory / MANIFEST).write_text, json.dumps(manifest, indent=2))
    return manifest


def check_files(directory: Path) -> list[str]:
    """Problems with the backup's own files (missing, changed, unexpected format)."""
    try:
        manifest = json.loads((directory / MANIFEST).read_text())
    except (OSError, ValueError) as exc:
        return [f"manifest unreadable: {exc}"]
    if manifest.get("format") != FORMAT:
        return [f"unsupported backup format {manifest.get('format')!r}"]
    problems = []
    for name, expected in manifest["files"].items():
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: missing")
        elif sha256_file(path) != expected["sha256"]:
            problems.append(f"{name}: checksum mismatch")
    return problems


def _check_blobs(rows, storage_root: str, deep: bool) -> list[str]:
    problems = []
    storage = Storage(storage_root)
    for raw_id, sha, size in rows:
        file_id = uuid.UUID(str(raw_id))  # SQLite returns the hex form
        try:
            blob = storage.blob(sha)
        except ValueError:
            problems.append(f"file {file_id}: invalid hash")
            continue
        if not blob.is_file():
            problems.append(f"file {file_id}: content missing")
        elif blob.stat().st_size != size:
            problems.append(f"file {file_id}: size {blob.stat().st_size}, expected {size}")
        elif deep and sha256_file(blob) != sha:
            problems.append(f"file {file_id}: content changed")
    return problems


async def verify_restore(manifest: dict, storage_root: str, deep=False, factory=None) -> list[str]:
    """Problems with a restored deployment compared with the backup's manifest."""
    factory = factory or get_sessionmaker()
    problems = []
    async with factory() as db:
        for table, expected in manifest["database"]["tables"].items():
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
                problems.append(f"table {table!r}: invalid name in manifest")
                continue
            try:
                actual = await db.scalar(text(f'SELECT count(*) FROM "{table}"'))
            except Exception:  # noqa: BLE001
                await db.rollback()
                problems.append(f"table {table}: missing")
                continue
            if actual != expected:
                problems.append(f"table {table}: {actual} rows, backup had {expected}")
        rows = (
            await db.execute(
                text("SELECT id, sha256, size_bytes FROM files WHERE deleted_at IS NULL")
            )
        ).all()
    problems += await asyncio.to_thread(_check_blobs, rows, storage_root, deep)
    return problems


def _report(problems: list[str], ok_message: str) -> None:
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        raise SystemExit(1)
    print(ok_message)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.ops.backup")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("manifest", help="Checksum a backup directory and census the DB")
    make.add_argument("directory", type=Path)
    make.add_argument("--git-revision")
    check = commands.add_parser("check", help="Verify a backup directory's checksums")
    check.add_argument("directory", type=Path)
    verify = commands.add_parser("verify", help="Compare the restored deployment with a manifest")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--deep", action="store_true", help="Re-hash every stored file")
    args = parser.parse_args(argv)

    if args.command == "manifest":
        manifest = asyncio.run(write_manifest(args.directory, args.git_revision))
        rows = sum(manifest["database"]["tables"].values())
        print(f"manifest: {len(manifest['files'])} files, {rows} database rows")
    elif args.command == "check":
        _report(check_files(args.directory), "checksums OK")
    else:
        settings = get_settings()
        manifest = json.loads(args.manifest.read_text())
        problems = asyncio.run(verify_restore(manifest, settings.storage_root, args.deep))
        _report(problems, "restore verified: row counts and stored files match the backup")


if __name__ == "__main__":
    main()
