import pytest

from sandbox.config import SandboxConfig
from sandbox.runner import become_subreaper, bwrap_available

become_subreaper()

TOKEN = "test-token-0123456789abcdef"

MODES = ["process"]
if bwrap_available(None):
    MODES.append("bwrap")


@pytest.fixture
def config(tmp_path, monkeypatch) -> SandboxConfig:
    monkeypatch.setenv("SANDBOX_TOKEN", TOKEN)  # must not reach the code being run
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    return SandboxConfig(
        token=TOKEN,
        workspaces_root=workspaces,
        scratch_root=tmp_path / "scratch",
        mode="auto",
        max_concurrent=2,
        max_timeout_seconds=30,
        max_memory_mb=1024,
        max_output_bytes=64 * 1024,
        max_file_bytes=8 * 1024 * 1024,
        max_workspace_bytes=16 * 1024 * 1024,
        max_processes=256,
    )


@pytest.fixture(params=MODES)
def mode(request, config) -> str:
    config.scratch_root.mkdir(parents=True, exist_ok=True)
    return request.param
