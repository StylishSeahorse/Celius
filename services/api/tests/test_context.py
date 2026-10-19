import pytest

from app.errors import AppError
from app.services.context import HistoryItem, build_context, context_budget, estimate_tokens


def h(*pairs):
    return [HistoryItem(role, text) for role, text in pairs]


def test_budget():
    assert context_budget(None, 512, 256) is None
    assert context_budget(8192, 1024, 256) == 8192 - 1024 - 256
    assert context_budget(8192, None, 0) == 8192 - 2048
    assert context_budget(100, 200, 0) == 0


def test_keeps_everything_when_it_fits():
    built = build_context("sys", h(("user", "a"), ("assistant", "b"), ("user", "c")), None)
    assert [m.role for m in built.messages] == ["system", "user", "assistant", "user"]
    assert built.dropped_messages == 0


def test_drops_oldest_turns_but_keeps_system_and_latest():
    history = h(("user", "x" * 300), ("assistant", "y" * 300), ("user", "latest"))
    budget = estimate_tokens("sys") + estimate_tokens("latest") + estimate_tokens("y" * 300)
    built = build_context("sys", history, budget)
    # The assistant turn fit, but a history can't start with an assistant turn: it is trimmed too.
    assert [(m.role, m.content) for m in built.messages] == [("system", "sys"), ("user", "latest")]
    assert built.dropped_messages == 2


def test_overflow_of_latest_message_is_an_error():
    with pytest.raises(AppError) as exc:
        build_context("sys", h(("user", "z" * 3000)), 100)
    assert exc.value.code == "context_overflow"


def test_consecutive_roles_merged_and_empty_skipped():
    built = build_context("", h(("user", "one"), ("assistant", ""), ("user", "two")), None)
    assert [(m.role, m.content) for m in built.messages] == [("user", "one\n\ntwo")]


def test_must_end_with_user():
    with pytest.raises(AppError):
        build_context("sys", h(("user", "a"), ("assistant", "b")), None)
