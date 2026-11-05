"""Secrets never become memories (ADR-C04).

`find_secret` is used before anything is stored as a memory (from the user, the model or
extraction) and `redact` before an exchange is indexed for recall. Detection is
deliberately broad: a false positive costs one unremembered sentence, a false negative
leaks a credential into every future prompt.
"""

import math
import re
from collections import Counter

REDACTED = "[redacted secret]"

_CRED = (
    r"(?:password|passwd|passphrase|pwd|secret|api[_ -]?key|access[_ -]?token|"
    r"auth[_ -]?token|client[_ -]?secret|token)"
)
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("API key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{16,}")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("AWS key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{20,}")),
    (
        "credential",
        re.compile(
            rf"(?i)\b{_CRED}\b\s*[=:]\s*['\"]?[^\s'\"]{{4,}}",
        ),
    ),
    (
        # "my password is hunter2!" (the value must look like one: a digit or symbol)
        "credential",
        re.compile(rf"(?i)\b{_CRED}\s+is\s+['\"]?(?=\S*[\d!@#$%^&*])[^\s'\"]{{4,}}"),
    ),
    ("connection string", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]{3,}@")),
]
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")


def _entropy(token: str) -> float:
    counts = Counter(token)
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def _random_looking(token: str) -> bool:
    """A long token mixing letters and digits with high entropy (hashes, keys)."""
    if token.startswith(("http", "www")) or "/" in token.strip("/") and token.count("/") > 2:
        return False
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    return has_digit and has_alpha and _entropy(token) >= 4.0


def find_secret(text: str) -> str | None:
    """The kind of secret found in `text`, or None."""
    for kind, pattern in _PATTERNS:
        if pattern.search(text):
            return kind
    for token in _LONG_TOKEN.findall(text):
        if _random_looking(token):
            return "random-looking token"
    return None


def redact(text: str) -> str:
    for _, pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _LONG_TOKEN.sub(lambda m: REDACTED if _random_looking(m.group(0)) else m.group(0), text)
