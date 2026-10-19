"""Sandbox health as last seen by the worker.

Only the worker can reach the sandbox (compose.yaml), so it records what it sees on the
shared app-data volume, and the API shows that in settings.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

from app.config import Settings

STALE_SECONDS = 300


def _path(settings: Settings) -> Path:
    return Path(settings.storage_root) / "status" / "sandbox.json"


def write_status(settings: Settings, health: dict[str, Any] | None) -> None:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"health": health, "checked_at": time.time()}))
    os.replace(tmp, path)


def read_status(settings: Settings) -> dict[str, Any] | None:
    """The worker's last observation, or None if there is none recent."""
    try:
        data = json.loads(_path(settings).read_text())
    except (OSError, ValueError):
        return None
    if time.time() - data.get("checked_at", 0) > STALE_SECONDS:
        return None
    return data.get("health") or {"status": "unreachable"}
