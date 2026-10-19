import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.db import get_db
from app.schemas.projects import ProjectCreate, ProjectOut, ProjectUpdate
from app.services import projects as svc

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


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: uuid.UUID, auth: CurrentAuth, db: DB) -> ProjectOut:
    return svc.project_out(await svc.get_owned_project(db, auth.user.id, project_id))


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID, body: ProjectUpdate, auth: CurrentAuth, db: DB
) -> ProjectOut:
    project = await svc.get_owned_project(db, auth.user.id, project_id)
    await svc.update_project(db, project, body)
    await db.commit()
    return svc.project_out(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    project = await svc.get_owned_project(db, auth.user.id, project_id)
    await svc.delete_project(db, project)
    await db.commit()
