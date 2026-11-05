"""The Agent Skills format (https://agentskills.io): a directory with SKILL.md (YAML
frontmatter + Markdown instructions) and optional supporting files.

    my-skill/
      SKILL.md            name, description (required); license, compatibility,
                          metadata, allowed-tools (optional); instructions in the body
      scripts/            code the instructions run
      references/         documentation loaded only when needed
      assets/             templates and data
      examples/           worked examples (Celius extension)
      tests/              files the test command uses (Celius extension)

Celius reads `metadata.test` as the command that tests the skill in the sandbox, run from
the skill's directory (e.g. `bash tests/run.sh`). A skill without one is never "proven":
its test status stays `not_defined`.

Files are stored as text in the database (one version = one JSON object of path ->
content), so a skill is backed up, restored and exported with everything else.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

import yaml

SKILL_FILE = "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
MAX_SKILL_MD_CHARS = 100_000
MAX_FILE_CHARS = 200_000
MAX_TOTAL_CHARS = 1_000_000
MAX_FILES = 40
BODY_LINES_ADVISED = 500
SUPPORT_DIRS = ("scripts", "references", "assets", "examples", "tests")
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.DOTALL)


class SkillFormatError(ValueError):
    pass


@dataclass
class ParsedSkill:
    frontmatter: dict
    body: str
    files: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return str(self.frontmatter.get("name") or "")

    @property
    def description(self) -> str:
        return " ".join(str(self.frontmatter.get("description") or "").split())

    @property
    def metadata(self) -> dict:
        value = self.frontmatter.get("metadata")
        return value if isinstance(value, dict) else {}

    @property
    def test_command(self) -> str | None:
        value = self.metadata.get("test")
        return str(value).strip() if isinstance(value, str | int | float) and value else None

    @property
    def tags(self) -> list[str]:
        value = self.metadata.get("tags") or self.frontmatter.get("tags")
        if isinstance(value, str):
            value = [t.strip() for t in value.split(",")]
        return [str(t)[:40] for t in value or [] if str(t).strip()][:12]


def split_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER.match(text.lstrip("﻿"))
    if not match:
        raise SkillFormatError("SKILL.md must start with YAML frontmatter between --- lines")
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillFormatError(f"The frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillFormatError("The frontmatter must be a mapping of keys to values")
    return data, match.group(2).strip()


def parse(files: dict[str, str]) -> ParsedSkill:
    if SKILL_FILE not in files:
        raise SkillFormatError("A skill needs a SKILL.md file")
    frontmatter, body = split_frontmatter(files[SKILL_FILE])
    return ParsedSkill(frontmatter=frontmatter, body=body, files=dict(files))


def render(frontmatter: dict, body: str) -> str:
    head = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{head}\n---\n\n{body.strip()}\n"


def content_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def supporting_files(files: dict[str, str]) -> list[str]:
    return sorted(path for path in files if path != SKILL_FILE)


def extract_skill_md(text: str) -> str | None:
    """A SKILL.md inside a model's answer (bare, or in a ``` fence)."""
    fenced = re.search(r"```(?:markdown|md|yaml)?\s*\n(---\s*\n.*?)\n```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip() + "\n"
    start = text.find("---")
    if start == -1:
        return None
    candidate = text[start:].strip()
    return candidate + "\n" if _FRONTMATTER.match(candidate) else None
