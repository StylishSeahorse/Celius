"""Health checks for the whole deployment, and alerts when one changes state.

Checks: database, model server, sandbox, worker heartbeat, disk space, run and job
queues, failed jobs, backup age. The API shows them (/api/v1/status, admins) and as
Prometheus metrics (/api/v1/metrics, with METRICS_TOKEN). The worker evaluates them
every minute and posts to ALERT_WEBHOOK_URL when one becomes a warning or critical, when
it recovers, and again every ALERT_REPEAT_HOURS while it stays critical.
"""

import json
import logging
import os
import shutil
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.models import Job, JobStatus, Run, RunStatus, ToolCall, ToolCallStatus
from app.services.tools.sandbox_status import read_status as sandbox_status

log = logging.getLogger(__name__)

Level = Literal["ok", "warning", "critical"]
ORDER = {"ok": 0, "warning": 1, "critical": 2}
WORKER_STALE_SECONDS = 180


@dataclass
class Check:
    name: str
    level: Level
    summary: str
    value: float | None = None
    detail: dict = field(default_factory=dict)


def _status_dir(settings: Settings) -> Path:
    return Path(settings.storage_root) / "status"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def record_worker_heartbeat(settings: Settings, worker_id: str) -> None:
    _write_json(
        _status_dir(settings) / "worker.json",
        {"worker_id": worker_id, "host": socket.gethostname(), "at": time.time()},
    )


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def _aware(moment):
    return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=utcnow().tzinfo)


def disk_checks(settings: Settings) -> list[Check]:
    checks, seen = [], set()
    for label, path in (
        ("storage", settings.storage_root),
        ("workspaces", settings.workspace_root),
        ("backups", settings.backup_root),
    ):
        target = Path(path)
        while not target.exists() and target != target.parent:
            target = target.parent
        try:
            usage = shutil.disk_usage(target)
            device = os.stat(target).st_dev
        except OSError:
            continue
        if device in seen:
            continue  # same filesystem as one already reported
        seen.add(device)
        free = usage.free / usage.total * 100 if usage.total else 100
        level: Level = (
            "critical"
            if free < settings.alert_disk_critical_percent
            else "warning"
            if free < settings.alert_disk_warning_percent
            else "ok"
        )
        checks.append(
            Check(
                f"disk_{label}",
                level,
                f"{free:.0f}% free ({usage.free / 1024**3:.1f} of {usage.total / 1024**3:.1f} GB)",
                round(free, 1),
                {"path": str(target), "free_bytes": usage.free, "total_bytes": usage.total},
            )
        )
    return checks


async def collect(db: AsyncSession, settings: Settings, gateway=None) -> list[Check]:
    checks: list[Check] = []
    now = utcnow()

    try:
        await db.execute(text("SELECT 1"))
        checks.append(Check("database", "ok", "reachable"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("database", "critical", f"unreachable ({type(exc).__name__})"))
        return checks

    if gateway is not None:
        health = await gateway.health_check()
        checks.append(
            Check(
                "model_server",
                "ok" if health.ok else "warning",
                "online" if health.ok else f"offline ({health.detail or 'no answer'})",
                health.latency_ms,
            )
        )

    if settings.sandbox_url and settings.sandbox_token:
        status = sandbox_status(settings)
        ok = bool(status) and status.get("status") == "ok"
        checks.append(
            Check(
                "sandbox",
                "ok" if ok else "warning",
                f"running ({status.get('mode')})" if ok else "not reachable from the worker",
                detail=status or {},
            )
        )

    beat = _read_json(_status_dir(settings) / "worker.json")
    if beat is None:
        checks.append(Check("worker", "critical", "no heartbeat yet"))
    else:
        age = time.time() - beat.get("at", 0)
        checks.append(
            Check(
                "worker",
                "ok" if age < WORKER_STALE_SECONDS else "critical",
                f"alive ({_age(age)} ago)"
                if age < WORKER_STALE_SECONDS
                else f"silent for {_age(age)}",
                round(age),
                {"worker_id": beat.get("worker_id")},
            )
        )

    checks += disk_checks(settings)

    queued, oldest = (
        await db.execute(
            select(func.count(), func.min(Run.created_at)).where(Run.status == RunStatus.queued)
        )
    ).one()
    wait = (now - _aware(oldest)).total_seconds() if oldest else 0
    level: Level = (
        "critical"
        if wait > settings.alert_queue_critical_minutes * 60
        else "warning"
        if wait > settings.alert_queue_warning_minutes * 60
        else "ok"
    )
    checks.append(
        Check(
            "run_queue",
            level,
            f"{queued} waiting" + (f", oldest {_age(wait)}" if queued else ""),
            queued,
            {"oldest_wait_seconds": round(wait)},
        )
    )

    jobs_queued, jobs_oldest = (
        await db.execute(
            select(func.count(), func.min(Job.created_at)).where(Job.status == JobStatus.queued)
        )
    ).one()
    jobs_wait = (now - _aware(jobs_oldest)).total_seconds() if jobs_oldest else 0
    failed = await db.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.status == JobStatus.failed,
            Job.finished_at > now - timedelta(hours=24),
            Job.error != "Cancelled",
        )
    )
    level = (
        "warning"
        if jobs_wait > settings.alert_queue_warning_minutes * 60
        or failed >= settings.alert_failed_jobs_per_day
        else "ok"
    )
    checks.append(
        Check(
            "job_queue",
            level,
            f"{jobs_queued} waiting, {failed} failed in 24 h",
            jobs_queued,
            {"failed_24h": failed, "oldest_wait_seconds": round(jobs_wait)},
        )
    )

    pending = await db.scalar(
        select(func.count())
        .select_from(ToolCall)
        .where(ToolCall.status == ToolCallStatus.pending_approval)
    )
    checks.append(Check("approvals", "ok", f"{pending} waiting for you", pending))

    last = _read_json(Path(settings.backup_root) / "last-success.json")
    if last is None:
        checks.append(Check("backup", "warning", "no backup has completed yet"))
    else:
        age = time.time() - float(last.get("finished_at", 0))
        hours = age / 3600
        level = (
            "critical"
            if hours > settings.alert_backup_critical_hours
            else "warning"
            if hours > settings.alert_backup_warning_hours
            else "ok"
        )
        summary = f"last one {_age(age)} ago"
        if not last.get("copied_off_host"):
            # A backup on the same disk doesn't survive losing the machine.
            level = level if level == "critical" else "warning"
            summary += (
                ", but the off-host copy failed"
                if last.get("offsite_target")
                else ", only on this machine (set BACKUP_OFFSITE_TARGET)"
            )
        checks.append(
            Check(
                "backup",
                level,
                summary,
                round(hours, 1),
                {
                    "archive": last.get("archive"),
                    "copied_off_host": last.get("copied_off_host", False),
                },
            )
        )
    return checks


def overall(checks: list[Check]) -> Level:
    return max((c.level for c in checks), key=ORDER.__getitem__, default="ok")


def prometheus(checks: list[Check]) -> str:
    lines = [
        "# HELP celius_check_level 0 ok, 1 warning, 2 critical",
        "# TYPE celius_check_level gauge",
    ]
    lines += [f'celius_check_level{{check="{c.name}"}} {ORDER[c.level]}' for c in checks]
    lines += [
        "# HELP celius_check_value The check's measured value (see its summary)",
        "# TYPE celius_check_value gauge",
    ]
    lines += [
        f'celius_check_value{{check="{c.name}"}} {c.value}' for c in checks if c.value is not None
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- alerts


async def alert_on_changes(
    db: AsyncSession, settings: Settings, gateway=None, transport=None
) -> list[str]:
    """Compare with the last evaluation; notify on changes. Returns what was sent."""
    if not settings.alert_webhook_url:
        return []
    checks = await collect(db, settings, gateway)
    state_path = _status_dir(settings) / "alerts.json"
    previous = _read_json(state_path) or {}
    now = time.time()
    messages: list[tuple[Check, str]] = []
    state = {}
    for check in checks:
        before = previous.get(check.name, {"level": "ok", "sent_at": 0})
        sent_at = before.get("sent_at", 0)
        if check.level != before["level"]:
            if check.level == "ok":
                messages.append((check, f"✅ {check.name} recovered: {check.summary}"))
            else:
                messages.append((check, f"{_icon(check.level)} {check.name}: {check.summary}"))
            sent_at = now
        elif check.level == "critical" and now - sent_at > settings.alert_repeat_hours * 3600:
            messages.append((check, f"{_icon(check.level)} still {check.name}: {check.summary}"))
            sent_at = now
        state[check.name] = {"level": check.level, "sent_at": sent_at}
    sent = []
    for check, message in messages:
        if await _send(settings, check, message, transport):
            sent.append(message)
    _write_json(state_path, state)
    return sent


def _icon(level: Level) -> str:
    return {"warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")


async def _send(settings: Settings, check: Check, message: str, transport=None) -> bool:
    url = settings.alert_webhook_url
    assert url
    title = f"Celius: {check.name} {check.level}"
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            if settings.alert_webhook_format == "ntfy":
                priority = {"critical": "urgent", "warning": "high"}.get(check.level, "default")
                response = await client.post(
                    url, content=message.encode(), headers={"Title": title, "Priority": priority}
                )
            elif settings.alert_webhook_format == "text":
                response = await client.post(
                    url, content=message.encode(), headers={"Content-Type": "text/plain"}
                )
            else:
                response = await client.post(
                    url,
                    json={
                        "title": title,
                        "message": message,
                        "level": check.level,
                        "check": asdict(check),
                    },
                )
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("Alert webhook failed: %s", exc)
        return False
