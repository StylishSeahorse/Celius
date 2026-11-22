"""One-command device installers served by Celius (/install.sh, /install.ps1, the wheel)."""

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[2] / "remote-agent"
WHEEL = "celius_agent-9.9.9-py3-none-any.whl"


@pytest.fixture
def dist(settings, tmp_path):
    folder = tmp_path / "agent-dist"
    folder.mkdir()
    for name in ("install-linux.sh", "install-windows.ps1"):
        shutil.copy(AGENT / "install" / name, folder / name)
    (folder / WHEEL).write_bytes(b"PK fake wheel")
    settings.remote_agent_dist_dir = str(folder)
    settings.app_public_url = "https://celius.test/"
    return folder


def _shell_checks(body: str) -> None:
    """The filled-in script still parses, and stays ShellCheck-clean."""
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n"], input=body.encode(), check=False).returncode == 0
    if shutil.which("shellcheck"):
        result = subprocess.run(
            ["shellcheck", "-s", "bash", "-"], input=body.encode(), capture_output=True, check=False
        )
        assert result.returncode == 0, result.stdout.decode()


async def test_linux_script_is_filled_in_for_this_server(client, dist):
    r = await client.get("/install.sh", params={"user": "alice"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.text
    package = f"celius-agent[monitor] @ https://celius.test/install/{WHEEL}"
    assert (
        f"SERVED_SERVER=https://celius.test SERVED_PACKAGE={shlex.quote(package)} "
        "SERVED_USER=alice\n" in body
    )
    assert 'SERVED_SERVER="" SERVED_PACKAGE=""' not in body
    _shell_checks(body)


async def test_windows_script_wraps_the_installer_with_this_servers_values(client, dist):
    r = await client.get("/install.ps1", params={"user": "alice"})
    assert r.status_code == 200
    body = r.text
    assert body.startswith("# Celius for Windows, from celius.test.")
    assert body.rstrip().endswith(
        "} -Server 'https://celius.test' "
        f"-Package 'celius-agent[monitor] @ https://celius.test/install/{WHEEL}' "
        "-CeliusUser 'alice'"
    )
    assert "[CmdletBinding(" in body and "AllowInsecureHttp" in body
    assert "-AllowInsecureHttp\n" not in body  # https: nothing to relax

    upgrade = (await client.get("/install.ps1", params={"upgrade": "1"})).text
    assert upgrade.rstrip().endswith(
        f"-Upgrade -Package 'celius-agent[monitor] @ https://celius.test/install/{WHEEL}'"
    )
    assert (
        (await client.get("/install.ps1", params={"uninstall": "1"}))
        .text.rstrip()
        .endswith("} -Uninstall")
    )


async def test_plain_http_server_lets_its_own_script_connect(client, dist, settings):
    settings.app_public_url = "http://192.168.1.20:8000"
    assert (await client.get("/install.ps1")).text.rstrip().endswith("-AllowInsecureHttp")
    assert "SERVED_SERVER=http://192.168.1.20:8000 " in (await client.get("/install.sh")).text


@pytest.mark.parametrize("user", ["al ice", "a'b", "$(reboot)", "-x"])
async def test_user_names_that_could_inject_are_refused(client, dist, user):
    for path in ("/install.sh", "/install.ps1"):
        r = await client.get(path, params={"user": user})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_user"


async def test_the_package_is_served_by_its_exact_name(client, dist):
    r = await client.get(f"/install/{WHEEL}")
    assert r.status_code == 200
    assert r.content == b"PK fake wheel"
    assert (await client.get("/install/celius_agent-0.0.1-py3-none-any.whl")).status_code == 404
    assert (await client.get("/install/..%2Fsecret")).status_code == 404


async def test_without_a_package_the_installers_say_so(client, dist):
    (dist / WHEEL).unlink()
    r = await client.get("/install.sh")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "installer_unavailable"


async def test_turned_off_with_remote_machines(client, dist, settings):
    settings.remote_agents_enabled = False
    assert (await client.get("/install.sh")).status_code == 404
    assert (await client.get(f"/install/{WHEEL}")).status_code == 404


async def test_add_a_device_shows_the_one_line_commands(client, dist, auth_headers):
    page = (await client.get("/api/v1/remote/devices", headers=auth_headers)).json()
    user = page["login_command"].rsplit(" ", 1)[-1]
    assert (
        page["install_linux"]
        == f'curl -fsSL "https://celius.test/install.sh?user={user}" | sudo bash'
    )
    assert page["install_windows"] == f'irm "https://celius.test/install.ps1?user={user}" | iex'
