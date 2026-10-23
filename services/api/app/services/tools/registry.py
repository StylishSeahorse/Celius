"""Tool definitions shared by the built-in tools and MCP tools."""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.db import SessionFactory
from app.services.tools.netguard import Resolver, system_resolver
from app.services.tools.policy import Risk


@dataclass
class ToolContext:
    settings: Settings
    factory: SessionFactory
    owner_id: uuid.UUID
    conversation_id: uuid.UUID
    run_id: uuid.UUID
    message_id: uuid.UUID
    workspace: Path
    sandbox: Any = None  # SandboxClient | None
    resolver: Resolver = system_resolver
    web_transport: Any = None  # tests only: replaces the guarded transport
    # Seconds this call may take (per-call timeout, capped by what is left of the run's budget).
    time_limit: float = 120


@dataclass
class ToolOutput:
    content: str  # what the model gets back
    data: dict[str, Any] = field(default_factory=dict)  # detail for the UI
    is_error: bool = False


class ToolArgumentError(ValueError):
    pass


Handler = Callable[[ToolContext, Any], Awaitable[ToolOutput]]


@dataclass
class ToolSpec:
    name: str
    description: str
    risk: Risk
    handler: Handler
    args_model: type[BaseModel] | None = None  # built-in tools validate with pydantic
    schema: dict[str, Any] | None = None  # MCP tools bring their own JSON Schema
    needs_sandbox: bool = False
    needs_network: bool = False
    label: str = ""
    # Why the tool can't be offered on this server (None: it can), e.g. a missing API key.
    unavailable_on: Callable[[Settings], str | None] | None = None

    def parameters(self) -> dict[str, Any]:
        if self.schema is not None:
            return self.schema
        assert self.args_model is not None
        return _strip_titles(self.args_model.model_json_schema())

    def openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters(),
            },
        }

    def parse(self, raw: str | dict | None) -> dict[str, Any]:
        """Decode and validate arguments. The returned dict is what approval is bound to."""
        if isinstance(raw, dict):
            arguments = raw
        else:
            try:
                arguments = json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise ToolArgumentError(f"Arguments are not valid JSON: {exc.msg}") from exc
        if not isinstance(arguments, dict):
            raise ToolArgumentError("Arguments must be a JSON object")
        if self.args_model is None:
            return arguments
        try:
            return self.args_model.model_validate(arguments).model_dump(exclude_none=True)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
                for e in exc.errors()[:5]
            )
            raise ToolArgumentError(f"Invalid arguments: {problems}") from exc

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
        value = self.args_model.model_validate(arguments) if self.args_model else arguments
        return await self.handler(context, value)


def _strip_titles(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


BUILTIN: dict[str, ToolSpec] = {}


def builtin(spec: ToolSpec) -> ToolSpec:
    BUILTIN[spec.name] = spec
    return spec
