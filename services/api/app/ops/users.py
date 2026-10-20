"""Account maintenance from the command line (inside the api container):

    docker compose -f infra/compose.yaml --env-file .env exec api python -m app.ops.users list
    ... exec api python -m app.ops.users reset-password admin      # prompts for the password
    echo 'new password' | ... exec -T api python -m app.ops.users reset-password admin --stdin

Resetting a password signs the account out everywhere and clears login throttling.
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import delete, select

from app.auth.passwords import hash_password
from app.auth.sessions import revoke_all
from app.config import get_settings
from app.db import get_sessionmaker
from app.db.base import utcnow
from app.models import AuditEvent, User

MIN_LENGTH = 12


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Again: ") != password:
            raise SystemExit("The passwords don't match")
    if len(password) < MIN_LENGTH:
        raise SystemExit(f"The password must be at least {MIN_LENGTH} characters")
    return password


async def reset_password(username: str, password: str, factory=None) -> int:
    factory = factory or get_sessionmaker()
    async with factory() as db:
        user = await db.scalar(select(User).where(User.username == username))
        if user is None:
            raise SystemExit(f"No account named {username!r}")
        user.password_hash = hash_password(password)
        user.is_active = True
        revoked = await revoke_all(db, user.id)
        # A forgotten password usually means failed attempts: let the owner straight in.
        await db.execute(
            delete(AuditEvent).where(
                AuditEvent.action == "auth.login_failed",
                AuditEvent.detail["username"].as_string() == username,
            )
        )
        db.add(
            AuditEvent(
                user_id=user.id,
                action="auth.password_reset_cli",
                detail={"sessions_revoked": revoked},
                created_at=utcnow(),
            )
        )
        await db.commit()
        return revoked


async def list_users(factory=None) -> list[tuple[str, bool, bool]]:
    factory = factory or get_sessionmaker()
    async with factory() as db:
        rows = (await db.scalars(select(User).order_by(User.username))).all()
        return [(u.username, u.is_admin, u.is_active) for u in rows]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.ops.users")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List accounts")
    reset = commands.add_parser("reset-password", help="Set a new password")
    reset.add_argument("username")
    reset.add_argument("--stdin", action="store_true", help="Read the password from stdin")
    args = parser.parse_args(argv)
    get_settings()  # fail early on bad configuration
    if args.command == "list":
        for username, admin, active in asyncio.run(list_users()):
            print(
                f"{username}\t{'admin' if admin else 'user'}\t{'active' if active else 'disabled'}"
            )
    else:
        password = _read_password(args.stdin)
        revoked = asyncio.run(reset_password(args.username, password))
        print(f"Password changed for {args.username}; {revoked} session(s) signed out.")


if __name__ == "__main__":
    main()
