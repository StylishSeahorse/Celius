"""Changing the machine's policy from Celius (Remote machines → the machine)."""

import asyncio
import json
import os

import pytest

from celius_agent.config import Paths, PolicyRefused, load_policy, update_policy_file
from celius_agent.runtime import Runtime
from celius_agent.state import StateStore


@pytest.fixture
def paths(tmp_path):
    paths = Paths(tmp_path / "home")
    load_policy(paths)  # writes the default file
    return paths


def test_folders_and_permissions_are_saved_and_other_keys_kept(paths, tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    raw = json.loads(paths.policy.read_text())
    raw["env_deny"] = ["SECRET"]
    paths.policy.write_text(json.dumps(raw))

    policy = update_policy_file(
        paths,
        {
            "roots": [{"path": str(code), "write": False}, {"path": str(code) + os.sep}],
            "allow_shell": True,
        },
    )
    assert policy.roots == [{"path": os.path.realpath(code), "write": False}]  # deduplicated
    assert policy.allow_shell is True
    saved = json.loads(paths.policy.read_text())
    assert saved["env_deny"] == ["SECRET"] and saved["allow_shell"] is True
    assert saved["allow_write"] is True  # not in the change: unchanged


@pytest.mark.parametrize(
    ("root", "message"),
    [("relative/dir", "not a full path"), ("/no/such/folder/here", "not a folder")],
)
def test_paths_that_are_not_folders_here_are_refused(paths, root, message):
    with pytest.raises(PolicyRefused, match=message):
        update_policy_file(paths, {"roots": [{"path": root}]})
    assert json.loads(paths.policy.read_text())["roots"] == []  # nothing saved


def test_locked_on_the_machine(paths, tmp_path):
    raw = json.loads(paths.policy.read_text())
    raw["allow_remote_edit"] = False
    paths.policy.write_text(json.dumps(raw))
    with pytest.raises(PolicyRefused, match="only be changed on the machine"):
        update_policy_file(paths, {"allow_shell": True})
    assert load_policy(paths).report()["allow_remote_edit"] is False


async def test_the_command_saves_reports_and_answers(paths, tmp_path):
    code = tmp_path / "work"
    code.mkdir()
    runtime = Runtime(
        StateStore(tmp_path / "state"),
        load_policy(paths),
        policy_loader=lambda: load_policy(paths),
        policy_editor=lambda changes: update_policy_file(paths, changes),
    )
    await runtime.handle_command(
        {
            "id": "c1",
            "kind": "policy.update",
            "payload": {"policy": {"roots": [{"path": str(code)}], "allow_remote_edit": False}},
        }
    )
    await asyncio.wait(set(runtime.tasks), timeout=10)
    assert runtime.policy.roots == [{"path": os.path.realpath(code), "write": True}]
    assert runtime.policy.allow_remote_edit is True  # never changed from Celius
    assert runtime.policy_changed
    [result] = runtime.results
    assert result["type"] == "result" and result["command_id"] == "c1"
    assert result["status"] == "completed"
    assert result["result"]["local_policy"]["roots"][0]["path"] == os.path.realpath(code)

    # A refused change answers with the reason, and nothing changes.
    runtime.results.clear()
    await runtime.handle_command(
        {"id": "c2", "kind": "policy.update", "payload": {"policy": {"roots": [{"path": "x"}]}}}
    )
    await asyncio.wait(set(runtime.tasks), timeout=10)
    assert runtime.results[0]["status"] == "failed"
    assert "full path" in runtime.results[0]["error"]
    assert runtime.policy.roots == [{"path": os.path.realpath(code), "write": True}]
    # Re-delivered: never applied twice.
    await runtime.handle_command({"id": "c2", "kind": "policy.update", "payload": {}})
    assert len(runtime.results) == 1
