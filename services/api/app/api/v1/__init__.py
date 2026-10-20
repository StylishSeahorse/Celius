from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    automation,
    conversations,
    exports,
    files,
    health,
    memory,
    messages,
    models,
    projects,
    push,
    runs,
    sync,
    tools,
)

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(models.router)
router.include_router(projects.router)
router.include_router(conversations.router)
router.include_router(messages.router)
router.include_router(files.router)
router.include_router(exports.router)
router.include_router(runs.router)
router.include_router(sync.router)
router.include_router(tools.router)
router.include_router(automation.router)
router.include_router(memory.router)
router.include_router(push.router)
router.include_router(admin.router)
