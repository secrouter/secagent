"""Models for generated tests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class GeneratedTest:
    kind: str          # unit | functional
    target: str        # source file (unit) or component name (functional)
    language: str
    framework: str
    path: str          # output path of the generated test, repo-relative
    ok: bool = True    # False if the model returned nothing usable
    # How much of the source the model actually saw. When `saw_chars < total_chars` the
    # tests cover only the visible part — and anything the generator "knows" about the
    # rest is invention. Click's core.py is 147KB against a 24KB cap, so Command, Group,
    # Option, Argument and Parameter all sat past the cut.
    saw_chars: int = 0
    total_chars: int = 0
    # Why `ok` is False. A file that could not be read and a file the model returned
    # nothing usable for both looked like plain "failed" with no way to tell which —
    # or, before this field existed, the unreadable case didn't even reach here; it was
    # dropped from `results` before a reason could be attached at all.
    error: str = ""
    # True when `framework` is the configured default with no repo evidence behind it
    # (no gtest/pytest/cargo/xUnit marker found for this language) — i.e. ASSUMED, not
    # observed. A wrong framework silently assumed is how a whole generated suite
    # becomes unrunnable, so this must be visible per-test in the manifest, not just as
    # a run-level footnote.
    framework_assumed: bool = False

    @property
    def partial(self) -> bool:
        """Truncated **and** actually produced. `ok` is not optional here.

        This checked only `saw_chars < total_chars`. A file whose generation TIMED OUT
        after a truncated read satisfies that and produced no test at all — so Click's
        `core.py`, which yielded nothing, was listed in the disclosure banner, the README
        and the manifest under "Incomplete source coverage", as though a partial but usable
        test existed. There was no test.

        A tool whose selling point is honest coverage reporting cannot describe a
        nonexistent artifact as partial. Failure is `failed`; partial means "here it is,
        and it saw only some of the source".
        """
        return (self.ok
                and bool(self.total_chars)
                and self.saw_chars < self.total_chars)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
