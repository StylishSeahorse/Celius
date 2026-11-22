"""One-command device installers, served by Celius itself (docs/remote/INSTALL.md).

    Linux:    curl -fsSL https://celius.example/install.sh | sudo bash
    Windows:  irm https://celius.example/install.ps1 | iex

Each script is the repository's installer with this server's address, the celius package
(a wheel downloaded from here) and, with `?user=`, the Celius user name filled in. Nothing
needs the repository on the device.
"""

import re
import shlex
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, PlainTextResponse

from app.config import Settings, get_settings
from app.errors import AppError

AppSettings = Annotated[Settings, Depends(get_settings)]
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# The line of install-linux.sh that this module fills in.
LINUX_DEFAULTS = 'SERVED_SERVER="" SERVED_PACKAGE="" SERVED_USER=""'
_REPO_AGENT = Path(__file__).resolve().parents[4] / "services" / "remote-agent"

router = APIRouter(tags=["remote"], include_in_schema=False)


def _enabled(settings: AppSettings) -> None:
    if not settings.remote_agents_enabled:
        raise AppError(404, "remote_disabled", "Remote machines are turned off on this server")


def _script(settings: Settings, name: str) -> Path:
    base = (
        Path(settings.remote_agent_dist_dir)
        if settings.remote_agent_dist_dir
        else _REPO_AGENT / "install"
    )
    path = base / name
    if not path.is_file():
        raise AppError(503, "installer_unavailable", f"{name} is not available on this server")
    return path


def agent_wheel(settings: Settings) -> Path | None:
    """The newest celius wheel this server offers, if any."""
    base = (
        Path(settings.remote_agent_dist_dir)
        if settings.remote_agent_dist_dir
        else _REPO_AGENT / "dist"
    )
    wheels = sorted(base.glob("celius_agent-*-py3-none-any.whl"), key=lambda p: p.stat().st_mtime)
    return wheels[-1] if wheels else None


def public_url(settings: Settings) -> str:
    return settings.app_public_url.rstrip("/")


def install_commands(settings: Settings, username: str | None) -> dict[str, str]:
    """The one-line install commands shown in Celius (Remote machines → Add a device)."""
    base = public_url(settings)
    query = f"?user={quote(username)}" if username and USERNAME.match(username) else ""
    linux = f"{base}/install.sh{query}"
    windows = f"{base}/install.ps1{query}"
    return {
        "linux": f'curl -fsSL "{linux}" | sudo bash'
        if query
        else f"curl -fsSL {linux} | sudo bash",
        "windows": f'irm "{windows}" | iex' if query else f"irm {windows} | iex",
    }


def _package(settings: Settings) -> str:
    wheel = agent_wheel(settings)
    if wheel is None:
        raise AppError(
            503,
            "installer_unavailable",
            "This server has no celius package to install (build it with `make agent-wheel`)",
        )
    return f"celius-agent[monitor] @ {public_url(settings)}/install/{wheel.name}"


def _user(user: str | None) -> str:
    if user is None or user == "":
        return ""
    if not USERNAME.match(user):
        raise AppError(400, "invalid_user", "Not a Celius user name")
    return user


def _plain_http(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1")


def _ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _text(body: str) -> PlainTextResponse:
    return PlainTextResponse(
        body,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/install.sh", dependencies=[Depends(_enabled)])
async def install_sh(
    settings: AppSettings, user: Annotated[str | None, Query(max_length=64)] = None
) -> PlainTextResponse:
    """`curl -fsSL …/install.sh | sudo bash` (add `-s -- --upgrade` to upgrade)."""
    script = _script(settings, "install-linux.sh").read_text(encoding="utf-8")
    if LINUX_DEFAULTS not in script:
        raise AppError(503, "installer_unavailable", "install-linux.sh has no defaults line")
    filled = (
        f"SERVED_SERVER={shlex.quote(public_url(settings))} "
        f"SERVED_PACKAGE={shlex.quote(_package(settings))} "
        f"SERVED_USER={shlex.quote(_user(user))}"
    )
    return _text(script.replace(LINUX_DEFAULTS, filled, 1))


@router.get("/install.ps1", dependencies=[Depends(_enabled)])
async def install_ps1(
    settings: AppSettings,
    user: Annotated[str | None, Query(max_length=64)] = None,
    upgrade: bool = False,
    uninstall: bool = False,
) -> PlainTextResponse:
    """`irm …/install.ps1 | iex` (`?upgrade=1` / `?uninstall=1` for those)."""
    script = _script(settings, "install-windows.ps1").read_text(encoding="utf-8")
    if uninstall:
        args = "-Uninstall"
    elif upgrade:
        args = f"-Upgrade -Package {_ps(_package(settings))}"
    else:
        args = f"-Server {_ps(public_url(settings))} -Package {_ps(_package(settings))}"
        if name := _user(user):
            args += f" -CeliusUser {_ps(name)}"
        if _plain_http(public_url(settings)):
            args += " -AllowInsecureHttp"  # the script itself came from this address
    host = urlsplit(public_url(settings)).netloc
    body = (
        f"# Celius for Windows, from {host}. Run in PowerShell (not as administrator):\n"
        f"#   irm {public_url(settings)}/install.ps1 | iex\n"
        f"& {{\n{script}\n}} {args}\n"
    )
    return _text(body)


@router.get("/install/{filename}", dependencies=[Depends(_enabled)])
async def install_wheel(filename: str, settings: AppSettings) -> FileResponse:
    """The celius package the installers download."""
    wheel = agent_wheel(settings)
    if wheel is None or filename != wheel.name:
        raise AppError(404, "not_found", "No such package")
    return FileResponse(
        wheel,
        media_type="application/octet-stream",
        filename=wheel.name,
        headers={"Cache-Control": "public, max-age=3600"},
    )
