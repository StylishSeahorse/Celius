"""The system prompt, assembled from modules (ADR C-09).

Each module is a named section. Trusted instructions (identity, the operating principles of
the model's profile, the project's instructions written by its owner, tool and security
rules) are kept apart from context that is data (project files, the task record, memories,
the skill index): those sections say so themselves and are wrapped in tags.

Sections are rendered in a fixed order and measured, so a run records how much of the
context each one used (`run.started.prompt_sections`), the basis for tuning profiles.
"""

from dataclasses import dataclass, field

from app.services.context import estimate_tokens

# Render order. Instructions first, then context (data), then the tool rules last, where
# small models attend most.
ORDER = (
    "identity",
    "principles",
    "project_instructions",
    "project_files",
    "data_note",
    "task",
    "memory",
    "skills",
    "assistant",
    "tools",
)
TRUSTED = {"identity", "principles", "project_instructions", "data_note", "assistant", "tools"}

PRINCIPLES = {
    "lite": (
        "Work in small steps. Use at most one tool per turn, read its result, then decide "
        "the next step. Keep replies short and plain. After changing something, check that "
        "it worked. If something is unclear or keeps failing, ask the user."
    ),
    "standard": "",
    "advanced": (
        "For complex work, understand the situation first, then plan, act, check each "
        "result and adjust. Independent tool calls may go together in one turn; dependent "
        "ones in order. Before reporting, verify the outcome and say what you could not "
        "check."
    ),
}


@dataclass
class PromptBuilder:
    sections: dict[str, str] = field(default_factory=dict)

    def set(self, name: str, text: str | None) -> None:
        assert name in ORDER, name
        text = (text or "").strip()
        if text:
            self.sections[name] = text
        else:
            self.sections.pop(name, None)

    def add(self, name: str, text: str | None) -> None:
        """Append to a section (e.g. notes to the tool rules)."""
        text = (text or "").strip()
        if text:
            current = self.sections.get(name)
            self.sections[name] = f"{current} {text}" if current else text

    def render(self) -> str:
        return "\n\n".join(self.sections[name] for name in ORDER if name in self.sections)

    def sizes(self) -> dict[str, int]:
        """Estimated tokens per section (for the run record and evaluation)."""
        return {
            name: estimate_tokens(self.sections[name]) for name in ORDER if name in self.sections
        }
