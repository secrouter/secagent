"""Symbol extraction.

Python is parsed precisely with the ``ast`` module; other languages use lightweight
regex heuristics. The goal is a useful index of top-level functions/classes (with
signatures) for retrieval and summaries — not a full parser for every language.
"""

from __future__ import annotations

import ast
import re

from .models import Symbol, TypeRecord

# Regex symbol patterns per language family. Each yields (kind, name) on match.
_REGEX_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "JavaScript": [
        ("function", re.compile(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")),
        # Arrow assignments: const/let/var f = (…) => … (optionally async).
        ("function", re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(")),
        # Class/object methods incl. TS access modifiers + return-type annotations. The
        # negative lookahead keeps control-flow keywords (if/for/while/…) from matching
        # the generic `name(args) {` shape.
        ("method", re.compile(
            r"^\s*(?:public\s+|private\s+|protected\s+|readonly\s+|abstract\s+|override\s+"
            r"|async\s+|static\s+|get\s+|set\s+)*"
            r"(?!(?:if|for|while|switch|catch|return|function|class|const|let|var|else|"
            r"do|new|await|yield|typeof|throw|delete|void|in|of|case|default)\b)"
            r"(\w+)\s*\([^)]*\)\s*(?::[^={]+)?\{")),
    ],
    "Go": [
        ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(")),
        ("class", re.compile(r"^\s*type\s+(\w+)\s+struct")),
    ],
    "Java": [
        ("class", re.compile(r"^\s*(?:public|private|protected)?\s*(?:final\s+)?class\s+(\w+)")),
        ("method", re.compile(
            r"^\s*(?:public|private|protected)\s+[\w<>\[\]]+\s+(\w+)\s*\(")),
    ],
    "Rust": [
        ("function", re.compile(r"^\s*(?:pub\s+)?fn\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:pub\s+)?struct\s+(\w+)")),
    ],
    "Ruby": [
        ("method", re.compile(r"^\s*def\s+(\w+)")),
        ("class", re.compile(r"^\s*class\s+(\w+)")),
    ],
    "C#": [
        # type declarations (class/interface/struct/record/enum), optionally attributed
        ("class", re.compile(
            r"^\s*(?:\[[^\]]*\]\s*)*"
            r"(?:public|internal|private|protected|sealed|static|abstract|partial|readonly|\s)*"
            r"\b(?:class|interface|struct|record|enum)\s+(\w+)")),
        # methods: require a leading modifier so control-flow keywords (if/for/while/
        # switch/using/return/lock) don't masquerade as declarations.
        ("method", re.compile(
            r"^\s*(?:\[[^\]]*\]\s*)*"
            r"(?:public|private|protected|internal|static|virtual|override|sealed|"
            r"async|extern|unsafe|partial|new|abstract)\s+"
            r"(?:[\w<>\[\],.\?]+\s+)+(\w+)\s*\(")),
    ],
}
_REGEX_PATTERNS["TypeScript"] = _REGEX_PATTERNS["JavaScript"]

# C/C++ fallback used only when libclang is unavailable (a fresh box or the FIPS image).
# Without it, C/C++ files get zero symbols — empty functions/callers/API-ref — silently.
# Conservative "recall over nothing": single-line definitions only.
_C_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Type declarations: `struct Foo {`, `typedef struct Bar_t {`, `enum E;`, and C++
    # `class Foo`, `class Foo final : public Base`. The inheritance clause matters —
    # requiring `{`/`;`/EOL right after the name missed every derived C++ class, which is
    # most of them in a real C++ codebase.
    ("class", re.compile(
        r"^\s*(?:typedef\s+)?(?:class|struct|union|enum)\s+(\w+)\b"
        r"(?:\s+final)?\s*(?:\{|;|:|$)")),
    # function definitions: `<return type> name(args)` optionally with a trailing `{`,
    # not ending in `;` (that's a prototype) and not a control-flow/return statement.
    # Handles C++ qualified names (`Foo::bar`) and trailing qualifiers (`const`/`override`).
    ("function", re.compile(
        r"^\s*(?!(?:return|if|else|for|while|switch|do|case|sizeof|typedef|struct|"
        r"union|enum|static_assert)\b)"
        r"(?:[A-Za-z_][\w\*:]*\s+)(?:[\w\*:\s]*?)"  # return type: >=1 token + whitespace
        r"((?:\w+::)*\w+)\s*\([^;{]*\)"             # (qualified) name(args)
        r"\s*(?:const|noexcept|override|final|\s)*\{?\s*$")),
]
# Header-level declarations. In C embedded/flight software the command codes, message
# structs, message IDs and event IDs ARE the wiring, and they live entirely in headers as
# `#define` and `typedef struct`. Without these, `find-symbol FM_GET_FILE_INFO_CC` answers
# "No symbols matching ..." — worded identically to "does not exist" — which cost devs in
# two separate agentic test cohorts a lot of wasted round-trips.
_C_PATTERNS += [
    # `} FM_GetFileInfoCmd_t;` — the name of an anonymous `typedef struct { ... }`.
    ("class", re.compile(r"^\s*\}\s*([A-Za-z_]\w*)\s*;")),
    # One-line typedefs: `typedef uint32 FM_Id_t;`
    ("class", re.compile(r"^\s*typedef\s+.*?\b([A-Za-z_]\w*)\s*;\s*$")),
    # Object-like/function-like macros: `#define FM_GET_FILE_INFO_CC ...`
    ("constant", re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)")),
]
_REGEX_PATTERNS["C"] = _C_PATTERNS
_REGEX_PATTERNS["C++"] = _C_PATTERNS

# Include-guard macros (`#define FM_APP_H`) are structure, not API. Filtered by name
# shape rather than by tracking the matching `#ifndef`, which the per-line scanner has no
# state for; a real constant ending in _H is vanishingly rare.
_INCLUDE_GUARD_RE = re.compile(r"_H_?$|_HPP_?$|_INCLUDED_?$", re.IGNORECASE)


def extract_symbols(rel_path: str, text: str, language: str) -> list[Symbol]:
    if language == "Python":
        return _python_symbols(rel_path, text)
    return _regex_symbols(rel_path, text, language)


def python_imports(text: str) -> list[str]:
    """Return imported module names (dotted) from Python source."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # Preserve relative-import level so the IO mapper can resolve it.
            mods.append("." * (node.level or 0) + node.module)
    return mods


_RUST_USE_RE = re.compile(r"^\s*(?:pub\s+)?use\s+([^;]+);", re.M)
_RUST_MOD_RE = re.compile(r"^\s*(?:pub\s+)?mod\s+(\w+)\s*;", re.M)


def _rust_split_top(inner: str) -> list[str]:
    """Split a ``use`` group body on top-level commas (ignoring nested ``{}``)."""
    items: list[str] = []
    depth = 0
    cur = ""
    for ch in inner:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur)
    return items


def _rust_expand_use(path: str) -> list[str]:
    """Expand one ``use`` path into concrete module paths, flattening ``{}`` groups and
    dropping ``as`` aliases / ``*`` globs. E.g. ``crate::a::{self, b::c}`` ->
    ``["crate::a", "crate::a::b::c"]``."""
    path = path.strip()
    if "{" not in path:
        p = re.sub(r"\s+as\s+\w+\s*$", "", path).strip()
        p = p.rstrip("*").rstrip(":")  # drop a trailing glob / dangling '::'
        return [p] if p else []
    i = path.index("{")
    prefix = path[:i].strip()          # includes the trailing '::'
    inner = path[i + 1 : path.rindex("}")] if "}" in path[i:] else path[i + 1 :]
    out: list[str] = []
    for item in _rust_split_top(inner):
        item = item.strip()
        if not item:
            continue
        if item == "self":
            base = prefix.rstrip(":")
            if base:
                out.append(base)
        else:
            out.extend(_rust_expand_use(prefix + item))
    return out


def rust_imports(text: str) -> list[str]:
    """Module paths this Rust file pulls in — ``use`` targets and ``mod X;`` declarations.

    ``crate::``/``super::``/``self::`` paths stay prefixed so the IO mapper can resolve
    them; a ``mod X;`` (external-file submodule) is emitted as ``self::X``. External
    crates (``std``, ``tokio``, …) are returned verbatim and simply don't resolve to an
    internal file. Inline ``mod X { … }`` blocks are ignored (no separate file).
    """
    out: list[str] = []
    for m in _RUST_USE_RE.finditer(text):
        out.extend(_rust_expand_use(m.group(1)))
    for m in _RUST_MOD_RE.finditer(text):
        out.append("self::" + m.group(1))
    # de-dup, preserve order
    seen: dict[str, None] = {}
    for x in out:
        if x and x not in seen:
            seen[x] = None
    return list(seen)


_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _is_property_getter(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    # Bare `@property` only — `@x.setter`/`@x.deleter` are Attribute nodes, not Name.
    return any(isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list)


def _dedupe_defs(defs: list[_DefNode]) -> _DefNode:
    """Collapse everything sharing a ``(file, parent, name)`` slot down to the one
    definition a reader/caller would actually resolve to.

    The rule is Python's own name binding: **the last definition wins**, because each
    redefinition rebinds the name and `getattr(module, name)` returns the last one. That
    single rule already handles the case this function was written for — a run of
    ``@overload`` stubs followed by the real implementation — because typing requires the
    implementation to come *after* its stubs, so the last def IS the implementation.

    There used to be an explicit `_is_overload` predicate here, preferring the last
    non-stub. It was removed rather than tested: an adversarial review mutated it to
    return `False` and the entire suite still passed, and that is correct — it can only
    change the answer for a group whose LAST def is an ``@overload`` stub while an earlier
    one is not, which no type checker accepts and no real code contains. Pinning it would
    have meant asserting behaviour for a file Python's own tooling rejects; the honest
    move is to let the simpler mechanism stand alone. Do not reintroduce it without a
    real file that needs it.

    The one genuine exception is a ``@property`` / ``@x.setter`` / ``@x.deleter`` triple,
    where last-wins gives the deleter. Keep the getter: it is what `find_symbol` and
    `functions` show as the signature, and it is the one carrying the documented type.
    That predicate IS load-bearing and is pinned by a test asserting the getter's line.
    """
    if len(defs) == 1:
        return defs[0]
    if all(isinstance(d, ast.FunctionDef | ast.AsyncFunctionDef) for d in defs):
        funcs = [d for d in defs if isinstance(d, ast.FunctionDef | ast.AsyncFunctionDef)]
        getters = [d for d in funcs if _is_property_getter(d)]
        if getters:
            return getters[-1]
    return defs[-1]


def _group_defs(body: list[ast.stmt]) -> tuple[list[str], dict[str, list[_DefNode]]]:
    """Function/class defs directly in ``body``, grouped by name (first-seen order)."""
    order: list[str] = []
    groups: dict[str, list[_DefNode]] = {}
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name not in groups:
                order.append(node.name)
            groups.setdefault(node.name, []).append(node)
    return order, groups


def _is_final_annotation(annotation: ast.expr) -> bool:
    """``Final`` / ``Final[...]`` / ``t.Final[...]`` / ``typing.Final[...]``, by its
    trailing name as written — no attempt to resolve what it imports from, since the
    annotation may or may not be subscripted and may or may not be dotted."""
    node = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    if isinstance(node, ast.Name):
        return node.id == "Final"
    if isinstance(node, ast.Attribute):
        return node.attr == "Final"
    return False


def _is_constant_name(name: str) -> bool:
    return name.isupper()


def _constant_symbols(body: list[ast.stmt], rel_path: str, parent: str = "") -> list[Symbol]:
    """ALL-CAPS assignments and ``Final``-annotated attributes, module- or class-level.

    Two different, deliberately narrow signals, either one sufficient:

    - ALL-CAPS name (``MAX_RETRIES = 3``) — the existing convention this already
      caught, now also recognised when spelled as an annotated assignment
      (``MAX_RETRIES: int = 3``), which the old ``ast.Assign``-only check missed.
    - A ``Final`` annotation, regardless of case (``message: t.Final[str]``) — the
      language's own way of saying "this does not change," which real code (Click's
      exception hierarchy among them) uses on plainly-lowercase attribute names. This
      is *not* the same as merely being annotated: a bare ``name: str`` or
      ``x: t.ClassVar[int]`` class attribute is a typed field, not a constant, and
      capturing every one of those was rejected deliberately — most classes in a real
      codebase declare several, and treating each as an API-reference-eligible symbol
      would flood the table with what are really just typed instance attributes. (They
      still don't show up there either way — `constant` isn't in that page's kind
      filter — but the store would carry the noise regardless.)
    """
    out: list[Symbol] = []
    for node in body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and _is_constant_name(tgt.id):
                    out.append(Symbol(tgt.id, "constant", rel_path, node.lineno, parent=parent))
        elif isinstance(node, ast.AnnAssign):
            tgt = node.target
            if isinstance(tgt, ast.Name) and (
                _is_constant_name(tgt.id) or _is_final_annotation(node.annotation)
            ):
                out.append(Symbol(tgt.id, "constant", rel_path, node.lineno, parent=parent))
    return out


def _param_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    args = node.args.args
    return args[0].arg if len(args) == 1 else None


def _string_literals(expr: ast.expr) -> list[str]:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return [expr.value]
    if isinstance(expr, ast.List | ast.Tuple | ast.Set):
        return [e.value for e in expr.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _pep562_exports(node: ast.FunctionDef | ast.AsyncFunctionDef, rel_path: str) -> list[Symbol]:
    """Names served by a *module-level* ``__getattr__`` (PEP 562 lazy re-exports).

    Only called on a ``__getattr__`` found directly in ``tree.body`` — a same-named
    method on a class (``self``/``cls`` as first arg, defined inside a ``ClassDef``) is
    never passed here, so it is never mistaken for a module export.

    Recognises the parameter compared against a string literal with ``==``
    (``name == "BaseCommand"``) and against a literal collection with ``in``
    (``name in ("A", "B")`` / ``in {"A", "B"}`` / ``in ["A", "B"]`` — all three appear
    in real Click modules). ``!=`` is deliberately not treated as a positive signal —
    it is usually a guard, not a declaration of what's served. Dict-keyed dispatch
    (``name in _SOME_DICT`` / ``return _SOME_DICT[name]``) is deliberately not resolved
    either: enumerating a dict's keys from another assignment is a data-flow inference,
    not a syntactic fact read off this node, and getting it wrong would be worse than
    not trying.
    """
    param = _param_name(node)
    if param is None:
        return []
    out: list[Symbol] = []
    seen: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Compare):
            continue
        if not (isinstance(sub.left, ast.Name) and sub.left.id == param):
            continue
        for op, comparator in zip(sub.ops, sub.comparators, strict=True):
            if not isinstance(op, ast.Eq | ast.In):
                continue
            for name in _string_literals(comparator):
                if name not in seen:
                    seen.add(name)
                    out.append(Symbol(name, "export", rel_path, sub.lineno))
    return out


def _sig(node: ast.AST) -> str:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        args = [a.arg for a in node.args.args]
        if node.args.vararg:
            args.append("*" + node.args.vararg.arg)
        if node.args.kwarg:
            args.append("**" + node.args.kwarg.arg)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)})"
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}"
    return ""


def _python_symbols(rel_path: str, text: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _regex_symbols(rel_path, text, "Python")
    symbols: list[Symbol] = []

    order, groups = _group_defs(tree.body)
    for name in order:
        node = _dedupe_defs(groups[name])
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            symbols.append(Symbol(node.name, "function", rel_path, node.lineno, _sig(node)))
            if node.name == "__getattr__":
                symbols.extend(_pep562_exports(node, rel_path))
        elif isinstance(node, ast.ClassDef):
            symbols.append(Symbol(node.name, "class", rel_path, node.lineno, _sig(node)))
            m_order, m_groups = _group_defs(node.body)
            for m_name in m_order:
                sub = _dedupe_defs(m_groups[m_name])
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
                    symbols.append(
                        Symbol(sub.name, "method", rel_path, sub.lineno, _sig(sub),
                               parent=node.name)
                    )
                # A same-named nested `class` shadowing a method (or vice versa) in the
                # group is a pathological shape; `_dedupe_defs` still picks the last
                # def, it just isn't re-emitted as anything here — nested classes were
                # never extracted as symbols before this change either.
            symbols.extend(_constant_symbols(node.body, rel_path, parent=node.name))

    symbols.extend(_constant_symbols(tree.body, rel_path))
    return symbols


def python_types(rel_path: str, text: str) -> list[TypeRecord]:
    """Class bases (inheritance), from top-level ``class`` statements.

    Populates the same ``types`` table the heavy Roslyn/rust-analyzer backends write to
    (see ``store.replace_types_for_files`` — this must never call ``store.set_types``,
    which replaces the whole table and would wipe those backends' rows on every Python
    reindex). Only module-level classes are recorded: nested classes are not extracted
    as `class` symbols elsewhere in this module either, so a type record for one would
    be an orphan nothing else in the store can reach via ``find_symbol``/``functions``.

    Base names are recorded exactly as written (``Command``, ``t.Generic``) — resolving
    them to fully-qualified names would need import resolution this syntactic backend
    doesn't have, and a guessed qualification would be worse than the plain name.
    ``qualified_name`` is likewise just the class's own (unqualified) name, for the same
    reason: there is no namespace to resolve it against.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    order, groups = _group_defs(tree.body)
    out: list[TypeRecord] = []
    for name in order:
        classes = [d for d in groups[name] if isinstance(d, ast.ClassDef)]
        if not classes:
            continue
        node = classes[-1]  # last definition wins, matching ordinary Python semantics
        bases = [ast.unparse(b) for b in node.bases]
        out.append(TypeRecord(node.name, "class", rel_path, node.lineno, bases))
    return out


def _blank_comments(text: str) -> str:
    """Replace comment and string-literal bodies with spaces, keeping every line and
    column in place so reported line numbers stay exact.

    The scanner is per-line and had no idea when it was inside a block comment, so prose
    that happens to be shaped like a declaration was extracted as API. Measured on the
    real PX4 GPS drivers, `sbf.h` carried the Doxygen continuation line

        Bit 3: set if orbit accuracy information is used(UERE/SISA)

    which matched the function pattern as `used` and was rendered in the API reference in
    exactly the format of a real entry — code literal plus a `file:line` citation — and
    then given a fluent LLM description. Nothing distinguished an English sentence from a
    declared symbol. String literals are blanked for the same reason: a `//` inside a URL
    would otherwise truncate the declaration that precedes it.
    """
    out: list[str] = []
    i, n = 0, len(text)
    block = False
    while i < n:
        ch = text[i]
        if block:
            if text.startswith("*/", i):
                out.append("  ")
                i += 2
                block = False
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
        elif text.startswith("/*", i):
            out.append("  ")
            i += 2
            block = True
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
        elif ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n and text[i] != quote and text[i] != "\n":
                out.append("\\" if text[i] == "\\" else " ")
                if text[i] == "\\" and i + 1 < n:
                    out.append(" ")
                    i += 1
                i += 1
            if i < n and text[i] == quote:
                out.append(quote)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# Languages whose regex fallback runs over comment-stripped text. C-family syntax only —
# `#` comments and docstrings are a different shape and their extractors are AST-based.
_C_COMMENT_LANGS = frozenset({"C", "C++"})


def _regex_symbols(rel_path: str, text: str, language: str) -> list[Symbol]:
    patterns = _REGEX_PATTERNS.get(language, [])
    if not patterns:
        return []
    scan = _blank_comments(text) if language in _C_COMMENT_LANGS else text
    symbols: list[Symbol] = []
    # Match against the blanked text, but keep the ORIGINAL line as the signature so a
    # declaration that legitimately contains a string literal still reads correctly.
    # strict=True is the assertion that blanking preserved every newline: if the two
    # ever disagree, every line number on the page would be silently off.
    for i, (line, raw) in enumerate(
            zip(scan.splitlines(), text.splitlines(), strict=True), start=1):
        for kind, pat in patterns:
            m = pat.search(line)
            if m:
                name = m.group(1)
                if kind == "constant" and _INCLUDE_GUARD_RE.search(name):
                    break  # include guard, not API
                symbols.append(Symbol(name, kind, rel_path, i, raw.strip()[:120]))
                break
    return symbols
