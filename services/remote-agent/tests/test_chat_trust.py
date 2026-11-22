import json

from celius_agent.chat import _result_text, _summary, ensure_folder_allowed
from celius_agent.config import Paths, load_policy


class Answers:
    def __init__(self, *answers: bool):
        self.answers = list(answers)
        self.asked: list[str] = []
        self.notes: list[str] = []

    def confirm(self, question: str) -> bool:
        self.asked.append(question)
        return self.answers.pop(0)

    def note(self, text: str) -> None:
        self.notes.append(text)


def test_starting_in_a_new_folder_asks_once_and_records_it(tmp_path):
    paths = Paths(tmp_path / "home")
    load_policy(paths)  # the default: no folders, no commands
    work = tmp_path / "project"
    work.mkdir()
    ui = Answers(True, True)
    assert ensure_folder_allowed(paths, str(work), ui)
    assert len(ui.asked) == 2
    policy = json.loads(paths.policy.read_text())
    assert policy["roots"] == [{"path": str(work), "write": True}]
    assert policy["allow_shell"] is True
    # Next time: nothing to ask.
    again = Answers()
    assert ensure_folder_allowed(paths, str(work / "src"), again)  # inside it
    assert again.asked == []


def test_declining_keeps_the_policy(tmp_path):
    paths = Paths(tmp_path / "home")
    load_policy(paths)
    before = paths.policy.read_text()
    assert not ensure_folder_allowed(paths, str(tmp_path), Answers(False))
    assert paths.policy.read_text() == before
    # Folder yes, commands no.
    assert ensure_folder_allowed(paths, str(tmp_path), Answers(True, False))
    assert json.loads(paths.policy.read_text()).get("allow_shell") is False


def test_tool_lines_are_short():
    assert _summary({"arguments": {"command": "ls -la\npwd"}}) == "ls -la …"
    assert _summary({"arguments": {"args": ["status", "-s"]}}) == "git status -s"
    text = _result_text({"result": {"stdout": "\n".join(str(i) for i in range(30))}})
    assert text.splitlines()[0] == "… 18 more lines"
