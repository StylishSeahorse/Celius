"""The skill registry: lifecycle, trust policy, search and usage (ADR C-06).

Lifecycle:  RECOGNISE → EXTRACT → GENERATE → VALIDATE → TEST → APPROVE → PUBLISH → IMPROVE

- Every change is a new version; a published version is never edited in place. Publishing
  retires the previous one, which stays available for rollback.
- What the model may do is decided here by the user's trust level, never by the model:
    conservative  model-created skills wait for the user; nothing is generated
                  automatically after runs
    balanced      (default) generated and validated automatically; tested when they have
                  a test; publishing always waits for the user's approval
    autonomous    the model may publish a version that passed validation *and* its test;
                  untested versions still wait for approval
- Only published skills are offered to the model (search, the prompt's skill index,
  skill_read). Candidates are visible to it only to update, validate or test them.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.config import Settings
from app.db.base import utcnow
from app.errors import AppError
from app.models import (
    Run,
    RunStatus,
    Skill,
    SkillEvent,
    SkillVersion,
    ToolCall,
    ToolCallStatus,
    UserSettings,
)
from app.services.audit import audit
from app.services.memory.embeddings import Embedder, cosine
from app.services.memory.retrieval import keyword_score, terms
from app.services.skills.format import SkillFormatError, content_hash, parse
from app.services.skills.validate import validate
from app.services.sync import record_sync

Trust = Literal["conservative", "balanced", "autonomous"]
TRUST_LEVELS: tuple[Trust, ...] = ("conservative", "balanced", "autonomous")
Actor = Literal["user", "model", "generated"]


@dataclass
class SkillPrefs:
    trust: Trust
    auto_generate: bool


def skill_prefs(settings: Settings, row: UserSettings | None) -> SkillPrefs:
    saved = ((row.preferences if row else None) or {}).get("skills") or {}
    trust = saved.get("trust", settings.skill_trust_default)
    return SkillPrefs(
        trust=trust if trust in TRUST_LEVELS else "balanced",
        auto_generate=saved.get("auto_generate", settings.skill_autogen_enabled),
    )


async def user_skill_prefs(db: AsyncSession, owner_id: uuid.UUID, settings: Settings) -> SkillPrefs:
    return skill_prefs(settings, await db.get(UserSettings, owner_id))


# ---------------------------------------------------------------------- lookup


async def get_skill(
    db: AsyncSession, owner_id: uuid.UUID, name_or_id: str | uuid.UUID
) -> Skill | None:
    if isinstance(name_or_id, uuid.UUID):
        return await db.scalar(
            select(Skill).where(Skill.id == name_or_id, Skill.owner_id == owner_id)
        )
    return await db.scalar(
        select(Skill).where(Skill.owner_id == owner_id, Skill.name == name_or_id.strip().lower())
    )


async def get_version(
    db: AsyncSession, skill: Skill, version: int | None = None
) -> SkillVersion | None:
    """A version by number; else the published one; else the latest."""
    number = version or skill.published_version or skill.latest_version
    return await db.scalar(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill.id, SkillVersion.version == number
        )
    )


def event(
    db: AsyncSession,
    skill: Skill,
    kind: str,
    outcome: str,
    *,
    version: int | None = None,
    run_id: uuid.UUID | None = None,
    duration_ms: int | None = None,
    **detail,
) -> SkillEvent:
    row = SkillEvent(
        id=uuid.uuid4(),
        skill_id=skill.id,
        version=version,
        kind=kind,
        outcome=outcome,
        run_id=run_id,
        duration_ms=duration_ms,
        detail=detail,
        created_at=utcnow(),
    )
    db.add(row)
    return row


# ---------------------------------------------------------------------- versions


async def create_skill(
    db: AsyncSession,
    owner_id: uuid.UUID,
    files: dict[str, str],
    *,
    origin: str,
    created_by: Actor,
    changelog: str | None = None,
    run_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
) -> tuple[Skill, SkillVersion]:
    try:
        parsed = parse(files)
    except SkillFormatError as exc:
        raise AppError(422, "invalid_skill", str(exc)) from exc
    name = parsed.name.strip().lower()
    report = validate(files)
    if not name or any("name must be" in e for e in report.errors):
        raise AppError(422, "invalid_skill", "; ".join(report.errors) or "The skill needs a name")
    if await get_skill(db, owner_id, name):
        raise AppError(409, "skill_exists", f"A skill named {name!r} already exists; update it")
    skill = Skill(
        id=uuid.uuid4(),
        owner_id=owner_id,
        name=name,
        description=parsed.description[:1024] or name,
        status="candidate",
        origin=origin,
        tags=parsed.tags,
        latest_version=0,
    )
    db.add(skill)
    await db.flush()
    version = await add_version(
        db,
        skill,
        files,
        created_by=created_by,
        changelog=changelog or "First version",
        run_id=run_id,
        conversation_id=conversation_id,
    )
    audit(db, owner_id, "skill.created", "skill", skill.id, origin=origin, name=name)
    return skill, version


async def add_version(
    db: AsyncSession,
    skill: Skill,
    files: dict[str, str],
    *,
    created_by: Actor,
    changelog: str | None = None,
    run_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
) -> SkillVersion:
    """A new candidate version, validated at once. An identical copy of an existing
    version is refused (nothing would change)."""
    digest = content_hash(files)
    same = await db.scalar(
        select(SkillVersion.version).where(
            SkillVersion.skill_id == skill.id, SkillVersion.content_hash == digest
        )
    )
    if same is not None:
        raise AppError(409, "unchanged", f"Version {same} already has exactly this content")
    report = validate(files, expected_name=skill.name)
    try:
        frontmatter = parse(files).frontmatter
    except SkillFormatError:
        frontmatter = {}
    skill.latest_version += 1
    version = SkillVersion(
        id=uuid.uuid4(),
        skill_id=skill.id,
        version=skill.latest_version,
        status="validated" if report.ok else "candidate",
        files=dict(files),
        frontmatter=_jsonable(frontmatter),
        content_hash=digest,
        changelog=(changelog or "").strip()[:2000] or None,
        created_by=created_by,
        source_run_id=run_id,
        source_conversation_id=conversation_id,
        validation=report.as_dict(),
        test={"status": "untested" if _test_command(files) else "not_defined"},
    )
    db.add(version)
    await db.flush()
    event(
        db,
        skill,
        "validation",
        "passed" if report.ok else "failed",
        version=version.version,
        errors=report.errors,
        warnings=report.warnings,
    )
    record_sync(db, skill.owner_id, "skill", skill.id)
    return version


def _test_command(files: dict[str, str]) -> str | None:
    try:
        return parse(files).test_command
    except SkillFormatError:
        return None


def _jsonable(value):
    """YAML can produce dates and other types JSON columns refuse."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


async def revalidate(db: AsyncSession, skill: Skill, version: SkillVersion) -> dict:
    report = validate(version.files, expected_name=skill.name)
    version.validation = report.as_dict()
    if version.status in ("candidate", "validated"):
        version.status = "validated" if report.ok else "candidate"
    event(
        db,
        skill,
        "validation",
        "passed" if report.ok else "failed",
        version=version.version,
        errors=report.errors,
        warnings=report.warnings,
    )
    return version.validation


def record_test(
    db: AsyncSession, skill: Skill, version: SkillVersion, passed: bool, output: str, ms: int
) -> None:
    version.test = {
        "status": "passed" if passed else "failed",
        "output": output[-4000:],
        "at": utcnow().isoformat(),
    }
    event(
        db,
        skill,
        "test",
        "passed" if passed else "failed",
        version=version.version,
        duration_ms=ms,
        output=output[-1000:],
    )


# ---------------------------------------------------------------------- publishing


async def request_publish(
    db: AsyncSession,
    skill: Skill,
    version: SkillVersion,
    *,
    actor: Actor,
    settings: Settings,
    embedder: Embedder | None = None,
) -> str:
    """Publish if the actor may, under the user's trust level. Returns "published",
    "pending" (waiting for the user's approval) or raises AppError when it can't be."""
    if version.status in ("published",):
        return "published"
    if version.status in ("rejected", "retired") and actor != "user":
        raise AppError(409, "not_publishable", f"Version {version.version} is {version.status}")
    if not (version.validation or {}).get("ok"):
        raise AppError(
            422,
            "validation_failed",
            "Fix the validation errors first: " + "; ".join(version.validation.get("errors", [])),
        )
    if actor == "user":
        await publish(db, skill, version, embedder=embedder, by="user")
        return "published"
    prefs = await user_skill_prefs(db, skill.owner_id, settings)
    tested = (version.test or {}).get("status") == "passed"
    if prefs.trust == "autonomous" and tested:
        await publish(db, skill, version, embedder=embedder, by=actor)
        return "published"
    version.status = "pending"
    event(
        db,
        skill,
        "lifecycle",
        "pending",
        version=version.version,
        action="publish_requested",
        by=actor,
        reason="approval required"
        if prefs.trust != "autonomous"
        else "untested versions need approval",
    )
    audit(db, skill.owner_id, "skill.publish_requested", "skill", skill.id, version=version.version)
    record_sync(db, skill.owner_id, "skill", skill.id)
    return "pending"


async def publish(
    db: AsyncSession,
    skill: Skill,
    version: SkillVersion,
    *,
    embedder: Embedder | None = None,
    by: str = "user",
) -> None:
    await db.execute(
        update(SkillVersion)
        .where(SkillVersion.skill_id == skill.id, SkillVersion.status == "published")
        .values(status="retired")
    )
    version.status = "published"
    version.published_at = utcnow()
    parsed = parse(version.files)
    skill.published_version = version.version
    skill.status = "active"
    skill.description = parsed.description[:1024] or skill.description
    skill.tags = parsed.tags
    skill.embedding, skill.embedding_model = None, None
    if embedder is not None:
        vectors = await embedder.embed([_search_text(skill)])
        if vectors:
            skill.embedding, skill.embedding_model = vectors[0], embedder.model
    event(db, skill, "lifecycle", "info", version=version.version, action="published", by=by)
    audit(db, skill.owner_id, "skill.published", "skill", skill.id, version=version.version, by=by)
    record_sync(db, skill.owner_id, "skill", skill.id)


async def reject_version(db: AsyncSession, skill: Skill, version: SkillVersion) -> None:
    if version.status == "published":
        raise AppError(409, "published", "Deprecate the skill or publish another version instead")
    version.status = "rejected"
    event(db, skill, "lifecycle", "info", version=version.version, action="rejected", by="user")
    audit(db, skill.owner_id, "skill.version_rejected", "skill", skill.id, version=version.version)
    record_sync(db, skill.owner_id, "skill", skill.id)


async def deprecate(
    db: AsyncSession, skill: Skill, *, actor: Actor, settings: Settings, reason: str = ""
) -> str:
    """A user deprecates at once; the model only under autonomous trust, else it is
    recorded as a proposal for the user."""
    if actor != "user":
        prefs = await user_skill_prefs(db, skill.owner_id, settings)
        if prefs.trust != "autonomous":
            event(db, skill, "lifecycle", "pending", action="deprecation_proposed", reason=reason)
            return "pending"
    skill.status = "deprecated"
    event(db, skill, "lifecycle", "info", action="deprecated", by=actor, reason=reason)
    audit(db, skill.owner_id, "skill.deprecated", "skill", skill.id, by=actor)
    record_sync(db, skill.owner_id, "skill", skill.id)
    return "deprecated"


async def restore(db: AsyncSession, skill: Skill) -> None:
    skill.status = "active" if skill.published_version else "candidate"
    event(db, skill, "lifecycle", "info", action="restored", by="user")
    record_sync(db, skill.owner_id, "skill", skill.id)


# ---------------------------------------------------------------------- search


def _search_text(skill: Skill) -> str:
    return f"{skill.name.replace('-', ' ')}: {skill.description} {' '.join(skill.tags or [])}"


async def search(
    db: AsyncSession,
    owner_id: uuid.UUID,
    query: str,
    *,
    embedder: Embedder | None = None,
    limit: int = 8,
    statuses: tuple[str, ...] = ("active",),
    min_similarity: float = 0.45,
) -> list[tuple[float, Skill]]:
    """Skills ranked by words in name/description/tags and, with embeddings, meaning.
    Skills are few per user, so this scores them all in Python."""
    rows = (
        await db.scalars(
            select(Skill)
            .options(undefer(Skill.embedding))
            .where(Skill.owner_id == owner_id, Skill.status.in_(statuses))
        )
    ).all()
    if not rows:
        return []
    query_terms = terms(query)
    vector = None
    if embedder is not None and query.strip():
        vectors = await embedder.embed([query[:2000]])
        vector = vectors[0] if vectors else None
    scored: list[tuple[float, Skill]] = []
    for skill in rows:
        text = _search_text(skill)
        keyword = keyword_score(query_terms, text.lower().replace("-", " "))
        semantic = None
        if (
            vector is not None
            and skill.embedding is not None
            and (skill.embedding_model == (embedder.model if embedder else None))
        ):
            semantic = cosine(vector, list(skill.embedding))
        if keyword <= 0 and (semantic is None or semantic < min_similarity):
            continue
        score = keyword if semantic is None else 0.6 * semantic + 0.4 * keyword
        # Proven skills first among equals: a little credit for successful uses.
        score *= 1 + min(skill.success_count, 20) / 100
        scored.append((round(score, 4), skill))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:limit]


# ---------------------------------------------------------------------- usage


async def record_use(
    db: AsyncSession, skill: Skill, version: int | None, run_id: uuid.UUID | None
) -> None:
    """The model loaded the skill in a run; the outcome is settled when the run ends."""
    if run_id is not None and await db.scalar(
        select(SkillEvent.id).where(
            SkillEvent.skill_id == skill.id, SkillEvent.run_id == run_id, SkillEvent.kind == "use"
        )
    ):
        return
    skill.use_count += 1
    skill.last_used_at = utcnow()
    event(db, skill, "use", "pending", version=version, run_id=run_id)


async def settle_uses(db: AsyncSession, run: Run) -> list[uuid.UUID]:
    """At the end of a run: each skill used in it succeeded if the run completed and no
    tool call failed after the skill was loaded; otherwise it failed. Returns the skills
    whose use failed (candidates for an improvement)."""
    uses = (
        await db.scalars(
            select(SkillEvent).where(
                SkillEvent.run_id == run.id,
                SkillEvent.kind == "use",
                SkillEvent.outcome == "pending",
            )
        )
    ).all()
    for use in uses:
        failed_calls = await db.scalar(
            select(func.count())
            .select_from(ToolCall)
            .where(
                ToolCall.run_id == run.id,
                (ToolCall.status == ToolCallStatus.failed) | ToolCall.is_error.is_(True),
                ToolCall.created_at >= use.created_at - timedelta(seconds=1),
            )
        )
        success = run.status == RunStatus.completed and not failed_calls
        use.outcome = "success" if success else "failure"
        started = use.created_at if use.created_at.tzinfo else use.created_at.replace(tzinfo=UTC)
        use.duration_ms = int((utcnow() - started).total_seconds() * 1000)
        use.detail = {**(use.detail or {}), "failed_tool_calls": failed_calls or 0}
        column = Skill.success_count if success else Skill.failure_count
        await db.execute(
            update(Skill).where(Skill.id == use.skill_id).values({column.key: column + 1})
        )
    return [use.skill_id for use in uses if use.outcome == "failure"]


async def feedback(db: AsyncSession, skill: Skill, outcome: str, note: str) -> None:
    """The user's verdict on a use ("it worked", "it didn't", "I had to correct it")."""
    event(db, skill, "feedback", outcome, version=skill.published_version, note=note[:2000])
    if outcome == "success":
        skill.success_count += 1
    elif outcome in ("failure", "corrected"):
        skill.failure_count += 1
    record_sync(db, skill.owner_id, "skill", skill.id)


IMPROVE_WINDOW = 10  # look at the last this many outcomes
IMPROVE_FAILURES = 3  # at least this many of them failed
IMPROVE_EVERY = timedelta(days=7)


async def needs_improvement(db: AsyncSession, skill: Skill) -> bool:
    """A failure cluster: IMPROVE_FAILURES of the last IMPROVE_WINDOW outcomes (uses and
    the user's feedback) failed, and no improvement was asked for in the last week."""
    if skill.status != "active":
        return False
    outcomes = (
        await db.scalars(
            select(SkillEvent.outcome)
            .where(
                SkillEvent.skill_id == skill.id,
                SkillEvent.kind.in_(["use", "feedback"]),
                SkillEvent.outcome.in_(["success", "failure", "corrected"]),
            )
            .order_by(SkillEvent.created_at.desc())
            .limit(IMPROVE_WINDOW)
        )
    ).all()
    if sum(o in ("failure", "corrected") for o in outcomes) < IMPROVE_FAILURES:
        return False
    recent = await db.scalar(
        select(SkillEvent.id).where(
            SkillEvent.skill_id == skill.id,
            SkillEvent.kind == "lifecycle",
            SkillEvent.created_at >= utcnow() - IMPROVE_EVERY,
            SkillEvent.detail["action"].as_string() == "improvement_requested",
        )
    )
    return recent is None
