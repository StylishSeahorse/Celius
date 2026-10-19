from fastapi import APIRouter

from app.api.v1 import auth, conversations, health, messages, models, projects, runs, sync

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(models.router)
router.include_router(projects.router)
router.include_router(conversations.router)
router.include_router(messages.router)
router.include_router(runs.router)
router.include_router(sync.router)
