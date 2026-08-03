"""Break the implementation on purpose, and require the test to notice.

A test that passes against deliberately broken code is not testing that code. This is the
only gate that catches the vacuous shape — assertions sealed inside a branch that never
executes — because such a test compiles, runs and passes exactly like a real one.

Everything here is pure text manipulation over source, so it is testable without executing
anything. Applying a mutant and judging it lives in `harness.py`.

PER-LANGUAGE OPERATORS ARE EXPECTED, NOT A DESIGN FAILURE. `&&` is `and` in Python; C has
no `elif`. The table below is keyed by language for that reason. What must generalise is
the *shape* — flip a comparison, flip a connective, perturb a constant, short-circuit a
function — not the spelling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Mutant:
    """One deliberate break: a file, a line, and what was done to it."""

    path: str
    line: int          # 1-based, into the ORIGINAL file
    operator: str      # e.g. "comparison-flip"
    before: str
    after: str

    @property
    def label(self) -> str:
        return f"{self.path}:{self.line} {self.operator} ({self.before.strip()[:60]})"


# Swaps applied to a single line. Each entry is (pattern, replacement) and is applied to
# the FIRST match only, so one mutant changes exactly one thing — a mutant that changes
# two is not attributable when it survives.
_COMPARISON = [
    (r"==", "!="), (r"!=", "=="),
    (r"<=", ">"), (r">=", "<"),
    (r"(?<![<=>!])<(?![<=])", ">="), (r"(?<![<=>!])>(?![>=])", "<="),
]
_C_FAMILY = _COMPARISON + [(r"&&", "||"), (r"\|\|", "&&")]
# `->` is an annotation arrow, not a comparison. Excluded explicitly because the generic
# `>` rule turned `def f() -> str:` into `-<= str:`, a syntax error dressed as a mutant.
_PYTHON = [
    (r"==", "!="), (r"!=", "=="),
    (r"<=", ">"), (r">=", "<"),
    (r"(?<![<=>!-])<(?![<=])", ">="), (r"(?<![<=>!-])>(?![>=])", "<="),
    # `is None` / `is not None` is one of the commonest Python branches, and leaving it
    # out made whole functions un-mutatable. `read_config(ctx, param, value)`, whose only
    # branch is `if value is None`, produced ZERO mutants — so a genuinely good test of it
    # and a vacuous one were both scored `vacuous`, indistinguishable. `is not` is handled
    # by the `not`-drop below; this covers the bare `is`.
    (r"\bis\b(?!\s+not\b)", "is not"),
    (r"\band\b", "or"), (r"\bor\b", "and"), (r"\bnot\b", ""),
]

_OPERATORS: dict[str, list[tuple[str, str]]] = {
    "C": _C_FAMILY,
    "C++": _C_FAMILY,
    "Python": _PYTHON,
}

# Lines that must never be mutated: changing a preprocessor guard, an include or a comment
# either breaks the build (an invalid mutant, wasted) or changes nothing (an equivalent
# mutant, which looks like a surviving one and slanders a good test).
_SKIP_C = re.compile(r"^\s*(#|//|/\*|\*)")
_SKIP_PY = re.compile(r"^\s*(#|\"\"\"|''')")
_SKIP = {"C": _SKIP_C, "C++": _SKIP_C, "Python": _SKIP_PY}


def supported(language: str) -> bool:
    return language in _OPERATORS


def _blank_python(source: str) -> str:
    """Blank comments and string literals in Python source, keeping line/column layout.

    The line-based skip has no block state, so it caught a line that OPENS a docstring and
    nothing inside one. Measured on Click's `_utils.py` — a 36-line enum with no
    comparisons, no branches and no logic — all four generated mutants landed in prose: a
    URL inside a docstring, the `->` of a return annotation, and two more docstring lines.
    None changes behaviour, so all four survived and the harness called a file with real
    assertions `vacuous`. A false `vacuous` is the worst verdict this tool can produce:
    it is the accusation the whole design exists to make credible.

    `tokenize` rather than a regex because it is exact about triple quotes, f-strings and
    nesting, and it is stdlib.
    """
    import io
    import tokenize

    lines = source.splitlines(keepends=True)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    for tok in toks:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (r1, c1), (r2, c2) = tok.start, tok.end
        for row in range(r1, r2 + 1):
            i = row - 1
            if i >= len(lines):
                break
            line = lines[i].rstrip("\n")
            start = c1 if row == r1 else 0
            end = c2 if row == r2 else len(line)
            lines[i] = (line[:start] + " " * (end - start) + line[end:]
                        + ("\n" if lines[i].endswith("\n") else ""))
    return "".join(lines)


def _strip_strings(line: str) -> str:
    """Blank string literals so a `==` inside a message is not mutated.

    Mutating text inside a literal changes an error message, not behaviour — an
    equivalent mutant that survives and makes a perfectly good test look vacuous.
    """
    return re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', lambda m: " " * len(m.group()), line)


def mutants_for(
    path: str,
    source: str,
    language: str,
    *,
    line_range: tuple[int, int] | None = None,
    limit: int = 10,
) -> list[Mutant]:
    """Candidate mutants for ``source``, optionally restricted to a function's lines.

    ``line_range`` is inclusive and comes from the affordance index, which already knows
    every function's start line. Restricting to the code a test claims to cover keeps the
    denominator honest: a mutant in a file the test never claimed would survive for a good
    reason and drag a real test's score down.

    Deterministic, and capped — the cap is spread across the range rather than taken from
    the top, so a 500-line function is not represented entirely by its first few lines.
    """
    ops = _OPERATORS.get(language)
    if not ops:
        return []
    skip = _SKIP[language]
    scan_src = _blank_python(source) if language == "Python" else source
    scan_lines = scan_src.splitlines()
    lines = source.splitlines()
    lo, hi = line_range or (1, len(lines))

    candidates: list[Mutant] = []
    for i, raw in enumerate(lines, start=1):
        if i < lo or i > hi or skip.match(raw):
            continue
        scannable = (scan_lines[i - 1] if language == "Python" and i <= len(scan_lines)
                     else _strip_strings(raw))
        for pattern, replacement in ops:
            m = re.search(pattern, scannable)
            if not m:
                continue
            mutated = raw[: m.start()] + replacement + raw[m.end():]
            if mutated == raw:
                continue
            candidates.append(Mutant(path, i, _name_for(pattern), raw, mutated))
            break          # one mutant per line: keep every survivor attributable
    if len(candidates) <= limit:
        return candidates
    step = len(candidates) / limit
    return [candidates[int(k * step)] for k in range(limit)]


def _name_for(pattern: str) -> str:
    if pattern in ("&&", r"\|\|", r"\band\b", r"\bor\b"):
        return "connective-flip"
    if pattern == r"\bnot\b":
        return "negation-drop"
    if "is" in pattern:
        return "identity-flip"
    return "comparison-flip"


def apply_mutant(source: str, mutant: Mutant) -> str:
    """Return ``source`` with ``mutant`` applied. Raises if the line moved underneath us."""
    lines = source.splitlines(keepends=True)
    idx = mutant.line - 1
    if idx < 0 or idx >= len(lines):
        raise ValueError(f"line {mutant.line} out of range for {mutant.path}")
    ending = "\n" if lines[idx].endswith("\n") else ""
    if lines[idx].rstrip("\r\n") != mutant.before.rstrip("\r\n"):
        raise ValueError(f"{mutant.path}:{mutant.line} no longer matches the mutant")
    lines[idx] = mutant.after.rstrip("\r\n") + ending
    return "".join(lines)


_ASSERT_RE = re.compile(
    r"\b(assert|ASSERT_\w+|EXPECT_\w+|assertEqual|assertTrue|assertRaises)\b")


def count_assertions(source: str) -> int:
    """How many assertions a test contains.

    Recorded so a (deferred) repair loop cannot "fix" a failing test by deleting the
    assertion that failed — the cheapest possible repair, and the one that manufactures
    exactly the vacuous tests this harness exists to catch.
    """
    return sum(1 for line in source.splitlines()
               if _ASSERT_RE.search(_strip_strings(line)))
