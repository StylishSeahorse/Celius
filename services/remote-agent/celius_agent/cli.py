"""`celius`: Celius on this device.

celius login --server https://celius.example [--user you]
      Sign this device in. It shows a code; accept the device in Celius.
celius                     Chat here: Celius works in the current folder.
celius -p "fix the tests"  One message, then exit.
celius --continue          Continue the latest chat in this folder.
celius agent               The background agent Celius controls (what the service runs).
celius status | policy | rotate-credential | logout
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
import threading
from pathlib import Path

from celius_agent import __version__
from celius_agent.client import ApiError, DeviceClient
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
from celius_agent.monitor import Monitor
from celius_agent.runtime import Runtime
from celius_agent.state import StateStore

log = logging.getLogger("celius_agent")


def setup_logging(paths: Paths, level: str, *, to_stderr: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    if to_stderr:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    paths.ensure()
    handler = logging.handlers.RotatingFileHandler(
        paths.logs / "agent.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class InstanceLock:
    """One agent per home folder: a second one would fight over the connection."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.handle = open(self.path, "a+")  # noqa: SIM115 - held for the process lifetime
        try:
            if sys.platform == "win32":  # pragma: no cover - Windows only
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None


# ---------------------------------------------------------------------- the agent


async def run_agent(paths: Paths, config: AgentConfig, stop: asyncio.Event) -> int:
    credential = load_credential(paths.credential)
    if not credential:
        log.error("This device isn't signed in; run: celius login --server …")
        return 1
    policy = load_policy(paths)
    if not policy.roots:
        log.warning("No folders allowed yet (roots in %s)", paths.policy)
    runtime = Runtime(StateStore(paths.state), policy, policy_loader=lambda: load_policy(paths))
    await runtime.recover_interrupted()
    connection = Connection(config, runtime, credential, Monitor([r["path"] for r in policy.roots]))
    log.info("Celius agent %s starting (machine %s)", __version__, config.agent_id)
    try:
        await connection.run_forever(stop)
    finally:
        await runtime.shutdown()
        log.info("Celius agent stopped")
    return 0


class BackgroundAgent:
    """The agent on a thread of its own, for as long as `celius` (the chat) runs, when no
    agent service is running for this device."""

    def __init__(self, paths: Paths, config: AgentConfig):
        self.paths, self.config = paths, config
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop: asyncio.Event | None = None
        self.thread = threading.Thread(target=self._main, name="celius-agent", daemon=True)
        self.started = threading.Event()

    def _main(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.stop = asyncio.Event()
        self.started.set()
        self.loop.run_until_complete(run_agent(self.paths, self.config, self.stop))

    def start(self) -> None:
        self.thread.start()
        self.started.wait(5)

    def close(self) -> None:
        if self.loop and self.stop:
            self.loop.call_soon_threadsafe(self.stop.set)
            self.thread.join(15)


# ---------------------------------------------------------------------- commands


def _need_login(paths: Paths) -> AgentConfig | None:
    config = load_config(paths)
    if (
        not config.is_registered
        or not paths.credential.exists()
        and not os.environ.get("CELIUS_AGENT_CREDENTIAL")
    ):
        print("This device isn't signed in. Run: celius login --server https://…", file=sys.stderr)
        return None
    return config


def cmd_login(args, paths: Paths) -> int:
    from celius_agent.login import LoginFailed, device_login

    try:
        server = check_server_url(args.server, args.allow_insecure_http)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    current = load_config(paths)
    if current.is_registered and not args.force:
        print(
            f"Already signed in to {current.server_url} as “{current.name}”. "
            "Use --force to sign in again (or celius logout first).",
            file=sys.stderr,
        )
        return 1
    config = AgentConfig(
        server_url=server, ca_file=args.ca_file, allow_insecure_http=args.allow_insecure_http
    )
    try:
        result = device_login(config, username=args.user, name=args.name)
    except LoginFailed as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except AuthError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"\nerror: can't reach {server}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    config.agent_id, config.name = result["agent_id"], result.get("name") or ""
    save_config(paths, config)
    save_credential(paths.credential, result["credential"])
    load_policy(paths)  # writes the default policy file if there is none
    print(f"\nSigned in as “{config.name}”, a machine of {result.get('username')}.")
    print("  celius          chat here, in the current folder")
    print("  celius agent    let Celius use this device from anywhere (or install the service)")
    print(f"Folders Celius may use are listed in {paths.policy}.")
    return 0


def cmd_logout(args, paths: Paths) -> int:
    config = _need_login(paths)
    if config is None:
        return 1
    try:
        client = DeviceClient(config, load_credential(paths.credential) or "")
        client.call("POST", "/api/v1/remote/device/logout")
    except (ApiError, OSError) as exc:
        print(f"Celius could not be told ({exc}); removing the sign-in here anyway.")
    paths.credential.unlink(missing_ok=True)
    save_config(paths, AgentConfig())
    print("Signed out. This device is removed from your machines in Celius.")
    return 0


def cmd_agent(args, paths: Paths) -> int:
    config = _need_login(paths)
    if config is None:
        return 1
    setup_logging(paths, args.log_level or config.log_level)
    lock = InstanceLock(paths.lock)
    if not lock.acquire():
        print(f"The Celius agent is already running for {paths.home}.", file=sys.stderr)
        return 1

    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # Windows
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        return await run_agent(paths, config, stop)

    try:
        return asyncio.run(main())
    finally:
        lock.release()


def cmd_chat(args, paths: Paths) -> int:
    from celius_agent.chat import Chat, TerminalUI, ensure_folder_allowed, repl

    config = _need_login(paths)
    if config is None:
        return 1
    ui = TerminalUI()
    directory = os.path.realpath(args.dir or os.getcwd())
    if not ensure_folder_allowed(paths, directory, ui):
        print(
            f"Celius may not work in {directory}. Add it to roots in {paths.policy}, or start "
            "celius in an allowed folder.",
            file=sys.stderr,
        )
        return 1
    # The agent that runs the tools: the service if it is running, else this process.
    lock = InstanceLock(paths.lock)
    background = None
    if lock.acquire():
        setup_logging(paths, config.log_level, to_stderr=False)
        background = BackgroundAgent(paths, config)
        background.start()
    client = DeviceClient(config, load_credential(paths.credential) or "")
    chat = Chat(client, ui)
    try:
        if args.continue_:
            here = [c for c in chat.chats() if c["working_directory"] == directory]
            if here:
                chat.resume(here[0]["conversation_id"])
        if chat.conversation_id is None:
            chat.start(directory, model=args.model)
        if args.prompt:
            status = chat.send(args.prompt)
            return 0 if status == "completed" else 1
        ui.note(f"Celius · {config.name} · {directory}")
        ui.note(f"Also in Celius: {chat.url}   (/help for commands)")
        return repl(chat, ui, directory)
    except ApiError as exc:
        ui.error(exc.message)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if background:
            background.close()
        lock.release()


def cmd_status(args, paths: Paths) -> int:
    config = load_config(paths)
    policy = load_policy(paths) if config.is_registered else None
    info = {
        "version": __version__,
        "home": str(paths.home),
        "signed_in": config.is_registered,
        "server": config.server_url or None,
        "machine": config.name or None,
        "agent_id": config.agent_id or None,
        "policy_file": str(paths.policy),
        "policy": policy.report() if policy else None,
    }
    if config.is_registered:
        lock = InstanceLock(paths.lock)
        info["agent_running"] = not lock.acquire()
        lock.release()
        try:
            post_json(
                config,
                "/api/v1/remote/token",
                {"credential": load_credential(paths.credential) or ""},
            )
            info["celius"] = "accepted"
        except AuthError as exc:
            info["celius"] = f"refused: {exc.code} ({exc})"
        except OSError as exc:
            info["celius"] = f"unreachable: {exc}"
    print(json.dumps(info, indent=2))
    return 0


def cmd_rotate(args, paths: Paths) -> int:
    config = _need_login(paths)
    if config is None:
        return 1
    credential = load_credential(paths.credential) or ""
    try:
        token = post_json(config, "/api/v1/remote/token", {"credential": credential})[
            "access_token"
        ]
        new = post_json(config, "/api/v1/remote/credentials/rotate", {}, token=token)["credential"]
    except (AuthError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    save_credential(paths.credential, new)
    post_json(config, "/api/v1/remote/token", {"credential": new})  # retires the old one
    print("Credential replaced. Restart the agent service to use it.")
    return 0


def cmd_policy(args, paths: Paths) -> int:
    policy = load_policy(paths)
    print(f"Policy file: {paths.policy}")
    print(json.dumps(policy.report(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="celius", description="Celius on this device")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--home", default=None, help="Folder for this device's sign-in and state")
    # The chat's options (used when no command is given).
    parser.add_argument("-p", "--prompt", help="Send one message, print the reply, exit")
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_",
        action="store_true",
        help="Continue the latest chat in this folder",
    )
    parser.add_argument("--dir", help="Work in this folder instead of the current one")
    parser.add_argument("--model", help="Model for a new chat")
    commands = parser.add_subparsers(dest="command")

    login = commands.add_parser("login", help="Sign this device in (accept it in Celius)")
    login.add_argument(
        "--server", required=True, help="Celius address, e.g. https://celius.example"
    )
    login.add_argument("--user", help="Your Celius user name: the device waits in your list")
    login.add_argument("--name", help="A name for this machine in Celius")
    login.add_argument("--ca-file", help="CA bundle for a private certificate authority")
    login.add_argument("--allow-insecure-http", action="store_true")
    login.add_argument("--force", action="store_true", help="Sign in again")

    commands.add_parser("chat", help="Chat here (the default)")
    for name in ("agent", "run"):
        agent = commands.add_parser(
            name, help="Run the background agent" if name == "agent" else argparse.SUPPRESS
        )
        agent.add_argument("--log-level", default=None)
    commands.add_parser("logout", help="Sign this device out and remove it from Celius")
    commands.add_parser("status", help="Sign-in, policy, and whether Celius accepts this device")
    commands.add_parser("policy", help="What this device allows")
    commands.add_parser("rotate-credential", help="Replace this device's credential")

    args = parser.parse_args(argv)
    paths = Paths(Path(args.home) if args.home else default_home())
    handlers = {
        None: cmd_chat,
        "chat": cmd_chat,
        "login": cmd_login,
        "logout": cmd_logout,
        "agent": cmd_agent,
        "run": cmd_agent,
        "status": cmd_status,
        "policy": cmd_policy,
        "rotate-credential": cmd_rotate,
    }
    return handlers[args.command](args, paths)


if __name__ == "__main__":
    raise SystemExit(main())
