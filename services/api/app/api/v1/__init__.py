from fastapi import APIRouter

from app.api.v1 import (
    admin,
    agent_tasks,
    assistant,
    auth,
    automation,
    conversations,
    exports,
    files,
    health,
    integrations,
    memory,
    messages,
    models,
    projects,
    push,
    runs,
    skills,
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
router.include_router(skills.router)
router.include_router(agent_tasks.router)
router.include_router(assistant.router)
router.include_router(integrations.router)
router.include_router(push.router)
router.include_router(admin.router)
