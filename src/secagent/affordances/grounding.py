"""Check generated prose against the index that already knows the truth.

Every LLM-written artifact secagent produces — a file purpose, a docs page, a triage note,
a generated test — names things: paths and symbols. Those names are the cheapest possible
hallucination detector, because secagent has already indexed every real one. A reference
that does not resolve is not a judgement call about tone or quality; it is a fact the
model got wrong, and it can be caught without another model call.

Evaluation runs kept turning up exactly this failure: a triage note asserting "the code
explicitly checks its value" about code that never does, a docs page attributing Kafka to
a component with no broker anywhere, generated tests written from the first 16% of a file
and free to invent whatever the other 84% contained. Each was found by a human reading
the source. Each was checkable mechanically.

The output is deliberately conservative. An unresolved reference is reported as
UNVERIFIED, not as a lie: prose legitimately mentions external libraries, planned files
and illustrative names. The point is to mark the claims a reader should check, and — more
usefully — to prove the rest were checked and hold.

WHAT THIS DOES NOT DO, stated here because "grounded" reads like a stronger word than it
is. This checks that the names a piece of prose mentions EXIST. It does not check that
what the prose SAYS about them is true. It is a hallucinated-name detector, not a
hallucinated-behaviour detector, and the difference is most of the damage:

    "The `BufferSize` variable is never actually used, so the warning is irrelevant."

`BufferSize` is used on the flagged line and is the function's second parameter. Every
name resolves; the sentence is false. Measured on UC3 triage output, roughly 4 in 10
notes on the C corpus contain a specific, checkable false claim of that shape, and this
check passed all of them — it fired identically on accurate and fabricated notes, because
what it fires on is local variable names not being in a project-wide symbol index (83% of
C++ notes, for that reason alone).

So a note without an UNVERIFIED annotation has had its REFERENCES checked, not its
CLAIMS. Anywhere that distinction could be read the other way, say which one you mean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .store import AffordanceStore

# A path-like token: at least one separator and a file extension, or a bare filename with
# a source-ish extension. Deliberately narrow — a false "unverified" is noise, and noise
# is how a signal stops being read.
# Known source/config extensions. Matching "any short alphabetic suffix" made every
# dotted attribute look like a file: on Python, `click.Abort`, `ctx.color` and `obj.value`
# were all reported as paths not in the index — a checker that is ~100% false positive on
# a whole language, which is worse than no checker because it trains people to ignore it.
_FILE_SUFFIXES = frozenset({
    "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "py", "pyi", "rs", "cs", "go",
    "java", "kt", "swift", "rb", "js", "jsx", "ts", "tsx", "sh", "bash", "ps1",
    "yaml", "yml", "json", "toml", "ini", "cfg", "xml", "sql", "proto", "cmake",
    "mk", "bzl", "gradle", "csproj", "sln", "dockerfile", "drawio", "rst", "md",
})

_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.([A-Za-z][A-Za-z0-9]{0,9}))(?![\w/])"
)

# Backticked identifiers are the only symbol references we trust ourselves to extract.
# Free prose is full of ordinary words that happen to be symbol-shaped; requiring the
# author to have marked it as code keeps the check honest.
_CODE_SPAN_RE = re.compile(r"`([A-Za-z_][\w:.<>~]{2,})`")

# Extensions that are almost always prose-level mentions rather than repo files.
_IGNORED_SUFFIXES = frozenset({".com", ".org", ".net", ".io", ".dev", ".md", ".txt"})

# Generated CODE names things without backticks, so prose-shaped extraction checked
# nothing at all on a .cpp file — the artifact UC5 actually produces. A qualified call
# anchored to a type this repo defines is groundable with no guessing: if `UnicoreParser`
# is indexed, `UnicoreParser::foo(...)` either names a real member or it does not.
_QUALIFIED_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*::\s*(\w+)\s*\(")

# Unqualified member calls (`p.foo()`, `p->foo()`) cannot be grounded without type
# inference. They are COUNTED rather than judged, so the report never implies more
# assurance than it has.
_MEMBER_CALL_RE = re.compile(r"(?:\.|->)\s*(\w+)\s*\(")


@dataclass
class Grounding:
    """What a piece of generated text claims, and which claims the index confirms."""

    verified_paths: list[str] = field(default_factory=list)
    unverified_paths: list[str] = field(default_factory=list)
    verified_symbols: list[str] = field(default_factory=list)
    unverified_symbols: list[str] = field(default_factory=list)
    # References seen but not groundable (member calls on a variable whose type we do
    # not track). Reported so "ok" is never read as "everything was checked".
    unchecked: int = 0
    # Headers the source declares external via `#include <...>`. Not checked and not a
    # problem — listed so "we ignored these" is visible rather than assumed.
    external: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unverified_paths and not self.unverified_symbols

    @property
    def checked(self) -> int:
        return (len(self.verified_paths) + len(self.unverified_paths)
                + len(self.verified_symbols) + len(self.unverified_symbols))

    def to_dict(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "ok": self.ok,
            "unverified_paths": self.unverified_paths,
            "unverified_symbols": self.unverified_symbols,
            "unchecked_member_calls": self.unchecked,
            "external_includes": self.external,
        }

    def note(self) -> str:
        """One line for a human, or empty when there is nothing to flag."""
        if self.ok:
            return ""
        bits = []
        if self.unverified_paths:
            bits.append("paths not in the index: "
                        + ", ".join(sorted(self.unverified_paths)[:5]))
        if self.unverified_symbols:
            bits.append("symbols not in the index: "
                        + ", ".join(sorted(self.unverified_symbols)[:5]))
        return "UNVERIFIED — " + "; ".join(bits)


def external_includes(text: str) -> set[str]:
    """Paths the source itself declares as coming from OUTSIDE this repository.

    `#include <gtest/gtest.h>` is, by the language's own convention, a request for a
    header on the system/third-party search path. It will never be in this repo's index,
    and reporting it as an unresolved reference is noise that drowns the signal: on four
    generated C++ test files the ONLY thing that made `ok` false was `gtest/gtest.h`, so
    a wholly fabricated test importing a non-existent `GpsParser.h` and a perfectly good
    one looked **identical** to anything reading the boolean. A quoted include
    (`#include "GpsParser.h"`) makes the opposite claim — that the header is part of this
    project — and stays checked.
    """
    return set(re.findall(r'^\s*#\s*include\s*<([^>]+)>', text or "", re.MULTILINE))


def extract_references(text: str) -> tuple[set[str], set[str]]:
    """Return ``(paths, symbols)`` mentioned by ``text``, excluding declared externals."""
    external = external_includes(text)
    paths = {
        m for m, suffix in _PATH_RE.findall(text or "")
        if suffix.lower() in _FILE_SUFFIXES
        and not any(m.lower().endswith(s) for s in _IGNORED_SUFFIXES)
        and m not in external
    }
    symbols = {
        m for m in _CODE_SPAN_RE.findall(text or "")
        if "." not in m or not m.rsplit(".", 1)[-1].isalpha() or "::" in m
    }
    return paths, symbols - paths


def check(store: AffordanceStore, text: str) -> Grounding:
    """Resolve every path and backticked symbol in ``text`` against the index."""
    paths, symbols = extract_references(text)
    calls = {(c, m) for c, m in _QUALIFIED_CALL_RE.findall(text or "")}
    member_calls = _MEMBER_CALL_RE.findall(text or "")
    g = Grounding(unchecked=len(member_calls), external=sorted(external_includes(text)))
    if not paths and not symbols and not calls:
        return g

    known_paths = store.known_paths()
    # A generated page may cite `src/foo.c` or just `foo.c`; both are real references.
    tails: dict[str, str] = {}
    for p in known_paths:
        tails.setdefault(p.rsplit("/", 1)[-1], p)

    for p in sorted(paths):
        tail = p.lstrip("./")
        if tail in known_paths or any(k.endswith("/" + tail) for k in known_paths) \
                or tail.rsplit("/", 1)[-1] in tails:
            g.verified_paths.append(p)
        else:
            g.unverified_paths.append(p)

    known_symbols = store.all_symbol_names() if (symbols or calls) else set()

    for sym in sorted(symbols):
        g.verified_symbols.append(sym) if _resolves(sym, known_symbols) \
            else g.unverified_symbols.append(sym)

    # Qualified calls in generated code, anchored to a type this repo defines.
    for cls, member in sorted(calls):
        if cls not in known_symbols:
            continue           # someone else's type; nothing to check it against
        name = f"{cls}::{member}"
        if name in g.verified_symbols or name in g.unverified_symbols:
            continue
        g.verified_symbols.append(name) if _resolves(name, known_symbols) \
            else g.unverified_symbols.append(name)
    return g


def _resolves(sym: str, known: set[str]) -> bool:
    """Does this name exist in the index?

    For `Class::member`, the MEMBER must resolve. Accepting a match on either half meant
    any invented method qualified with a real class passed —
    `UnicoreParser::totallyFakeMethod` verified clean because `UnicoreParser` is real,
    which is precisely the hallucination the check exists to catch.
    """
    if sym in known:
        return True
    if "::" in sym:
        return sym.rsplit("::", 1)[-1] in known
    return False
