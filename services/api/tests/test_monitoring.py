"""Phase 7 monitoring: health checks, metrics and alerts."""

import json
import time
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import update

from app.db.base import utcnow
from app.models import Run, User
from app.services import monitoring


def write_backup_status(settings, hours_ago: float, **extra) -> None:
    root = Path(settings.backup_root)
    root.mkdir(parents=True, exist_ok=True)
    status = {
        "finished_at": time.time() - hours_ago * 3600,
        "archive": "celius-x.tar.enc",
        "offsite_target": True,
        "copied_off_host": True,
    }
    (root / "last-success.json").write_text(json.dumps(status | extra))


async def checks_by_name(sessionmaker, settings, gateway=None) -> dict:
    async with sessionmaker() as db:
        return {c.name: c for c in await monitoring.collect(db, settings, gateway)}


async def test_status_lists_every_check_for_admins(
    client, auth_headers, settings, tmp_path, sessionmaker, user
):
    settings.backup_root = str(tmp_path / "backups")
    response = await client.get("/api/v1/status", headers=auth_headers)
    assert response.status_code == 200
    names = {c["name"] for c in response.json()["checks"]}
    assert {
        "database",
        "model_server",
        "worker",
        "run_queue",
        "job_queue",
        "backup",
        "approvals",
    } <= names
    assert any(n.startswith("disk_") for n in names)
    async with sessionmaker() as db:
        await db.execute(update(User).values(is_admin=False))
        await db.commit()
    assert (await client.get("/api/v1/status", headers=auth_headers)).status_code == 403


async def test_worker_heartbeat_and_backup_age(sessionmaker, settings, tmp_path):
    settings.backup_root = str(tmp_path / "backups")
    checks = await checks_by_name(sessionmaker, settings)
    assert checks["worker"].level == "critical"
    assert checks["backup"].level == "warning"  # never backed up

    monitoring.record_worker_heartbeat(settings, "w1")
    write_backup_status(settings, hours_ago=2)
    checks = await checks_by_name(sessionmaker, settings)
    assert checks["worker"].level == "ok"
    assert checks["backup"].level == "ok"

    write_backup_status(settings, hours_ago=30)
    assert (await checks_by_name(sessionmaker, settings))["backup"].level == "warning"
    write_backup_status(settings, hours_ago=100)
    assert (await checks_by_name(sessionmaker, settings))["backup"].level == "critical"

    # Recent, but never left the machine: losing the disk would lose it too.
    write_backup_status(settings, hours_ago=1, offsite_target=False, copied_off_host=False)
    backup = (await checks_by_name(sessionmaker, settings))["backup"]
    assert backup.level == "warning" and "only on this machine" in backup.summary
    write_backup_status(settings, hours_ago=1, copied_off_host=False)
    backup = (await checks_by_name(sessionmaker, settings))["backup"]
    assert backup.level == "warning" and "off-host copy failed" in backup.summary


async def test_a_stuck_queue_is_reported(client, auth_headers, sessionmaker, settings):
    conv = (await client.post("/api/v1/conversations", json={}, headers=auth_headers)).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": "q", "content": "hi"},
        headers=auth_headers,
    )
    checks = await checks_by_name(sessionmaker, settings)
    assert checks["run_queue"].level == "ok" and checks["run_queue"].value == 1
    async with sessionmaker() as db:
        await db.execute(update(Run).values(created_at=utcnow() - timedelta(minutes=90)))
        await db.commit()
    assert (await checks_by_name(sessionmaker, settings))["run_queue"].level == "critical"


async def test_disk_thresholds(settings):
    settings.alert_disk_warning_percent = 101  # anything is "low"
    assert {c.level for c in monitoring.disk_checks(settings)} == {"warning"}
    settings.alert_disk_critical_percent = 101
    assert {c.level for c in monitoring.disk_checks(settings)} == {"critical"}


async def test_alerts_fire_on_changes_and_recoveries_only(sessionmaker, settings, tmp_path):
    settings.backup_root = str(tmp_path / "backups")
    settings.alert_webhook_url = "http://homeassistant.local:8123/api/webhook/celius"
    settings.alert_disk_warning_percent = settings.alert_disk_critical_percent = 0
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    monitoring.record_worker_heartbeat(settings, "w1")
    write_backup_status(settings, hours_ago=1)
    async with sessionmaker() as db:
        assert await monitoring.alert_on_changes(db, settings, transport=transport) == []
        write_backup_status(settings, hours_ago=100)
        first = await monitoring.alert_on_changes(db, settings, transport=transport)
        again = await monitoring.alert_on_changes(db, settings, transport=transport)
        write_backup_status(settings, hours_ago=1)
        recovered = await monitoring.alert_on_changes(db, settings, transport=transport)
    assert len(first) == 1 and "backup" in first[0]
    assert again == []  # no repeat within ALERT_REPEAT_HOURS
    assert len(recovered) == 1 and "recovered" in recovered[0]
    assert sent[0]["level"] == "critical" and sent[0]["check"]["name"] == "backup"


async def test_ntfy_format(sessionmaker, settings, tmp_path):
    settings.backup_root = str(tmp_path / "backups")
    settings.alert_webhook_url = "https://ntfy.sh/my-celius"
    settings.alert_webhook_format = "ntfy"
    seen = []
    transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200))
    async with sessionmaker() as db:
        await monitoring.alert_on_changes(db, settings, transport=transport)
    worker_alert = next(r for r in seen if b"worker" in r.content)
    assert worker_alert.headers["priority"] == "urgent"
    assert worker_alert.headers["title"].startswith("Celius: worker")


async def test_metrics_need_their_token(client, settings, user):
    assert (await client.get("/api/v1/metrics")).status_code == 404
    settings.metrics_token = settings.app_secret_key.__class__("m" * 24)
    assert (
        await client.get("/api/v1/metrics", headers={"Authorization": "Bearer wrong"})
    ).status_code == 404
    response = await client.get("/api/v1/metrics", headers={"Authorization": "Bearer " + "m" * 24})
    assert response.status_code == 200
    assert 'celius_check_level{check="database"} 0' in response.text
