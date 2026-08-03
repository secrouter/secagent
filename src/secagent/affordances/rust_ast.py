"""Rust AST extraction via tree-sitter — the C/C++ ``clang_ast`` / C# ``csharp_ast``
analog for Rust.

clang gives accurate functions + call sites for C/C++, tree-sitter does the same for
C#; this does it for Rust using the tree-sitter grammar (a pip dependency, no Rust
toolchain and no built project required — so it fits the offline/FIPS posture). It
produces the same :class:`~secagent.affordances.clang_ast.FuncDef` /
:class:`~secagent.affordances.clang_ast.CallSite` shapes, so the language-agnostic
call-map machinery (``build_definition_table`` / ``resolve_calls``) consumes it unchanged.

Extraction is syntactic: every ``function_item`` (a free ``fn`` or a method inside an
``impl``/``trait``) becomes a function definition, and ``call_expression`` nodes become
call sites whose callee is the spelled name (resolved to a defining file later, exactly
as for clang/C#). Macro invocations (``println!`` etc.) are intentionally ignored.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Reuse the AST value types + cache (de)serializers from the clang backend. These are
# plain dataclasses; importing them does NOT load libclang (that import is lazy).
from .clang_ast import CallSite, FuncDef, ParsedUnit

# Bump when the extraction changes, to invalidate cached Rust parses.
RUST_PARSE_VERSION = "rs-v1"

# Declarations that introduce a callable (and thus an enclosing scope for calls). In
# Rust a free function and an impl/trait method are both ``function_item``.
_DEF_TYPES = frozenset({"function_item"})


def _build_parser():
    import tree_sitter_rust as ts
    from tree_sitter import Language, Parser

    lang = Language(ts.language())
    try:                                  # tree-sitter >= 0.22: Parser(language)
        return Parser(lang)
    except TypeError:                     # older API
        parser = Parser()
        try:
            parser.language = lang
        except Exception:                 # noqa: BLE001 - very old API
            parser.set_language(lang)
        return parser


@lru_cache(maxsize=1)
def _parser():
    return _build_parser()


@lru_cache(maxsize=1)
def rust_available() -> bool:
    """True if tree-sitter + the Rust grammar are importable and a parser builds."""
    try:
        _parser()
        return True
    except Exception:  # noqa: BLE001 - any import/load failure means "use the fallback"
        return False


def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _signature(node) -> str:
    """The declaration header (no body), whitespace-collapsed."""
    raw = _text(node)
    cut = len(raw)
    for stop in ("{", ";"):
        i = raw.find(stop)
        if i != -1:
            cut = min(cut, i)
    return " ".join(raw[:cut].split())[:200]


def _callee_name(fn) -> str:
    """The spelled name from a call's function expression."""
    if fn is None:
        return ""
    t = fn.type
    if t == "identifier":
        return _text(fn)
    if t == "field_expression":                  # receiver.method()
        return _callee_name(fn.child_by_field_name("field"))
    if t == "scoped_identifier":                 # module::func() / Type::method()
        return _callee_name(fn.child_by_field_name("name"))
    if t == "generic_function":                  # func::<T>()
        return _callee_name(fn.child_by_field_name("function"))
    if t in ("field_identifier", "type_identifier"):
        return _text(fn)
    nm = fn.child_by_field_name("name")
    return _callee_name(nm) if nm is not None else ""


def parse_rust_file(abs_path: Path, rel_path: str) -> ParsedUnit:
    """Parse one Rust file into function definitions + call sites."""
    if not rust_available():
        return ParsedUnit(parsed=False)
    try:
        src = abs_path.read_bytes()
    except OSError:
        return ParsedUnit(parsed=False)

    tree = _parser().parse(src)
    funcs: list[FuncDef] = []
    calls: list[CallSite] = []

    # Iterative DFS carrying the enclosing callable's name, so call sites are attributed
    # to their function (mirrors clang_ast / csharp_ast enclosing-function tracking).
    stack = [(tree.root_node, "")]
    while stack:
        node, enclosing = stack.pop()
        enc = enclosing
        if node.type in _DEF_TYPES:
            name = _text(node.child_by_field_name("name"))
            if name:
                funcs.append(FuncDef(name, _signature(node), rel_path, node.start_point[0] + 1))
                enc = name
        elif node.type == "call_expression":
            callee = _callee_name(node.child_by_field_name("function"))
            if callee:
                calls.append(CallSite(enclosing, callee, rel_path, node.start_point[0] + 1))
        for child in node.children:
            stack.append((child, enc))

    return ParsedUnit(functions=funcs, calls=calls, parsed=True, errors=0)
