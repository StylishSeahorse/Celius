"""Running a skill's test in the sandbox (the TEST step).

The skill's files are written into a workspace under `.skills/<name>/v<version>/` and its
`metadata.test` command runs there, in the same isolated sandbox as any other code, with
the normal time, memory and output limits. Exit code 0 is a pass. Nothing in a skill can
run anywhere else.
"""

import shlex
import time
from pathlib import Path

import anyio

from app.config import Settings
from app.models import Skill, SkillVersion
from app.services.skills.format import parse
from app.services.tools import workspace as ws
from app.services.tools.sandbox_client import SandboxUnavailable


def skill_dir(skill: Skill, version: SkillVersion) -> str:
    return f".skills/{skill.name}/v{version.version}"


async def write_files(root: Path, skill: Skill, version: SkillVersion) -> str:
    base = skill_dir(skill, version)
    for path, content in version.files.items():
        await anyio.to_thread.run_sync(
            lambda p=path, c=content: ws.write_bytes(root, f"{base}/{p}", c.encode())
        )
    return base


async def run_test(
    settings: Settings,
    sandbox,
    workspace_name: str,
    root: Path,
    skill: Skill,
    version: SkillVersion,
) -> tuple[bool | None, str, int]:
    """(passed, output, milliseconds). passed is None when the skill has no test."""
    command = parse(version.files).test_command
    if not command:
        return None, "This skill has no metadata.test command.", 0
    if sandbox is None:
        raise SandboxUnavailable("The sandbox is not configured")
    base = await write_files(root, skill, version)
    started = time.monotonic()
    result = await sandbox.execute(
        workspace_name,
        "shell",
        f"cd {shlex.quote(base)} && {command}",
        timeout_seconds=settings.skill_test_timeout_seconds,
        memory_mb=settings.tool_memory_mb,
        output_limit=64 * 1024,
        network=settings.tool_sandbox_network,
    )
    ms = int((time.monotonic() - started) * 1000)
    output = (result.get("stdout") or "") + (
        f"\n[stderr]\n{result['stderr']}" if result.get("stderr") else ""
    )
    reason = result.get("killed_reason")
    passed = result.get("exit_code") == 0 and not reason
    if reason:
        output += f"\n[stopped: {reason.replace('_', ' ')}]"
    return passed, output.strip(), ms
