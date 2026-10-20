import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.db import get_db
from app.errors import not_found
from app.models import User
from app.schemas.projects import MemberIn, MemberOut, ProjectCreate, ProjectOut, ProjectUpdate
from app.services import projects as svc
from app.services.access import require_project

router = APIRouter(prefix="/projects", tags=["projects"])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=list[ProjectOut])
async def list_projects(auth: CurrentAuth, db: DB, archived: bool = False) -> list[ProjectOut]:
    return await svc.list_projects(db, auth.user.id, archived=archived)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate, response: Response, auth: CurrentAuth, db: DB
) -> ProjectOut:
    project, created = await svc.create_project(db, auth.user.id, body)
    await db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return svc.project_out(project)


async def _out(db, auth, project, role) -> ProjectOut:
    shared = role != "owner" or bool(await svc.list_members(db, project))
    owner = (
        None
        if role == "owner"
        else await db.scalar(select(User.username).where(User.id == project.owner_id))
    )
    return svc.project_out(project, role=role, owner=owner, shared=shared)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: uuid.UUID, auth: CurrentAuth, db: DB) -> ProjectOut:
    project, role = await require_project(db, auth.user.id, project_id)
    return await _out(db, auth, project, role)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID, body: ProjectUpdate, auth: CurrentAuth, db: DB
) -> ProjectOut:
    # Members with write access change the content; memory and archiving are the owner's.
    owner_only = body.memory_enabled is not None or body.archived is not None
    project, role = await require_project(
        db, auth.user.id, project_id, "owner" if owner_only else "write"
    )
    await svc.update_project(db, project, body)
    await db.commit()
    return await _out(db, auth, project, role)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    project, _ = await require_project(db, auth.user.id, project_id, "owner")
    await svc.delete_project(db, project)
    await db.commit()


@router.get("/{project_id}/members", response_model=list[MemberOut])
async def list_members(project_id: uuid.UUID, auth: CurrentAuth, db: DB) -> list[MemberOut]:
    project, _ = await require_project(db, auth.user.id, project_id)
    return await svc.list_members(db, project)


@router.put("/{project_id}/members/{username}", response_model=MemberOut)
async def share(
    project_id: uuid.UUID, username: str, body: MemberIn, auth: CurrentAuth, db: DB
) -> MemberOut:
    """Share the project with someone (read or write), or change their access."""
    project, _ = await require_project(db, auth.user.id, project_id, "owner")
    member = await svc.set_member(db, project, username, body.role)
    await db.commit()
    return member


@router.delete("/{project_id}/members/{username}", status_code=status.HTTP_204_NO_CONTENT)
async def unshare(project_id: uuid.UUID, username: str, auth: CurrentAuth, db: DB) -> None:
    """The owner stops sharing with someone; a member can leave."""
    leaving = username == auth.user.username
    project, _ = await require_project(db, auth.user.id, project_id, "read" if leaving else "owner")
    user_id = await db.scalar(select(User.id).where(User.username == username))
    if user_id is None:
        raise not_found("Member")
    await svc.remove_member(db, project, user_id)
    await db.commit()
