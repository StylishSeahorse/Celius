import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import Conversation, Project
from app.schemas.projects import ProjectCreate, ProjectOut, ProjectUpdate
from app.services.memory.store import forget_project
from app.services.sync import record_sync


def project_out(project: Project, conversation_count: int = 0) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        instructions=project.instructions,
        memory_enabled=project.memory_enabled,
        archived=project.archived_at is not None,
        conversation_count=conversation_count,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


async def get_owned_project(
    db: AsyncSession, owner_id: uuid.UUID, project_id: uuid.UUID
) -> Project:
    project = await db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_id == owner_id)
    )
    if project is None:
        raise not_found("Project")
    return project


async def list_projects(
    db: AsyncSession, owner_id: uuid.UUID, *, archived: bool = False
) -> list[ProjectOut]:
    counts = (
        select(Conversation.project_id, func.count().label("n"))
        .where(Conversation.owner_id == owner_id, Conversation.deleted_at.is_(None))
        .group_by(Conversation.project_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Project, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.project_id == Project.id)
            .where(
                Project.owner_id == owner_id,
                Project.archived_at.is_not(None) if archived else Project.archived_at.is_(None),
            )
            .order_by(func.lower(Project.name))
        )
    ).all()
    return [project_out(project, count) for project, count in rows]


async def create_project(
    db: AsyncSession, owner_id: uuid.UUID, data: ProjectCreate
) -> tuple[Project, bool]:
    if data.id is not None:
        existing = await db.get(Project, data.id)
        if existing is not None:
            if existing.owner_id != owner_id:
                raise AppError(409, "id_conflict", "That project id is not available")
            return existing, False
    project = Project(
        id=data.id or uuid.uuid4(),
        owner_id=owner_id,
        name=data.name.strip(),
        description=data.description,
        instructions=data.instructions,
        memory_enabled=data.memory_enabled,
    )
    db.add(project)
    await db.flush()
    record_sync(db, owner_id, "project", project.id)
    return project, True


async def update_project(db: AsyncSession, project: Project, data: ProjectUpdate) -> Project:
    fields = data.model_dump(exclude_unset=True, exclude={"archived"})
    if fields.get("memory_enabled") is None:
        fields.pop("memory_enabled", None)
    elif project.memory_enabled and not fields["memory_enabled"]:
        await forget_project(db, project)  # its chats stop being recalled at once
    for key, value in fields.items():
        setattr(project, key, value.strip() if key == "name" and value else value)
    if data.archived is not None:
        project.archived_at = utcnow() if data.archived else None
    project.updated_at = utcnow()
    record_sync(db, project.owner_id, "project", project.id)
    return project


async def delete_project(db: AsyncSession, project: Project) -> None:
    """Delete the project. Its conversations are kept and become unfiled (FK SET NULL)."""
    moved = (
        await db.scalars(
            select(Conversation).where(
                Conversation.project_id == project.id, Conversation.deleted_at.is_(None)
            )
        )
    ).all()
    for conversation in moved:
        conversation.project_id = None
        conversation.version += 1
        record_sync(
            db,
            project.owner_id,
            "conversation",
            conversation.id,
            payload={"version": conversation.version},
        )
    record_sync(db, project.owner_id, "project", project.id, operation="delete")
    await db.delete(project)
