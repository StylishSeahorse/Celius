"""Who may do what with a project and its files (A-04).

A project belongs to its owner and can be shared with other users as `read` (use its
instructions and files in their own chats) or `write` (also change instructions and
files). Chats, memories, tools and everything else stay private to each person.
"""

import uuid
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, not_found
from app.models import Project, ProjectMember, StoredFile

Role = Literal["owner", "write", "read"]
RANK = {"read": 1, "write": 2, "owner": 3}


async def project_role(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[Project, Role] | None:
    project = await db.get(Project, project_id)
    if project is None:
        return None
    if project.owner_id == user_id:
        return project, "owner"
    role = await db.scalar(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )
    return (project, role) if role in ("read", "write") else None


async def require_project(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID, at_least: Role = "read"
) -> tuple[Project, Role]:
    """The project and the user's role, or 404 (no access) / 403 (not enough)."""
    found = await project_role(db, user_id, project_id)
    if found is None:
        raise not_found("Project")
    if RANK[found[1]] < RANK[at_least]:
        message = (
            "Only the project's owner can do that"
            if at_least == "owner"
            else "This project is shared with you read-only"
        )
        raise AppError(403, "project_read_only", message)
    return found


def accessible_projects(user_id: uuid.UUID):
    """A SELECT of the ids of projects the user owns or is a member of."""
    member_of = select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
    return select(Project.id).where(or_(Project.owner_id == user_id, Project.id.in_(member_of)))


async def readable_file(db: AsyncSession, user_id: uuid.UUID, file_id: uuid.UUID) -> StoredFile:
    """Your own files, and the files of projects shared with you."""
    file = await db.scalar(
        select(StoredFile).where(
            StoredFile.id == file_id,
            StoredFile.deleted_at.is_(None),
            or_(
                StoredFile.owner_id == user_id,
                StoredFile.project_id.in_(accessible_projects(user_id)),
            ),
        )
    )
    if file is None:
        raise not_found("File")
    return file


async def deletable_file(db: AsyncSession, user_id: uuid.UUID, file_id: uuid.UUID) -> StoredFile:
    """Your own files; in a shared project, also others' files if you may write to it."""
    file = await readable_file(db, user_id, file_id)
    if file.owner_id != user_id:
        if file.project_id is None:
            raise not_found("File")
        await require_project(db, user_id, file.project_id, "write")
    return file
