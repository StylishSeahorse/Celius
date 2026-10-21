"""audio and video files

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21 09:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = ("image", "pdf", "document", "spreadsheet", "presentation", "text", "archive", "export")


def upgrade() -> None:
    # SQLite stores the kind as plain text; only PostgreSQL has a type to extend.
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE file_kind ADD VALUE IF NOT EXISTS 'audio'")
        op.execute("ALTER TYPE file_kind ADD VALUE IF NOT EXISTS 'video'")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # PostgreSQL can't drop an enum value: rebuild the type. Audio and video files have no
    # kind to go back to, so the downgrade refuses while any exist.
    count = op.get_bind().exec_driver_sql(
        "SELECT count(*) FROM files WHERE kind IN ('audio', 'video')"
    )
    if count.scalar():
        raise RuntimeError("Delete the audio and video files before downgrading past 0008")
    kinds = ", ".join(f"'{kind}'" for kind in OLD_KINDS)
    op.execute("ALTER TYPE file_kind RENAME TO file_kind_old")
    op.execute(f"CREATE TYPE file_kind AS ENUM ({kinds})")
    op.execute("ALTER TABLE files ALTER COLUMN kind TYPE file_kind USING kind::text::file_kind")
    op.execute("DROP TYPE file_kind_old")
