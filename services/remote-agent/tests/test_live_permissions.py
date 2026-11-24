"""Permissions changed after a session opened apply to its next command."""

import os
import tempfile

import pytest

from celius_agent.config import Policy
from tests.test_runtime import events, make_runtime, settle


@pytest.fixture
def workdir(tmp_path):
    root = tmp_path / "code"
    (root / "proj").mkdir(parents=True)
    return root


async def _open(runtime, workdir, permissions):
    await runtime.handle_command(
        {
            "id": "open",
            "kind": "session.open",
            "session_id": "s1",
            "payload": {
                "origin": "portal",
                "working_directory": str(workdir / "proj"),
                "permissions": permissions,
                "next_seq": 1,
            },
        }
    )
    await settle(runtime)


async def _tool(runtime, command_id, tool, arguments, permissions=None):
    payload = {"tool": tool, "arguments": arguments}
    if permissions is not None:
        payload["permissions"] = permissions
    await runtime.handle_command(
        {"id": command_id, "kind": "tool.execute", "session_id": "s1", "payload": payload}
    )
    await settle(runtime)
    return events(runtime, "s1")[-1]["data"]["result"]


async def test_commands_turned_on_after_the_session_opened(tmp_path, workdir):
    runtime = make_runtime(tmp_path, workdir)
    await _open(runtime, workdir, {"allow_shell": False, "allow_write": True})
    refused = await _tool(runtime, "c1", "run_shell", {"command": "echo hi"})
    assert refused["is_error"] and "turn them on in Celius" in refused["content"]
    # Celius now allows them: the permissions sent with the command count.
    now = {"allow_shell": True, "allow_write": True}
    ran = await _tool(runtime, "c2", "run_shell", {"command": "echo hi"}, now)
    assert not ran["is_error"] and "hi" in ran["content"]
    # ...and are kept for the session's next commands.
    again = await _tool(runtime, "c3", "run_shell", {"command": "echo again"})
    assert "again" in again["content"]


async def test_folder_access_follows_the_policy_as_it_is_now(tmp_path, workdir):
    runtime = make_runtime(tmp_path, workdir)
    await _open(runtime, workdir, {"allow_shell": True, "allow_write": True})
    assert not (await _tool(runtime, "w1", "write_file", {"path": "a.txt", "content": "1"}))[
        "is_error"
    ]
    # The folder is made read only in the policy: writing stops without reopening.
    runtime.policy = Policy(roots=[{"path": str(workdir), "write": False}], allow_shell=True)
    refused = await _tool(runtime, "w2", "write_file", {"path": "a.txt", "content": "2"})
    assert refused["is_error"]
    # Removed from the policy: nothing runs there any more.
    runtime.policy = Policy(roots=[], allow_shell=True)
    gone = await _tool(runtime, "r1", "read_file", {"path": "a.txt"})
    assert gone["is_error"] and "no longer one of the folders" in gone["content"]


async def test_a_file_in_a_folder_that_takes_no_new_files_is_written_in_place(
    tmp_path, workdir, monkeypatch
):
    runtime = make_runtime(tmp_path, workdir)
    await _open(runtime, workdir, {"allow_shell": True, "allow_write": True})
    target = workdir / "proj" / "services.yaml"
    target.write_text("old\n")

    def no_new_files(*args, **kwargs):
        raise PermissionError(13, "Permission denied", kwargs.get("dir"))

    monkeypatch.setattr(tempfile, "mkstemp", no_new_files)
    done = await _tool(runtime, "w1", "write_file", {"path": "services.yaml", "content": "new\n"})
    assert not done["is_error"], done
    assert target.read_text() == "new\n"
    # A new file can't be made there: said plainly, with the user Celius runs as.
    refused = await _tool(runtime, "w2", "write_file", {"path": "other.yaml", "content": "x"})
    assert refused["is_error"] and "Permission denied" in refused["content"]
    assert "runs as" in refused["content"]


async def test_permission_errors_from_tools_are_explained(tmp_path, workdir, monkeypatch):
    runtime = make_runtime(tmp_path, workdir)
    await _open(runtime, workdir, {"allow_shell": True, "allow_write": True})
    (workdir / "proj" / "secret").write_text("x")
    real_open = open

    def denied(path, *args, **kwargs):
        if str(path).endswith("secret"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    result = await _tool(runtime, "r1", "read_file", {"path": "secret"})
    assert result["is_error"]
    assert "Permission denied" in result["content"] and os.sep in result["content"]
