"""C# AST extraction via tree-sitter — the C/C++ ``clang_ast`` analog for .NET.

clang gives accurate functions + call sites for C/C++; this does the same for C# using
the tree-sitter grammar (a pip dependency, no .NET SDK and no built project required).
It produces the same :class:`~secagent.affordances.clang_ast.FuncDef` /
:class:`~secagent.affordances.clang_ast.CallSite` shapes, so the language-agnostic call-map
machinery (``build_definition_table`` / ``resolve_calls``) consumes it unchanged.

Extraction is syntactic: method/constructor/local-function declarations become method
definitions, and ``invocation_expression`` nodes become call sites whose callee is the
spelled method name (resolved to a defining file later, exactly as for clang).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Reuse the AST value types + cache (de)serializers from the clang backend. These are
# plain dataclasses; importing them does NOT load libclang (that import is lazy).
from .clang_ast import CallSite, FuncDef, ParsedUnit

# Bump when the extraction changes, to invalidate cached C# parses.
CSHARP_PARSE_VERSION = "cs-v1"

# Declarations that introduce a callable (and thus an enclosing scope for calls).
_DEF_TYPES = frozenset({
    "method_declaration", "constructor_declaration", "destructor_declaration",
    "local_function_statement", "operator_declaration",
})

# Generated/partial files that add noise rather than architecture signal.
_GENERATED_SUFFIXES = (".designer.cs", ".g.cs", ".g.i.cs", ".generated.cs")


def is_generated_csharp(path: str) -> bool:
    p = path.lower()
    return p.endswith(_GENERATED_SUFFIXES) or p.endswith("assemblyinfo.cs")


def _build_parser():
    import tree_sitter_c_sharp as ts
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
def csharp_available() -> bool:
    """True if tree-sitter + the C# grammar are importable and a parser builds."""
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
    for stop in ("{", "=>", ";"):
        i = raw.find(stop)
        if i != -1:
            cut = min(cut, i)
    return " ".join(raw[:cut].split())[:200]


def _callee_name(fn) -> str:
    """The spelled method name from an invocation's function expression."""
    if fn is None:
        return ""
    t = fn.type
    if t == "identifier":
        return _text(fn)
    if t == "member_access_expression":          # obj.Method() / a.b.Method()
        return _callee_name(fn.child_by_field_name("name"))
    if t == "generic_name":                      # Method<T>()
        for ch in fn.children:
            if ch.type == "identifier":
                return _text(ch)
        return ""
    nm = fn.child_by_field_name("name")
    return _callee_name(nm) if nm is not None else ""


def parse_csharp_file(abs_path: Path, rel_path: str) -> ParsedUnit:
    """Parse one C# file into method definitions + call sites."""
    if not csharp_available():
        return ParsedUnit(parsed=False)
    try:
        src = abs_path.read_bytes()
    except OSError:
        return ParsedUnit(parsed=False)

    tree = _parser().parse(src)
    funcs: list[FuncDef] = []
    calls: list[CallSite] = []

    # Iterative DFS carrying the enclosing callable's name, so call sites are attributed
    # to their method (mirrors clang_ast's enclosing-function tracking).
    stack = [(tree.root_node, "")]
    while stack:
        node, enclosing = stack.pop()
        enc = enclosing
        if node.type in _DEF_TYPES:
            name = _text(node.child_by_field_name("name"))
            if name:
                funcs.append(FuncDef(name, _signature(node), rel_path, node.start_point[0] + 1))
                enc = name
        elif node.type == "invocation_expression":
            callee = _callee_name(node.child_by_field_name("function"))
            if callee:
                calls.append(CallSite(enclosing, callee, rel_path, node.start_point[0] + 1))
        for child in node.children:
            stack.append((child, enc))

    return ParsedUnit(functions=funcs, calls=calls, parsed=True, errors=0)
