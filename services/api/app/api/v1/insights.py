"""Insights: how Celius performs, for you or (administrators) everyone (ADR C-12)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.db import get_db
from app.errors import AppError
from app.services import analytics

router = APIRouter(tags=["insights"])
DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("/insights")
async def insights(
    auth: CurrentAuth,
    db: DB,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    scope: Literal["me", "everyone"] = "me",
) -> dict:
    if scope == "everyone" and not auth.user.is_admin:
        raise AppError(403, "admin_only", "Only administrators can see everyone's figures")
    return await analytics.compute(db, None if scope == "everyone" else auth.user.id, days)
