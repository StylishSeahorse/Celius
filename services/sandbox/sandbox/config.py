import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SandboxConfig:
    """Read from the environment. The service holds no other secrets than its token."""

    token: str = field(default_factory=lambda: os.environ.get("SANDBOX_TOKEN", ""))
    workspaces_root: Path = field(
        default_factory=lambda: Path(os.environ.get("SANDBOX_WORKSPACES", "/workspaces"))
    )
    scratch_root: Path = field(
        default_factory=lambda: Path(os.environ.get("SANDBOX_SCRATCH", "/tmp/celius-sandbox"))
    )
    # "auto": bubblewrap if this kernel/seccomp allows it, else process limits only.
    mode: str = field(default_factory=lambda: os.environ.get("SANDBOX_MODE", "auto"))
    max_concurrent: int = field(
        default_factory=lambda: int(os.environ.get("SANDBOX_MAX_CONCURRENT", "2"))
    )
    max_timeout_seconds: int = field(
        default_factory=lambda: int(os.environ.get("SANDBOX_MAX_TIMEOUT_SECONDS", "300"))
    )
    max_memory_mb: int = field(
        default_factory=lambda: int(os.environ.get("SANDBOX_MAX_MEMORY_MB", "2048"))
    )
    max_output_bytes: int = field(
        default_factory=lambda: int(os.environ.get("SANDBOX_MAX_OUTPUT_BYTES", str(1024 * 1024)))
    )
    max_file_bytes: int = field(
        default_factory=lambda: int(os.environ.get("SANDBOX_MAX_FILE_BYTES", str(512 * 1024**2)))
    )
    max_workspace_bytes: int = field(
        default_factory=lambda: int(os.environ.get("SANDBOX_MAX_WORKSPACE_BYTES", str(2 * 1024**3)))
    )
    max_processes: int = field(
        default_factory=lambda: int(os.environ.get("SANDBOX_MAX_PROCESSES", "128"))
    )
