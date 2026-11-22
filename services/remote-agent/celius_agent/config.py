"""Where the agent keeps its files, and the machine's own policy.

Agent home (override with CELIUS_AGENT_HOME or --home):
  Windows: %LOCALAPPDATA%\\CeliusAgent      Linux/macOS: ~/.config/celius-agent
    config.json   server address, agent id, TLS options (written by `celius login`)
    policy.json   what this machine allows: folders, shell, write, agent types. Edited
                  by the machine's owner; Celius can only narrow it, never widen it.
    credential    the machine credential (DPAPI-encrypted on Windows, mode 0600 elsewhere)
    state/        outbox of unsent events, command journal, session state
    logs/         agent.log (rotated)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

IS_WINDOWS = sys.platform == "win32"


def default_home() -> Path:
    env = os.environ.get("CELIUS_AGENT_HOME")
    if env:
        return Path(env)
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "CeliusAgent"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "celius-agent"


@dataclass
class AgentConfig:
    server_url: str = ""
    agent_id: str = ""
    name: str = ""
    # A CA bundle for a server with a private certificate authority.
    ca_file: str | None = None
    # Plain http:// is refused unless this is set (or the server is on localhost).
    allow_insecure_http: bool = False
    log_level: str = "INFO"

    @property
    def is_registered(self) -> bool:
        return bool(self.server_url and self.agent_id)


DEFAULT_POLICY: dict = {
    "_comment": (
        "What Celius may do on this machine. Nothing runs outside 'roots'. You can also "
        "change these in Celius (Remote machines), unless allow_remote_edit is false. Edits "
        "apply to the next session."
    ),
    # false: only someone on this machine can change this file (Celius's editor is refused).
    "allow_remote_edit": True,
    # Folders sessions may use, e.g. [{"path": "/home/me/code", "write": true}]
    "roots": [],
    # Run shell commands, Python and git (Celius still asks before each one by default).
    "allow_shell": False,
    "allow_write": True,
    "max_concurrent_commands": 2,
    "max_command_seconds": 3600,
    "max_output_chars": 1_000_000,
    # Linux/macOS only: address-space limit for each command, in MB (null: none).
    "memory_limit_mb": None,
    # Environment variables never passed to commands (besides the agent's own).
    "env_deny": ["CELIUS_AGENT_CREDENTIAL"],
}


@dataclass
class Policy:
    roots: list[dict] = field(default_factory=list)
    allow_shell: bool = False
    allow_write: bool = True
    max_concurrent_commands: int = 2
    max_command_seconds: int = 3600
    max_output_chars: int = 1_000_000
    memory_limit_mb: int | None = None
    env_deny: list[str] = field(default_factory=list)
    allow_remote_edit: bool = True

    @classmethod
    def from_dict(cls, raw: dict) -> Policy:
        roots = []
        for root in raw.get("roots") or []:
            if isinstance(root, str):
                root = {"path": root, "write": True}
            if isinstance(root, dict) and isinstance(root.get("path"), str):
                path = os.path.expanduser(os.path.expandvars(root["path"]))
                if os.path.isabs(path):
                    roots.append(
                        {"path": os.path.realpath(path), "write": bool(root.get("write", True))}
                    )
        return cls(
            roots=roots,
            allow_shell=bool(raw.get("allow_shell", False)),
            allow_write=bool(raw.get("allow_write", True)),
            max_concurrent_commands=max(1, int(raw.get("max_concurrent_commands") or 2)),
            max_command_seconds=max(1, int(raw.get("max_command_seconds") or 3600)),
            max_output_chars=max(10_000, int(raw.get("max_output_chars") or 1_000_000)),
            memory_limit_mb=raw.get("memory_limit_mb") or None,
            env_deny=[str(v) for v in raw.get("env_deny") or []],
            allow_remote_edit=raw.get("allow_remote_edit", True) is not False,
        )

    def report(self) -> dict:
        """What is sent to Celius in `hello`."""
        return {
            "roots": self.roots,
            "allow_shell": self.allow_shell,
            "allow_write": self.allow_write,
            "max_concurrent_commands": self.max_concurrent_commands,
            "allow_remote_edit": self.allow_remote_edit,
        }


class Paths:
    def __init__(self, home: Path):
        self.home = home
        self.config = home / "config.json"
        self.policy = home / "policy.json"
        self.credential = home / "credential"
        self.state = home / "state"
        self.logs = home / "logs"
        self.lock = home / "agent.lock"

    def ensure(self) -> None:
        for directory in (self.home, self.state, self.logs):
            directory.mkdir(parents=True, exist_ok=True)
            if not IS_WINDOWS:
                os.chmod(directory, 0o700)


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if not IS_WINDOWS:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_config(paths: Paths) -> AgentConfig:
    if not paths.config.exists():
        return AgentConfig()
    raw = json.loads(paths.config.read_text(encoding="utf-8"))
    known = {k: v for k, v in raw.items() if k in AgentConfig.__dataclass_fields__}
    return AgentConfig(**known)


def save_config(paths: Paths, config: AgentConfig) -> None:
    paths.ensure()
    _write_json(paths.config, asdict(config))


def _policy_raw(paths: Paths) -> dict:
    if not paths.policy.exists():
        paths.ensure()
        _write_json(paths.policy, DEFAULT_POLICY)
    raw = json.loads(paths.policy.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def load_policy(paths: Paths) -> Policy:
    return Policy.from_dict(_policy_raw(paths))


class PolicyRefused(ValueError):
    """A change from Celius this machine won't make (shown to the person who asked)."""


MAX_ROOTS = 50


def update_policy_file(paths: Paths, changes: dict) -> Policy:
    """A change made in Celius (Remote machines → the machine): checked here, against this
    machine's own file system, then saved in policy.json. Keys not changed are kept."""
    raw = _policy_raw(paths)
    if raw.get("allow_remote_edit") is False:
        raise PolicyRefused(
            "This machine's policy can only be changed on the machine itself "
            f"(allow_remote_edit is false in {paths.policy})."
        )
    if "roots" in changes:
        roots = changes["roots"]
        if not isinstance(roots, list) or len(roots) > MAX_ROOTS:
            raise PolicyRefused(f"Give a list of at most {MAX_ROOTS} folders.")
        cleaned: list[dict] = []
        for root in roots:
            path = root.get("path") if isinstance(root, dict) else None
            if not isinstance(path, str) or not path.strip():
                raise PolicyRefused("A folder has no path.")
            expanded = os.path.expanduser(os.path.expandvars(path.strip()))
            if not os.path.isabs(expanded):
                raise PolicyRefused(f"{path} is not a full path on this machine.")
            if not os.path.isdir(expanded):
                raise PolicyRefused(
                    f"{path} is not a folder that Celius can reach on this machine (check it "
                    "exists and that the user the agent runs as can open it)."
                )
            resolved = os.path.realpath(expanded)
            if not any(r["path"] == resolved for r in cleaned):
                cleaned.append({"path": resolved, "write": bool(root.get("write", True))})
        raw["roots"] = cleaned
    for key in ("allow_shell", "allow_write"):
        if key in changes:
            raw[key] = bool(changes[key])
    _write_json(paths.policy, raw)
    return Policy.from_dict(raw)


def check_server_url(url: str, allow_insecure_http: bool) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise ValueError("The server address must look like https://celius.example.com")
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme == "http" and not (local or allow_insecure_http):
        raise ValueError(
            "Refusing plain http:// (the credential would cross the network unencrypted). "
            "Use https://, or pass --allow-insecure-http on a network you trust."
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def websocket_url(server_url: str, path: str) -> str:
    parts = urlsplit(server_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}{path}"
