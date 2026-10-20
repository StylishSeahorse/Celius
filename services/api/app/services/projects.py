import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import Conversation, Project, ProjectMember, User
from app.schemas.projects import MemberOut, ProjectCreate, ProjectOut, ProjectUpdate
from app.services.access import accessible_projects
from app.services.memory.store import forget_project
from app.services.sync import record_sync


def project_out(
    project: Project,
    conversation_count: int = 0,
    *,
    role: str = "owner",
    owner: str | None = None,
    shared: bool = False,
) -> ProjectOut:
    return ProjectOut(
        role=role,
        owner=owner,
        shared=shared or role != "owner",
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
    db: AsyncSession, user_id: uuid.UUID, *, archived: bool = False
) -> list[ProjectOut]:
    """Your projects and the ones shared with you. Counts are of your own chats."""
    counts = (
        select(Conversation.project_id, func.count().label("n"))
        .where(Conversation.owner_id == user_id, Conversation.deleted_at.is_(None))
        .group_by(Conversation.project_id)
        .subquery()
    )
    roles = (
        select(ProjectMember.project_id, ProjectMember.role)
        .where(ProjectMember.user_id == user_id)
        .subquery()
    )
    shared = select(ProjectMember.project_id).distinct().subquery()
    rows = (
        await db.execute(
            select(
                Project,
                func.coalesce(counts.c.n, 0),
                roles.c.role,
                User.username,
                shared.c.project_id,
            )
            .join(User, User.id == Project.owner_id)
            .outerjoin(counts, counts.c.project_id == Project.id)
            .outerjoin(roles, roles.c.project_id == Project.id)
            .outerjoin(shared, shared.c.project_id == Project.id)
            .where(
                Project.id.in_(accessible_projects(user_id)),
                Project.archived_at.is_not(None) if archived else Project.archived_at.is_(None),
            )
            .order_by(func.lower(Project.name))
        )
    ).all()
    return [
        project_out(
            project,
            count,
            role="owner" if project.owner_id == user_id else role,
            owner=None if project.owner_id == user_id else owner,
            shared=shared_id is not None,
        )
        for project, count, role, owner, shared_id in rows
    ]


async def audience(db: AsyncSession, project: Project) -> list[uuid.UUID]:
    """Everyone who sees the project: its owner and members."""
    members = (
        await db.scalars(
            select(ProjectMember.user_id).where(ProjectMember.project_id == project.id)
        )
    ).all()
    return [project.owner_id, *members]


def _notify(db: AsyncSession, people: list[uuid.UUID], project: Project, **kwargs) -> None:
    for person in people:
        record_sync(db, person, "project", project.id, **kwargs)


async def list_members(db: AsyncSession, project: Project) -> list[MemberOut]:
    rows = (
        await db.execute(
            select(User.username, User.display_name, ProjectMember.role)
            .join(ProjectMember, ProjectMember.user_id == User.id)
            .where(ProjectMember.project_id == project.id)
            .order_by(User.username)
        )
    ).all()
    return [MemberOut(username=u, display_name=d, role=r) for u, d, r in rows]


async def set_member(db: AsyncSession, project: Project, username: str, role: str) -> MemberOut:
    user = await db.scalar(select(User).where(User.username == username, User.is_active))
    if user is None:
        raise not_found("User")
    if user.id == project.owner_id:
        raise AppError(409, "is_owner", "That's the project's owner")
    member = await db.get(ProjectMember, (project.id, user.id))
    if member is None:
        db.add(ProjectMember(project_id=project.id, user_id=user.id, role=role))
    else:
        member.role = role
    _notify(db, [*await audience(db, project), user.id], project)
    return MemberOut(username=user.username, display_name=user.display_name, role=role)


async def remove_member(db: AsyncSession, project: Project, user_id: uuid.UUID) -> None:
    """Stop sharing with someone. Their chats in it are kept, unfiled."""
    member = await db.get(ProjectMember, (project.id, user_id))
    if member is None:
        raise not_found("Member")
    people = await audience(db, project)
    await db.delete(member)
    chats = (
        await db.scalars(
            select(Conversation).where(
                Conversation.project_id == project.id, Conversation.owner_id == user_id
            )
        )
    ).all()
    for conversation in chats:
        conversation.project_id = None
        conversation.version += 1
        record_sync(
            db, user_id, "conversation", conversation.id, payload={"version": conversation.version}
        )
    _notify(db, [p for p in people if p != user_id], project)
    record_sync(db, user_id, "project", project.id, operation="delete")


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
    _notify(db, await audience(db, project), project)
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
    for conversation in moved:  # the owner's and the members' chats
        conversation.project_id = None
        conversation.version += 1
        record_sync(
            db,
            conversation.owner_id,
            "conversation",
            conversation.id,
            payload={"version": conversation.version},
        )
    _notify(db, await audience(db, project), project, operation="delete")
    await db.delete(project)
