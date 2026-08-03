"""Dataclass models for affordance artifacts.

These are plain dataclasses (not pydantic) so they serialize predictably to JSON in
the store and stay dependency-light in the hot path. All paths are repo-relative and
use forward slashes for cross-platform stability.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Symbol:
    name: str
    kind: str  # function | class | method | constant | route
    file: str
    lineno: int
    signature: str = ""
    parent: str = ""  # enclosing class/module, if any
    doc: str = ""  # one-line description of what it does (heuristic or LLM)
    # Fully-qualified name (namespace/type::method) — populated by semantic/heavy
    # backends (Roslyn, clang build); empty for the syntactic/regex backends.
    qualified_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CallEdge:
    """A directed call relationship between two source files (the call map)."""

    src_file: str
    dst_file: str
    caller: str = ""  # enclosing function in src_file
    callee: str = ""  # called function defined in dst_file
    # How the call dispatches — semantic backends distinguish virtual/interface from a
    # plain direct call; syntactic backends always emit "direct".
    edge_kind: str = "direct"  # direct | virtual | interface
    # Line of the call site in ``src_file``. Every producer already had this — clang's
    # `CallSite.line`, tree-sitter's `node.start_point[0] + 1`, the heavy backend's
    # `AnalysisCall.line` — and it was dropped on the way into the store, so "who calls
    # X, now show me the call site" needed a second lookup and a guess about which
    # symbol was meant. 0 means unknown (an edge from a store indexed before this
    # column existed); consumers must omit the field rather than print line 0.
    line: int = 0

    def key(self) -> tuple[str, str, str, str]:
        # `line` is deliberately NOT part of the key. A caller that calls the same
        # callee from five places is one edge, not five; including the line would
        # multiply every edge in `calls`/`callers` output by its call-site count.
        # The retained line is the FIRST call site — a representative, which is all
        # "show me where" needs.
        return (self.src_file, self.dst_file, self.caller, self.callee)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TypeRecord:
    """A declared type and its inheritance, from a semantic/heavy analysis backend."""

    qualified_name: str
    kind: str  # class | struct | interface | record | enum
    file: str
    lineno: int = 0
    bases: list[str] = field(default_factory=list)        # qualified base types
    interfaces: list[str] = field(default_factory=list)   # qualified implemented interfaces

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileRecord:
    path: str
    language: str
    size: int
    sha256: str
    loc: int = 0
    n_symbols: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IOEdge:
    """A directed input/output relationship between two components/externals."""

    src: str
    dst: str
    # import | http_endpoint | http_call | env | datastore | messaging | socket | file_io | cli
    kind: str
    detail: str = ""

    def key(self) -> tuple[str, str, str, str]:
        return (self.src, self.dst, self.kind, self.detail)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileSummary:
    path: str
    purpose: str = ""
    key_symbols: list[str] = field(default_factory=list)  # signatures
    imports: list[str] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)  # exposed routes
    outbound_calls: list[str] = field(default_factory=list)  # urls/hosts called
    env_vars: list[str] = field(default_factory=list)
    datastores: list[str] = field(default_factory=list)
    messaging: list[str] = field(default_factory=list)  # message queues/brokers used
    sockets: list[str] = field(default_factory=list)  # raw TCP/UDP/Unix socket usage
    side_effects: list[str] = field(default_factory=list)
    source: str = "heuristic"  # heuristic | llm

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Component:
    """A cohesive unit — a package, top-level module group, or detected service."""

    name: str
    path: str
    kind: str  # package | module | service | dir
    files: list[str] = field(default_factory=list)
    language: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProjectMap:
    root: str
    languages: dict[str, int] = field(default_factory=dict)  # language -> file count
    components: list[Component] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    build_files: list[str] = field(default_factory=list)
    tree: dict[str, Any] = field(default_factory=dict)  # nested dir outline
    file_count: int = 0
    total_loc: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def outline(self, max_lines: int = 200) -> str:
        """A compact, agent-readable structure outline."""
        lines: list[str] = [f"# Project: {self.root}"]
        langs = ", ".join(f"{k}:{v}" for k, v in sorted(
            self.languages.items(), key=lambda kv: -kv[1]))
        lines.append(f"languages: {langs}")
        lines.append(f"files: {self.file_count}, loc: {self.total_loc}")
        if self.entrypoints:
            shown = ", ".join(self.entrypoints[:10])
            extra = len(self.entrypoints) - 10
            lines.append("entrypoints: " + shown
                         + (f" (+{extra} more)" if extra > 0 else ""))
        lines.append("components:")
        for i, c in enumerate(self.components):
            lines.append(f"  - {c.name} ({c.kind}, {c.language}, {len(c.files)} files)")
            if len(lines) >= max_lines:
                remaining = len(self.components) - (i + 1)
                if remaining > 0:
                    lines.append(f"  … +{remaining} more component(s) not shown "
                                 "(line budget)")
                break
        return "\n".join(lines)
