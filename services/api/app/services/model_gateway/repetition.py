"""Spotting a model that is stuck repeating itself, while it streams (ADR C-15).

Local models, above all while "thinking", can fall into a cycle: the same few sentences
("Okay. Let's write. One detail: …") over and over until the reply times out. The guard
is fed the stream and trips on any of:

- **Repeated sentences:** almost every one of the last sentences is one already written
  at least twice (only sentences long enough to mean something count; "Okay." is
  ignored; a draft and its final copy don't count).
- **A verbatim cycle:** the tail is the same block of text several times in a row
  (catches loops without sentence breaks).
- **A length cap** (thinking only): more than `max_chars`, looping or not.

Pure and cheap: segments are hashed as they complete, and the cycle check runs every
few hundred characters on the tail only.
"""

import re
from collections import deque
from dataclasses import dataclass, field

# Sentence or line ends; what's between is a segment.
_SPLIT = re.compile(r"(?<=[.!?:;])\s+|\n+")
_WORDS = re.compile(r"[\W_]+")


@dataclass
class GuardConfig:
    window: int  # segments looked at
    share: float  # share of them that must be repeats
    min_chars: int  # shorter segments are ignored
    max_chars: int = 0  # 0: no length cap
    cycle_every: int = 400
    cycle_tail: int = 4000
    cycle_repeats: int = 4
    cycle_min_unit: int = 24
    cycle_min_total: int = 1200


REASONING = GuardConfig(window=40, share=0.9, min_chars=8)
# Answers can legitimately repeat short lines (code, tables): stricter.
ANSWER = GuardConfig(window=60, share=0.95, min_chars=20, cycle_repeats=5)


@dataclass
class RepetitionGuard:
    config: GuardConfig
    tripped: str | None = None  # "repeating" | "too_long"
    # Where the repetition started (a character offset into what was fed).
    loop_start: int | None = None
    _text: list[str] = field(default_factory=list)
    _length: int = 0
    _partial: str = ""
    _partial_start: int = 0
    _seen: dict[int, int] = field(default_factory=dict)
    _window: deque = field(default_factory=deque)  # (was_repeat, start offset)
    _since_cycle_check: int = 0

    def feed(self, chunk: str) -> bool:
        """Add streamed text; True once the guard has tripped."""
        if self.tripped or not chunk:
            return bool(self.tripped)
        self._text.append(chunk)
        self._length += len(chunk)
        self._since_cycle_check += len(chunk)
        if self.config.max_chars and self._length > self.config.max_chars:
            self.tripped, self.loop_start = "too_long", self._length
            return True
        self._segments(chunk)
        if not self.tripped and self._since_cycle_check >= self.config.cycle_every:
            self._since_cycle_check = 0
            self._cycle()
        return bool(self.tripped)

    def text(self) -> str:
        return "".join(self._text)

    # ------------------------------------------------------------------ checks

    def _segments(self, chunk: str) -> None:
        buffer = self._partial + chunk
        base = self._partial_start
        last = 0
        for match in _SPLIT.finditer(buffer):
            self._add(buffer[last : match.start()], base + last)
            last = match.end()
            if self.tripped:
                break
        # The rest may still be growing.
        self._partial = buffer[last:]
        self._partial_start = base + last

    def _add(self, segment: str, start: int) -> None:
        normal = _WORDS.sub(" ", segment.lower()).strip()
        if len(normal) < self.config.min_chars:
            return
        key = hash(normal)
        # A third or later time: writing something twice (a draft, then the final
        # version) is normal; a loop says it again and again.
        repeat = self._seen.get(key, 0) >= 2
        self._seen[key] = self._seen.get(key, 0) + 1
        self._window.append((repeat, start))
        if len(self._window) > self.config.window:
            self._window.popleft()
        if len(self._window) == self.config.window:
            repeats = sum(1 for r, _ in self._window if r)
            if repeats >= self.config.share * self.config.window:
                self.tripped = "repeating"
                self.loop_start = self._window[0][1]

    def _cycle(self) -> None:
        cfg = self.config
        tail = self.text()[-cfg.cycle_tail :]
        longest = len(tail) // cfg.cycle_repeats
        for unit in range(cfg.cycle_min_unit, longest + 1):
            if unit * cfg.cycle_repeats < cfg.cycle_min_total:
                continue
            block = tail[-unit:]
            if all(
                tail.startswith(block, len(tail) - unit * (i + 1))
                for i in range(1, cfg.cycle_repeats)
            ):
                self.tripped = "repeating"
                self.loop_start = self._length - unit * cfg.cycle_repeats
                return
