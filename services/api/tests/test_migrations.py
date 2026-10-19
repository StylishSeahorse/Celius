"""Round-trips the Alembic migrations against a real PostgreSQL database.

Skipped unless TEST_POSTGRES_URL is set (CI and `make test-api-pg` set it), since
the unit tests run on SQLite and cannot catch PostgreSQL-specific DDL problems.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PG_URL = os.environ.get("TEST_POSTGRES_URL")
API_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_POSTGRES_URL not set")


def alembic(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        env={**os.environ, "DATABASE_URL": PG_URL or ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


def test_upgrade_check_downgrade_upgrade():
    alembic("downgrade", "base")
    alembic("upgrade", "head")
    # Models and migrations must agree; drift means someone forgot a migration.
    assert "No new upgrade operations detected" in alembic("check")
    alembic("downgrade", "base")
    alembic("upgrade", "head")
