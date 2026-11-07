"""Prompt evaluation: run scenarios through the real Celius stack (model registry, prompt
builder, executor, tools) against a model server, once per profile, and score them.

    uv run python -m devtools.prompt_eval --base-url http://gpu-pc:8080/v1 --model qwen3
    uv run python -m devtools.prompt_eval --base-url http://127.0.0.1:18080/v1   # the fake

Each scenario is a user message and what a good reply does: call a given tool (or none),
and/or contain some text. The report lists, per profile, which scenarios passed, the
prompt size (estimated tokens, by section), tool calls and time. Nothing is written to
your Celius database: everything runs in a temporary SQLite file and workspace.

This measures; it doesn't decide. Use it to pick a model's profile in Account > Models,
and before changing prompt modules (docs/implementation/ARCHITECTURE_DECISIONS.md C-09).
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

os.environ.setdefault("APP_ENV", "test")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import app.models  # noqa: E402, F401
from app.config import Settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models import (  # noqa: E402
    Message,
    ModelCatalogEntry,
    Run,
    RunEvent,
    ToolCall,
    User,
)
from app.schemas.conversations import ConversationCreate, MessageSubmit  # noqa: E402
from app.services.chat import submit_message  # noqa: E402
from app.services.conversations import create_conversation, load_parts, message_text  # noqa: E402
from app.services.model_gateway import OpenAICompatibleGateway  # noqa: E402
from app.worker.executor import claim_next_run, execute_run  # noqa: E402


@dataclass
class Scenario:
    name: str
    message: str
    expect_tool: str | None = None  # a tool that must be called; "" = no tool at all
    expect_text: list[str] = field(default_factory=list)  # all must appear (any case)
    tool_profile: str = "read_only"


SCENARIOS = [
    Scenario("plain answer", "What is the capital of France? One word.", "", ["paris"]),
    Scenario("pick a tool", "List the files in my workspace.", "list_files"),
    Scenario(
        "remember",
        "Please remember that I prefer metric units.",
        "remember",
    ),
    Scenario(
        "plan a multi-step task",
        "Create notes.txt containing 'hello', then read it back to check it. Plan the steps first.",
        "plan_start",
        tool_profile="developer",
    ),
    Scenario(
        "write then verify",
        "Write 'ok' to check.txt and confirm the file contains it.",
        "write_file",
        tool_profile="developer",
    ),
]


@dataclass
class Result:
    scenario: str
    profile: str
    passed: bool
    why: str
    prompt_tokens: int | None
    sections: dict
    tools_offered: int
    tool_calls: list[str]
    seconds: float


async def run_one(
    scenario: Scenario,
    profile: str,
    base_url: str,
    model: str,
    *,
    transport=None,
    root: Path,
) -> Result:
    db_path = root / f"eval-{uuid.uuid4().hex}.db"
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{db_path}",
        storage_root=str(root / "storage"),
        workspace_root=str(root / "workspaces"),
        model_base_url=base_url,
        model_default_id=model,
        model_max_retries=1,
        run_delta_flush_ms=50,
        memory_require_review=True,
        skill_autogen_enabled=False,
    )
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    gateway = OpenAICompatibleGateway.from_settings(settings, transport=transport)
    started = time.monotonic()
    try:
        async with factory() as db:
            user = User(username="eval", password_hash="-", is_admin=True)
            db.add(user)
            await db.flush()
            info = await gateway.find_model(model)
            db.add(
                ModelCatalogEntry(
                    provider=gateway.provider,
                    model_id=info.id if info else model,
                    capabilities={},
                    overrides={},
                    limits={},
                    profile=profile,
                    is_available=True,
                )
            )
            conversation, _ = await create_conversation(
                db, user.id, ConversationCreate(tool_profile=scenario.tool_profile)
            )
            await db.commit()
            await submit_message(
                db,
                user,
                conversation.id,
                MessageSubmit(client_message_id=uuid.uuid4().hex, content=scenario.message),
                settings,
            )
        while (run_id := await claim_next_run(factory, "eval", settings)) is not None:
            await execute_run(
                run_id, factory=factory, gateway=gateway, settings=settings, worker_id="eval"
            )
        async with factory() as db:
            run = await db.scalar(select(Run).order_by(Run.created_at.desc()).limit(1))
            calls = (
                await db.scalars(
                    select(ToolCall.tool_name)
                    .where(ToolCall.run_id == run.id)
                    .order_by(ToolCall.step, ToolCall.position)
                )
            ).all()
            started_event = await db.scalar(
                select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "run.started")
            )
            data = started_event.data if started_event else {}
            reply = ""
            if run.output_message_id:
                message = await db.get(Message, run.output_message_id)
                parts = await load_parts(db, [message])
                reply = message_text(parts[message.id])
    finally:
        await gateway.aclose()
        await engine.dispose()
        db_path.unlink(missing_ok=True)

    why = []
    if run.status.value != "completed":
        why.append(f"run {run.status.value}: {run.error_code}")
    if scenario.expect_tool == "" and calls:
        why.append(f"called {', '.join(calls)} but no tool was needed")
    elif scenario.expect_tool and scenario.expect_tool not in calls:
        why.append(f"didn't call {scenario.expect_tool} (called: {', '.join(calls) or 'none'})")
    missing = [t for t in scenario.expect_text if t.lower() not in reply.lower()]
    if missing:
        why.append(f"reply lacks {missing}")
    return Result(
        scenario=scenario.name,
        profile=profile,
        passed=not why,
        why="; ".join(why) or "ok",
        prompt_tokens=data.get("estimated_prompt_tokens"),
        sections=data.get("prompt_sections") or {},
        tools_offered=len(data.get("tools") or []),
        tool_calls=list(calls),
        seconds=round(time.monotonic() - started, 1),
    )


async def evaluate(
    base_url: str,
    model: str,
    profiles: list[str],
    scenarios: list[Scenario] = SCENARIOS,
    *,
    transport=None,
) -> list[Result]:
    results = []
    with tempfile.TemporaryDirectory(prefix="celius-eval-") as tmp:
        for profile in profiles:
            for scenario in scenarios:
                results.append(
                    await run_one(
                        scenario, profile, base_url, model, transport=transport, root=Path(tmp)
                    )
                )
    return results


def summary(results: list[Result]) -> dict[str, dict]:
    """Per profile: pass rate, prompt tokens, tool calls and time (for --save/--compare)."""
    out: dict[str, dict] = {}
    for profile in dict.fromkeys(r.profile for r in results):
        mine = [r for r in results if r.profile == profile]
        tokens = [r.prompt_tokens for r in mine if r.prompt_tokens]
        out[profile] = {
            "pass_rate": round(sum(r.passed for r in mine) / len(mine), 3),
            "prompt_tokens_mean": round(sum(tokens) / len(tokens)) if tokens else None,
            "tool_calls_mean": round(sum(len(r.tool_calls) for r in mine) / len(mine), 2),
            "seconds_mean": round(sum(r.seconds for r in mine) / len(mine), 2),
        }
    return out


def _delta(now: dict, before: dict, key: str) -> str:
    value, old = now.get(key), before.get(key)
    if value is None:
        return "-"
    return f"{value}" if old is None else f"{value} ({value - old:+.3g})"


def compare(current: dict[str, dict], previous: dict[str, dict]) -> str:
    lines = [
        "| profile | pass rate | prompt tokens | tool calls | seconds |",
        "| --- | --- | --- | --- | --- |",
    ]
    for profile, now in current.items():
        before = previous.get(profile, {})
        cells = " | ".join(
            _delta(now, before, key)
            for key in ("pass_rate", "prompt_tokens_mean", "tool_calls_mean", "seconds_mean")
        )
        lines.append(f"| {profile} | {cells} |")
    return "\n".join(lines)


def report(results: list[Result]) -> str:
    lines = [
        "| profile | scenario | result | prompt tokens | tools offered | tool calls | s |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r.profile} | {r.scenario} | {'pass' if r.passed else 'FAIL: ' + r.why} | "
            f"{r.prompt_tokens} | {r.tools_offered} | {', '.join(r.tool_calls) or '-'} | "
            f"{r.seconds} |"
        )
    lines.append("")
    for profile in dict.fromkeys(r.profile for r in results):
        mine = [r for r in results if r.profile == profile]
        passed = sum(r.passed for r in mine)
        lines.append(f"{profile}: {passed}/{len(mine)} passed")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible /v1 URL")
    parser.add_argument("--model", default="default")
    parser.add_argument("--profiles", default="lite,standard,advanced")
    parser.add_argument("--json", action="store_true", help="Print results as JSON")
    parser.add_argument("--save", help="Write a summary per profile to this JSON file")
    parser.add_argument("--compare", help="Show changes against a saved summary")
    args = parser.parse_args()
    results = asyncio.run(evaluate(args.base_url, args.model, args.profiles.split(",")))
    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        print(report(results))
    totals = summary(results)
    if args.compare:
        with open(args.compare) as handle:
            print("\n" + compare(totals, json.load(handle)))
    if args.save:
        with open(args.save, "w") as handle:
            json.dump(totals, handle, indent=2)
    sys.exit(0 if all(r.passed for r in results) else 1)


if __name__ == "__main__":
    main()
