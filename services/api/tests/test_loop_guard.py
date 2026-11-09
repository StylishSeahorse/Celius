"""A model stuck repeating itself is stopped and recovered from (ADR C-15)."""

import glob
from pathlib import Path

import pytest

from app.models import RunStatus
from app.services.model_gateway import ChatMessage, ChatRequest
from app.services.model_gateway.openai_compatible import _chat_payload
from app.services.model_gateway.repetition import ANSWER, REASONING, RepetitionGuard
from app.worker.executor import LOOP_NUDGE
from tests.fakes import FakeLlama, drain_runs
from tests.test_tools import detail, run_status, start

ROOT = Path(__file__).resolve().parents[3]

INTRO = (
    "The user wants to fix the nested boot folder. Let's look at the previous command.\n"
    "If the destination didn't exist, rsync creates the folder and puts everything in it.\n"
)
# The shape of a real stuck trace: the same few lines, over and over.
CYCLE = (
    'One detail: The user said "exclude roms_base".\nI will mention it.\n\nOkay.\n\n'
    "Let's write.\n\nFinal check.\nThe response should be helpful and clear.\n\nOkay.\n\n"
    "One detail: `rsync` command.\n`cd /mnt/ssd/boot/`\n`rsync -aAXv boot/ .`\n\n"
    "This is the key.\n\nThen UUID.\n\nThen userdata.\n\n"
)
LOOP = INTRO + CYCLE * 60


def feed(text: str, guard: RepetitionGuard, step: int = 37) -> int | None:
    """Where (in characters) the guard tripped, or None."""
    for i in range(0, len(text), step):
        if guard.feed(text[i : i + step]):
            return i
    return None


def docs() -> str:
    return "".join(
        Path(f).read_text() for f in sorted(glob.glob(f"{ROOT}/docs/**/*.md", recursive=True))
    )


# ---------------------------------------------------------------------- the guard


def test_guard_catches_loops_early():
    guard = RepetitionGuard(REASONING)
    at = feed(LOOP, guard)
    assert guard.tripped == "repeating" and at is not None and at < 2500
    assert len(INTRO) <= guard.loop_start < at  # what came before the loop is kept

    varied = INTRO + "".join(
        f"One detail: check item {i % 7}.\nOkay.\nLet's write.\nI will mention it.\nFinal check.\n"
        for i in range(200)
    )
    assert feed(varied, RepetitionGuard(REASONING)) is not None

    # Without any sentence breaks: the verbatim cycle check.
    flat = INTRO + "and then the next thing we do is the same thing again " * 200
    assert feed(flat, RepetitionGuard(REASONING)) is not None


def test_guard_leaves_normal_text_alone():
    text = docs()
    assert len(text) > 100_000
    assert feed(text, RepetitionGuard(REASONING)) is None
    assert feed(text, RepetitionGuard(ANSWER)) is None
    code = "".join(
        Path(f).read_text()
        for f in sorted(glob.glob(f"{ROOT}/services/api/app/**/*.py", recursive=True))[:80]
    )
    assert feed(code, RepetitionGuard(ANSWER)) is None
    assert feed(code, RepetitionGuard(REASONING)) is None
    # Drafting code while thinking, then writing it out again, is not a loop.
    draft = "".join(
        Path(f).read_text()
        for f in sorted(glob.glob(f"{ROOT}/services/api/app/services/tools/*.py"))[:3]
    )
    assert feed(f"Draft:\n{draft}\nFinal version:\n{draft}", RepetitionGuard(REASONING)) is None


def test_guard_length_cap():
    from dataclasses import replace

    guard = RepetitionGuard(replace(REASONING, max_chars=5000))
    assert feed(docs(), guard) is not None and guard.tripped == "too_long"


def test_anti_repetition_sampling_is_sent_only_where_understood():
    request = ChatRequest(
        model="m",
        messages=[ChatMessage(role="user", content="hi")],
        presence_penalty=1.2,
        repeat_penalty=1.1,
        dry_multiplier=0.8,
    )
    llama = _chat_payload(request, llama_cpp=True)
    assert (llama["presence_penalty"], llama["repeat_penalty"], llama["dry_multiplier"]) == (
        1.2,
        1.1,
        0.8,
    )
    other = _chat_payload(request, llama_cpp=False)
    assert other["presence_penalty"] == 1.2
    assert "dry_multiplier" not in other and "repeat_penalty" not in other
    plain = _chat_payload(ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")]))
    assert "presence_penalty" not in plain and "dry_multiplier" not in plain


# ---------------------------------------------------------------------- recovery


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


def parts(reply: dict, kind: str) -> str:
    return "".join(p.get("text") or "" for p in reply["parts"] if p["type"] == kind)


async def test_looping_thoughts_are_stopped_and_the_turn_retried_without_thinking(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.reasoning = LOOP
    fake.reply = "Move the files up a level with rsync, then fix the UUID."
    conv, sent = await start(client, auth_headers, "How do I move it up a level?")
    await drain_runs(sessionmaker, gateway, settings)

    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.completed
    assert fake.reasoning_sent < len(LOOP) / 3  # generation was cut off, not run out
    first, retry = fake.requests
    assert "chat_template_kwargs" not in first
    assert retry["chat_template_kwargs"] == {"enable_thinking": False}
    assert retry["messages"][-1]["content"].endswith(LOOP_NUDGE)

    reply = (await detail(client, auth_headers, conv["id"]))["messages"][-1]
    assert parts(reply, "text") == fake.reply
    thinking = parts(reply, "reasoning")
    assert thinking.startswith(INTRO.strip()) and "going round in circles" in thinking
    assert len(thinking) < 3000  # the loop itself isn't kept

    # The nudge was for that one turn only.
    fake.reasoning = None
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": "next-1", "content": "Thanks"},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    assert all(LOOP_NUDGE not in str(m.get("content")) for m in fake.requests[-1]["messages"])


async def test_a_model_that_loops_again_ends_with_a_note(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.reasoning = LOOP
    fake.reply = CYCLE * 30  # without thinking, it loops in the answer instead
    conv, sent = await start(client, auth_headers)
    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.completed
    assert len(fake.requests) == 2
    text = parts((await detail(client, auth_headers, conv["id"]))["messages"][-1], "text")
    assert "kept repeating itself" in text
    assert text.count("Then userdata") < 6


async def test_looping_answer_is_cut_without_a_retry(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.reply = "Here is the plan.\n\n" + CYCLE * 40
    conv, sent = await start(client, auth_headers)
    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.completed
    assert len(fake.requests) == 1
    text = parts((await detail(client, auth_headers, conv["id"]))["messages"][-1], "text")
    assert text.startswith("Here is the plan.") and "kept repeating itself" in text
    assert len(text) < 4000


async def test_thinking_too_long_is_capped(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    settings.model_max_reasoning_chars = 3000
    fake.reasoning = docs()[:50_000]  # long, but not repeating
    fake.reply = "The answer."
    conv, _ = await start(client, auth_headers)
    await drain_runs(sessionmaker, gateway, settings)
    assert fake.reasoning_sent <= 3200
    reply = (await detail(client, auth_headers, conv["id"]))["messages"][-1]
    assert parts(reply, "text") == "The answer."
    assert fake.requests[-1]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_guard_off_and_sampling_settings(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    settings.model_loop_guard = False
    settings.model_presence_penalty = 1.5
    settings.model_dry_multiplier = 0.8
    fake.reasoning = INTRO + CYCLE * 10
    await start(client, auth_headers)
    await drain_runs(sessionmaker, gateway, settings)
    assert fake.reasoning_sent == len(fake.reasoning)  # not stopped
    [request] = fake.requests
    assert request["presence_penalty"] == 1.5 and request["dry_multiplier"] == 0.8
