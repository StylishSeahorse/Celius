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
    assert sorted(on_sandbox_network) == ["sandbox", "worker"]
    assert compose["services"]["worker"]["environment"]["SANDBOX_URL"] == "http://sandbox:8100"


def test_database_and_cache_have_no_route_out(compose):
    assert compose["networks"]["backend"]["internal"] is True
    for name in ("postgres", "redis"):
        assert compose["services"][name]["networks"] == ["backend"]
