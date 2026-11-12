import os
import sys

import pytest

from celius_agent.config import Policy, check_server_url, websocket_url
from celius_agent.credentials import load_credential, save_credential
from celius_agent.paths import PathRefused, check_working_directory, resolve_in


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "allowed"
    (root / "project" / "src").mkdir(parents=True)
    (root / "project" / "src" / "app.py").write_text("print('hi')\n")
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "keys.txt").write_text("nope")
    return root, outside


def test_working_directory_must_be_inside_a_root(tree):
    root, outside = tree
    roots = [{"path": str(root), "write": False}]
    directory, writable = check_working_directory(str(root / "project"), roots)
    assert directory == os.path.realpath(root / "project")
    assert writable is False
    with pytest.raises(PathRefused):
        check_working_directory(str(outside), roots)
    with pytest.raises(PathRefused):
        check_working_directory("relative/path", roots)
    with pytest.raises(PathRefused):
        check_working_directory(str(root / "missing"), roots)
    with pytest.raises(PathRefused):
        check_working_directory(str(root / "project"), [])  # no roots: nothing allowed


def test_most_specific_root_decides_writability(tree):
    root, _ = tree
    roots = [{"path": str(root), "write": True}, {"path": str(root / "project"), "write": False}]
    assert check_working_directory(str(root / "project" / "src"), roots)[1] is False


def test_paths_cannot_escape_the_working_directory(tree):
    root, outside = tree
    workdir = os.path.realpath(root / "project")
    assert resolve_in(workdir, "src/app.py").endswith("app.py")
    for bad in ("../../secret/keys.txt", str(outside / "keys.txt"), "src/../../..", "a\x00b"):
        with pytest.raises(PathRefused):
            resolve_in(workdir, bad)
    with pytest.raises(PathRefused):
        resolve_in(workdir, "")  # a file path is required
    assert resolve_in(workdir, "", allow_root=True) == workdir


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinks_out_of_the_working_directory_are_refused(tree):
    root, outside = tree
    workdir = root / "project"
    (workdir / "escape").symlink_to(outside)
    with pytest.raises(PathRefused):
        resolve_in(os.path.realpath(workdir), "escape/keys.txt")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
def test_windows_paths_are_case_insensitive(tree):  # pragma: no cover - Windows CI
    root, _ = tree
    roots = [{"path": str(root).upper(), "write": True}]
    check_working_directory(str(root / "project").lower(), roots)


def test_policy_defaults_are_restrictive():
    policy = Policy.from_dict({})
    assert policy.roots == []
    assert policy.allow_shell is False
    assert policy.adapters == ["celius"]
    report = Policy.from_dict(
        {"roots": ["relative", "/tmp"], "adapters": ["claude_code", "evil"]}
    ).report()
    assert [r["path"] for r in report["roots"]] == [
        os.path.realpath("/tmp")
    ] or sys.platform == "win32"
    assert report["adapters"] == ["claude_code"]


def test_server_url_requires_https_except_localhost():
    assert check_server_url("https://celius.example/", False) == "https://celius.example"
    assert check_server_url("http://localhost:8000", False) == "http://localhost:8000"
    with pytest.raises(ValueError):
        check_server_url("http://192.168.0.10:8000", False)
    assert check_server_url("http://192.168.0.10:8000", True).startswith("http://")
    with pytest.raises(ValueError):
        check_server_url("ftp://x", True)
    assert websocket_url("https://c.example/base", "/api/x") == "wss://c.example/base/api/x"
    assert websocket_url("http://localhost:8000", "/api/x") == "ws://localhost:8000/api/x"


def test_credential_file_is_private(tmp_path, monkeypatch):
    monkeypatch.delenv("CELIUS_AGENT_CREDENTIAL", raising=False)
    path = tmp_path / "credential"
    save_credential(path, "cra_secret")
    assert load_credential(path) == "cra_secret"
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o077 == 0
        os.chmod(path, 0o644)
        with pytest.raises(PermissionError):
            load_credential(path)
    else:  # pragma: no cover - Windows CI: DPAPI-encrypted at rest
        assert "cra_secret" not in path.read_text()
