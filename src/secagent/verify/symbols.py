"""Does this test call things that exist, with the right number of arguments?

NOT a gate. This exists to ROUTE a repair stage, and the distinction is the whole point.

Python's dominant generated-test failure is a hallucinated API: 16 of 16 audited failures
were the model inventing a signature, none caused by a real bug in the code under test.
The cheapest way for a model to "fix" a `TypeError: read_config() missing 2 required
positional arguments` is to loosen the assertion until it stops complaining — and we have
a measured instance of it doing exactly that unprompted, in
`test_examples_aliases.py::test_read_config_file_not_found`, whose
`pytest.raises((FileNotFoundError, ValueError, Exception))` swallows the very error its
sibling test fails loudly on.

So: a failure caused by calling something that does not exist, or with the wrong arity,
must be routed to REGENERATE and never to patch. Patching a hallucinated signature is the
mechanism that manufactures vacuous tests.

Python only. C/C++'s dominant failure is a compile error the compile gate already catches,
and recovering call arity there needs a real AST rather than the stdlib.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SymbolIssue:
    name: str
    kind: str          # "missing" | "arity"
    detail: str


@dataclass
class SymbolReport:
    issues: list[SymbolIssue] = field(default_factory=list)
    checked: int = 0

    @property
    def hallucinated(self) -> bool:
        """True when the test calls something the repo does not provide as called.

        The routing signal: regenerate, do not patch.
        """
        return bool(self.issues)


def _module_file(repo: Path, module: str) -> Path | None:
    """Resolve a dotted module to a file, THROUGH the import rather than by name.

    Name-matching would have been confidently wrong on the exact file used as ground
    truth: Click's repo contains two different `read_config` definitions, one taking 3
    arguments and one taking 2. Matching on the bare name picks whichever is found first
    and reports a mismatch against a function the test never called.
    """
    rel = Path(*module.split("."))
    for candidate in (repo / rel.with_suffix(".py"), repo / rel / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _signatures(path: Path) -> dict[str, tuple[int, int, bool]]:
    """``{name: (required, total, accepts_varargs)}`` for module-level defs and classes."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return {}
    out: dict[str, tuple[int, int, bool]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            required = len(positional) - len(args.defaults)
            out[node.name] = (max(0, required), len(positional),
                              bool(args.vararg or args.kwarg))
        elif isinstance(node, ast.ClassDef):
            # A class is callable; check its __init__ minus `self`.
            init = next((n for n in node.body
                         if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
            if init is None:
                out[node.name] = (0, 0, True)
            else:
                positional = init.args.posonlyargs + init.args.args
                required = len(positional) - len(init.args.defaults) - 1
                out[node.name] = (max(0, required), max(0, len(positional) - 1),
                                  bool(init.args.vararg or init.args.kwarg))
    return out


def check_python_test(repo: Path, test_source: str) -> SymbolReport:
    """Check every call to a repo-imported name against that name's real signature."""
    report = SymbolReport()
    try:
        tree = ast.parse(test_source)
    except SyntaxError:
        return report

    # name -> module it was imported FROM. Only `from X import y` is resolvable to a
    # signature without executing anything, which is the only form worth checking.
    origin: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                origin[alias.asname or alias.name] = node.module

    calls: dict[str, set[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.setdefault(node.func.id, set()).add(len(node.args) + len(node.keywords))

    for name, arities in sorted(calls.items()):
        module = origin.get(name)
        if module is None:
            continue                       # not imported from anywhere we can resolve
        path = _module_file(repo, module)
        if path is None:
            continue                       # third-party or stdlib: not ours to judge
        sigs = _signatures(path)
        report.checked += 1
        if name not in sigs:
            report.issues.append(SymbolIssue(
                name, "missing",
                f"{module} defines no `{name}` — the test imports a name that is not there"))
            continue
        required, total, flexible = sigs[name]
        if flexible:
            continue
        for n in sorted(arities):
            if n < required or n > total:
                report.issues.append(SymbolIssue(
                    name, "arity",
                    f"called with {n} argument(s); {module}.{name} takes "
                    f"{required}..{total}"))
                break
    return report
