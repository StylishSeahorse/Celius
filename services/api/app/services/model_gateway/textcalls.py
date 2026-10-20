"""Tool calls a model wrote as text instead of returning them as structured calls.

llama-server turns template-specific markup into `tool_calls` only when its parser
recognises it. Some models or templates slip (e.g. Qwen3-Coder after a long reasoning
block), and the call arrives as plain text. These formats are recognised:

    <tool_call>{"name": "x", "arguments": {...}}</tool_call>             Hermes / Qwen 2.5
    <tool_call><function=x><parameter=p>v</parameter></function></tool_call>   Qwen3-Coder
    [TOOL_CALLS][{"name": "x", "arguments": {...}}]                        Mistral

Only names of offered tools are accepted, so ordinary prose is never mistaken for a call.
"""

import json
import re
from dataclasses import dataclass

_HERMES = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_XML_FUNCTION = re.compile(
    r"(?:<tool_call>\s*)?<function=([\w.\-]+)>(.*?)(?:</function>|$)\s*(?:</tool_call>)?",
    re.DOTALL,
)
_XML_PARAMETER = re.compile(
    r"<parameter=([\w.\-]+)>\s*(.*?)\s*(?:</parameter>|(?=<parameter=)|$)", re.DOTALL
)
_MISTRAL = re.compile(r"\[TOOL_CALLS\]\s*(\[.*\])", re.DOTALL)


@dataclass
class TextCall:
    name: str
    arguments: str  # JSON


def _value(raw: str):
    text = raw.strip()
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text  # numbers and booleans are coerced by the tool's argument model


def extract(text: str, offered: set[str]) -> tuple[list[TextCall], str]:
    """Calls found in `text` (for offered tools only) and the text without them."""
    calls: list[TextCall] = []
    spans: list[tuple[int, int]] = []

    for match in _HERMES.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        name = data.get("name") if isinstance(data, dict) else None
        if name in offered:
            arguments = data.get("arguments", data.get("parameters", {}))
            if isinstance(arguments, str):
                arguments = _value(arguments) if arguments.strip() else {}
            calls.append(
                TextCall(name, json.dumps(arguments if isinstance(arguments, dict) else {}))
            )
            spans.append(match.span())

    if not calls:
        for match in _XML_FUNCTION.finditer(text):
            name = match.group(1)
            if name not in offered:
                continue
            arguments = {
                p.group(1): _value(p.group(2)) for p in _XML_PARAMETER.finditer(match.group(2))
            }
            calls.append(TextCall(name, json.dumps(arguments)))
            spans.append(match.span())

    if not calls:
        match = _MISTRAL.search(text)
        if match:
            try:
                items = json.loads(match.group(1))
            except json.JSONDecodeError:
                items = []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get("name") in offered:
                    arguments = item.get("arguments", {})
                    calls.append(
                        TextCall(
                            item["name"],
                            json.dumps(arguments if isinstance(arguments, dict) else {}),
                        )
                    )
            if calls:
                spans.append(match.span())

    if not calls:
        return [], text
    remaining, last = [], 0
    for start, end in sorted(spans):
        remaining.append(text[last:start])
        last = end
    remaining.append(text[last:])
    cleaned = re.sub(r"\n{3,}", "\n\n", "".join(remaining)).strip()
    # A dangling wrapper left behind (e.g. "<tool_call>" with nothing parsed after it).
    cleaned = re.sub(r"</?tool_call>\s*$", "", cleaned).strip()
    return calls, cleaned
