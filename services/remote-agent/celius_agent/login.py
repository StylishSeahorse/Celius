"""`celius login`: sign this device in; someone accepts it in Celius.

The device asks Celius for a sign-in (like the OAuth 2.0 device flow): it shows a short
code, the person accepts the device in Celius (Remote machines, or the link), and the
device receives its own credential. No password is typed on the device.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from celius_agent.config import AgentConfig
from celius_agent.connection import AuthError, post_json
from celius_agent.monitor import machine_facts


class LoginFailed(Exception):
    pass


def device_login(
    config: AgentConfig,
    *,
    username: str | None = None,
    name: str | None = None,
    say: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Returns {agent_id, credential, name, username} once accepted."""
    started = post_json(
        config,
        "/api/v1/remote/device/login",
        {**machine_facts(), "username": username or None, "name": name or None},
    )
    interval = max(1, int(started.get("interval") or 3))
    if say is None:
        # Flushed: the code must show at once even when output is piped or logged.
        def say(text: str) -> None:
            print(text, flush=True)

    say("")
    say(f"  Sign-in code:  {started['user_code']}")
    say("")
    if username:
        say(f"  In Celius, open Remote machines: this device waits there for {username}.")
    say(f"  Or open: {started['verification_url']}")
    say("  Check the code matches, then accept the device. Waiting… (Ctrl+C to cancel)")
    deadline = time.monotonic() + int(started.get("expires_in") or 900)
    while time.monotonic() < deadline:
        sleep(interval)
        try:
            return post_json(
                config, "/api/v1/remote/device/login/poll", {"device_code": started["device_code"]}
            )
        except AuthError as exc:
            if exc.code == "authorization_pending":
                continue
            if exc.code == "slow_down":
                interval += 2
                continue
            if exc.code == "access_denied":
                raise LoginFailed("The sign-in was refused in Celius.") from exc
            if exc.code == "expired_token":
                raise LoginFailed("The sign-in expired; run celius login again.") from exc
            raise LoginFailed(str(exc)) from exc
    raise LoginFailed("The sign-in expired; run celius login again.")
