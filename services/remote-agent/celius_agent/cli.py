"""`celius-agent` command line.

celius-agent register --server https://celius.example --code ABCD-EFGH-JKLM
celius-agent run                  # foreground; what the service runs
celius-agent status               # configuration, policy, and whether Celius accepts us
celius-agent rotate-credential    # new credential (the old one stops working once used)
celius-agent policy               # where the policy file is, and what it allows
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from celius_agent import __version__
from celius_agent.config import (
    AgentConfig,
    Paths,
    check_server_url,
    default_home,
    load_config,
    load_policy,
    save_config,
)
from celius_agent.connection import AuthError, Connection, post_json
from celius_agent.credentials import load_credential, save_credential
from celius_agent.monitor import Monitor, machine_facts
from celius_agent.runtime import Runtime
from celius_agent.state import StateStore

log = logging.getLogger("celius_agent")


def setup_logging(paths: Paths, level: str, *, to_file: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if to_file:
        paths.ensure()
        handler = logging.handlers.RotatingFileHandler(
            paths.logs / "agent.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class InstanceLock:
    """One agent per home directory: a second one would fight over the connection."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> InstanceLock:
        self.handle = open(self.path, "a+")  # noqa: SIM115 - held for the process lifetime
        try:
            if sys.platform == "win32":  # pragma: no cover - Windows only
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SystemExit(
                f"Another celius-agent is already running for {self.path.parent}"
            ) from exc
        return self

    def __exit__(self, *exc) -> None:
        if self.handle:
            self.handle.close()


# ---------------------------------------------------------------------- commands


def cmd_register(args, paths: Paths) -> int:
    try:
        server = check_server_url(args.server, args.allow_insecure_http)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    config = load_config(paths)
    if config.is_registered and not args.force:
        print(
            f"This machine is already registered with {config.server_url} "
            "(use --force to register again).",
            file=sys.stderr,
        )
        return 1
    config = AgentConfig(
        server_url=server,
        ca_file=args.ca_file,
        allow_insecure_http=args.allow_insecure_http,
    )
    try:
        data = post_json(config, "/api/v1/remote/enroll", {"code": args.code, **machine_facts()})
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: can't reach {server}: {exc}", file=sys.stderr)
        return 1
    config.agent_id = data["agent_id"]
    config.name = data.get("name") or ""
    save_config(paths, config)
    save_credential(paths.credential, data["credential"])
    load_policy(paths)  # writes the default policy file if there is none
    print(f"Registered as “{config.name}” ({config.agent_id}).")
    print("Next:")
    print(f"  1. In Celius, open Remote machines and choose Authorise for {config.name}.")
    print(f"  2. Edit {paths.policy} to list the folders Celius may use (roots).")
    print("  3. Start the agent: celius-agent run (or install the service).")
    return 0


async def _run(paths: Paths, config: AgentConfig) -> int:
    credential = load_credential(paths.credential)
    if not credential:
        log.error("No credential at %s; run celius-agent register", paths.credential)
        return 1
    policy = load_policy(paths)
    if not policy.roots:
        log.warning(
            "The policy file lists no folders (roots): sessions can't open yet (%s)", paths.policy
        )
    store = StateStore(paths.state)
    runtime = Runtime(store, policy)
    await runtime.recover_interrupted()
    monitor = Monitor([r["path"] for r in policy.roots])
    connection = Connection(config, runtime, credential, monitor)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    log.info("celius-agent %s starting (machine %s)", __version__, config.agent_id)
    try:
        await connection.run_forever(stop)
    finally:
        await runtime.shutdown()
        log.info("celius-agent stopped")
    return 0


def cmd_run(args, paths: Paths) -> int:
    config = load_config(paths)
    if not config.is_registered:
        print("This machine is not registered; run celius-agent register first.", file=sys.stderr)
        return 1
    setup_logging(paths, args.log_level or config.log_level)
    with InstanceLock(paths.lock):
        return asyncio.run(_run(paths, config))


def cmd_status(args, paths: Paths) -> int:
    config = load_config(paths)
    policy = load_policy(paths) if config.is_registered else None
    info = {
        "version": __version__,
        "home": str(paths.home),
        "registered": config.is_registered,
        "server": config.server_url or None,
        "agent_id": config.agent_id or None,
        "credential_stored": paths.credential.exists()
        or bool(os.environ.get("CELIUS_AGENT_CREDENTIAL")),
        "policy_file": str(paths.policy),
        "policy": policy.report() if policy else None,
    }
    if config.is_registered:
        try:
            credential = load_credential(paths.credential)
            post_json(config, "/api/v1/remote/token", {"credential": credential or ""})
            info["celius"] = "accepted (authorised)"
        except AuthError as exc:
            info["celius"] = f"refused: {exc.code} ({exc})"
        except (OSError, PermissionError) as exc:
            info["celius"] = f"unreachable: {exc}"
    print(json.dumps(info, indent=2))
    return 0


def cmd_rotate(args, paths: Paths) -> int:
    config = load_config(paths)
    credential = load_credential(paths.credential)
    if not config.is_registered or not credential:
        print("Not registered.", file=sys.stderr)
        return 1
    try:
        token = post_json(config, "/api/v1/remote/token", {"credential": credential})[
            "access_token"
        ]
        new = post_json(config, "/api/v1/remote/credentials/rotate", {}, token=token)["credential"]
    except (AuthError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    save_credential(paths.credential, new)
    # Using the new credential once retires the old one at Celius.
    post_json(config, "/api/v1/remote/token", {"credential": new})
    print("Credential rotated. Restart the agent service to use it.")
    return 0


def cmd_policy(args, paths: Paths) -> int:
    policy = load_policy(paths)
    print(f"Policy file: {paths.policy}")
    print(json.dumps(policy.report(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="celius-agent", description="Celius Remote Agent")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--home", help="Agent home folder (default: %(default)s)", default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register", help="Register this machine with Celius")
    register.add_argument(
        "--server", required=True, help="Celius address, e.g. https://celius.example"
    )
    register.add_argument("--code", required=True, help="Pairing code from Celius")
    register.add_argument("--ca-file", help="CA bundle for a private certificate authority")
    register.add_argument("--allow-insecure-http", action="store_true")
    register.add_argument("--force", action="store_true")

    run = commands.add_parser("run", help="Connect and work (foreground)")
    run.add_argument("--log-level", default=None)
    commands.add_parser("status", help="Show configuration and check the server")
    commands.add_parser("rotate-credential", help="Replace this machine's credential")
    commands.add_parser("policy", help="Show this machine's policy")

    args = parser.parse_args(argv)
    paths = Paths(Path(args.home) if args.home else default_home())
    handlers = {
        "register": cmd_register,
        "run": cmd_run,
        "status": cmd_status,
        "rotate-credential": cmd_rotate,
        "policy": cmd_policy,
    }
    return handlers[args.command](args, paths)


if __name__ == "__main__":
    raise SystemExit(main())
