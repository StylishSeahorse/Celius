"""Skill validation: format, limits, file layout, secrets and unsafe content (ADR C-06).

Skill files are untrusted input whoever wrote them: the model may have been steered by a
web page, and an imported archive may come from anywhere. Validation blocks what must
never be stored or followed (secrets, instructions to override the system or its
permissions) and warns about what deserves a human look (destructive or piped-to-shell
commands; they can only run in the sandbox, through the normal tool policy, anyway).

A skill can never grant permissions: `allowed-tools` in the frontmatter is advisory, and
Celius ignores it for policy decisions.
"""

import re
from dataclasses import dataclass, field

from app.services.files.paths import UnsafePath, safe_relative_path
from app.services.memory.sensitive import find_secret
from app.services.skills.format import (
    BODY_LINES_ADVISED,
    MAX_COMPATIBILITY,
    MAX_DESCRIPTION,
    MAX_FILE_CHARS,
    MAX_FILES,
    MAX_NAME,
    MAX_SKILL_MD_CHARS,
    MAX_TOTAL_CHARS,
    NAME_RE,
    SKILL_FILE,
    SUPPORT_DIRS,
    SkillFormatError,
    parse,
)

KNOWN_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}

# Text that tries to change who decides: never acceptable in a skill.
_INJECTION = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (?:all |any )?(?:previous|prior|above|earlier) (?:instructions|rules)",
        r"disregard (?:the |your )?(?:system|previous|prior) (?:prompt|instructions)",
        r"(?:reveal|print|show|leak) (?:the |your )?system prompt",
        r"you (?:are|have been) (?:now )?(?:granted|given) (?:admin|administrator|root|"
        r"full|unrestricted)",
        r"(?:approve|grant|allow) (?:all |every )?(?:tool calls?|tools|permissions?) "
        r"(?:automatically|without asking)",
        r"(?:switch|change|set) (?:the )?(?:tool )?profile to (?:administrator|developer)",
        r"(?:disable|bypass|turn off|skip) (?:the )?(?:sandbox|approvals?|permission checks?)",
        r"exfiltrat",
        r"send (?:the |all )?(?:secrets|credentials|tokens|api keys|passwords)",
    )
]
# Worth a human look; allowed (they run only in the sandbox, under the tool policy).
_RISKY = [
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+/(?:\s|$)"), "deletes from the filesystem root"),
    (
        re.compile(r"(?:curl|wget)[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b"),
        "pipes a download into a shell",
    ),
    (
        re.compile(r"base64\s+(?:-d|--decode)[^\n|]*\|\s*(?:ba)?sh\b"),
        "runs decoded data as a script",
    ),
    (re.compile(r"\bchmod\s+(?:-R\s+)?777\b"), "makes files writable by everyone"),
    (re.compile(r"\bsudo\b"), "uses sudo (the sandbox has no root)"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "contains a fork bomb"),
]


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def validate(files: dict[str, str], *, expected_name: str | None = None) -> Validation:
    result = Validation()
    if not isinstance(files, dict) or not files:
        result.errors.append("A skill needs files, starting with SKILL.md")
        return result
    if len(files) > MAX_FILES:
        result.errors.append(f"At most {MAX_FILES} files per skill")
    total = 0
    for path, content in files.items():
        if not isinstance(content, str):
            result.errors.append(f"{path}: only text files are supported")
            continue
        total += len(content)
        if path != SKILL_FILE:
            _check_path(path, result)
        limit = MAX_SKILL_MD_CHARS if path == SKILL_FILE else MAX_FILE_CHARS
        if len(content) > limit:
            result.errors.append(f"{path} is longer than {limit:,} characters")
        if kind := find_secret(content):
            result.errors.append(f"{path} appears to contain a secret ({kind}); remove it")
        for pattern in _INJECTION:
            if match := pattern.search(content):
                result.errors.append(
                    f"{path} tries to override instructions or permissions (“{match.group(0)}”)"
                )
        for pattern, why in _RISKY:
            if pattern.search(content):
                result.warnings.append(f"{path} {why}")
    if total > MAX_TOTAL_CHARS:
        result.errors.append(f"The skill is larger than {MAX_TOTAL_CHARS:,} characters in total")

    try:
        parsed = parse(files)
    except SkillFormatError as exc:
        result.errors.append(str(exc))
        return result
    name = parsed.name
    if not name:
        result.errors.append("The frontmatter needs a name")
    elif len(name) > MAX_NAME or not NAME_RE.match(name):
        result.errors.append(
            "The name must be 1-64 lowercase letters, digits and single hyphens "
            "(not at the start or end), e.g. deploy-docker-app"
        )
    if expected_name and name and name != expected_name:
        result.errors.append(f"The name must stay {expected_name!r} (it identifies the skill)")
    if not parsed.description:
        result.errors.append("The frontmatter needs a description: what it does and when to use it")
    elif len(parsed.description) > MAX_DESCRIPTION:
        result.errors.append(f"The description is longer than {MAX_DESCRIPTION} characters")
    elif len(parsed.description) < 20:
        result.warnings.append("The description is very short; say what it does and when")
    compatibility = parsed.frontmatter.get("compatibility")
    if compatibility is not None and len(str(compatibility)) > MAX_COMPATIBILITY:
        result.errors.append(f"compatibility is longer than {MAX_COMPATIBILITY} characters")
    metadata = parsed.frontmatter.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        result.errors.append("metadata must be a mapping")
    if "allowed-tools" in parsed.frontmatter:
        result.warnings.append(
            "allowed-tools is advisory only: Celius's tool policy decides what may run"
        )
    unknown = set(parsed.frontmatter) - KNOWN_KEYS
    if unknown:
        result.warnings.append(f"Unknown frontmatter keys ignored: {', '.join(sorted(unknown))}")
    if not parsed.body:
        result.errors.append("SKILL.md needs instructions after the frontmatter")
    elif parsed.body.count("\n") + 1 > BODY_LINES_ADVISED:
        result.warnings.append(
            f"SKILL.md is over {BODY_LINES_ADVISED} lines; move detail into references/"
        )
    for path in files:
        if path != SKILL_FILE and path.split("/", 1)[0] in ("scripts", "references", "assets"):
            if path not in parsed.body:
                result.warnings.append(f"{path} is not mentioned in SKILL.md")
    test = parsed.test_command
    if test and parsed.test_command and len(test) > 500:
        result.errors.append("metadata.test is longer than 500 characters")
    if not test:
        result.warnings.append("No metadata.test: the skill can't be tested automatically")
    return result


def _check_path(path: str, result: Validation) -> None:
    try:
        clean = safe_relative_path(path)
    except UnsafePath as exc:
        result.errors.append(f"{path}: {exc}")
        return
    if clean != path:
        result.errors.append(f"{path}: use a plain relative path ({clean})")
        return
    if "/" not in path or path.split("/", 1)[0] not in SUPPORT_DIRS:
        result.errors.append(
            f"{path}: supporting files go in {', '.join(d + '/' for d in SUPPORT_DIRS)}"
        )
