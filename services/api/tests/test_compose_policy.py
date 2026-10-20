"""Static checks on infra/compose.yaml: the sandbox's isolation must not erode silently."""

from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[3] / "infra" / "compose.yaml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_no_service_can_reach_the_docker_socket(compose):
    for name, service in compose["services"].items():
        for volume in service.get("volumes", []):
            source = volume if isinstance(volume, str) else volume.get("source", "")
            assert "docker.sock" not in str(source), name
        assert not service.get("privileged"), name


def test_sandbox_is_locked_down(compose):
    sandbox = compose["services"]["sandbox"]
    assert "env_file" not in sandbox  # no application secrets
    assert set(sandbox["environment"]) <= {
        "SANDBOX_TOKEN",
        "SANDBOX_MODE",
        "SANDBOX_MAX_CONCURRENT",
        "SANDBOX_MAX_MEMORY_MB",
        "SANDBOX_EGRESS_PROXY",
        "SANDBOX_EGRESS_TOKEN",
        "SANDBOX_NETWORK",
    }
    assert sandbox["networks"] == ["sandbox"]
    assert compose["networks"]["sandbox"]["internal"] is True
    assert "ports" not in sandbox
    assert sandbox["cap_drop"] == ["ALL"]
    assert sandbox["read_only"] is True
    assert "no-new-privileges:true" in sandbox["security_opt"]
    assert sandbox["user"] != "0" and not str(sandbox["user"]).startswith("0:")
    assert sandbox["pids_limit"] and sandbox["mem_limit"]
    assert [v.split(":")[1] for v in sandbox["volumes"]] == ["/workspaces"]


def test_only_the_worker_talks_to_the_sandbox(compose):
    on_sandbox_network = [
        name for name, s in compose["services"].items() if "sandbox" in s.get("networks", [])
    ]
    assert sorted(on_sandbox_network) == ["egress", "sandbox", "worker"]
    assert compose["services"]["worker"]["environment"]["SANDBOX_URL"] == "http://sandbox:8100"


def test_database_and_cache_have_no_route_out(compose):
    assert compose["networks"]["backend"]["internal"] is True
    for name in ("postgres", "redis"):
        assert compose["services"][name]["networks"] == ["backend"]


def test_the_egress_proxy_is_the_only_way_out_and_holds_nothing_else(compose):
    egress = compose["services"]["egress"]
    assert "env_file" not in egress and "volumes" not in egress and "ports" not in egress
    assert set(egress["environment"]) <= {"SANDBOX_EGRESS_TOKEN", "EGRESS_PORTS"}
    assert sorted(egress["networks"]) == ["egress", "sandbox"]
    assert egress["cap_drop"] == ["ALL"] and egress["read_only"] is True
    assert egress["command"][-1] == "sandbox.egress"
    # The sandbox itself still has no route out.
    assert compose["services"]["sandbox"]["networks"] == ["sandbox"]
    assert compose["services"]["sandbox"]["environment"]["SANDBOX_EGRESS_PROXY"] == (
        "http://egress:3128"
    )


def test_backups_are_read_only_inside_the_app(compose):
    """Only backup.sh on the host writes archives; the app just reads the status file."""
    for name in ("api", "worker"):
        mounts = [
            v for v in compose["services"][name]["volumes"] if v.endswith(":/data/backups:ro")
        ]
        assert len(mounts) == 1, name
    assert not any("backups" in str(v) for v in compose["services"]["sandbox"]["volumes"])
