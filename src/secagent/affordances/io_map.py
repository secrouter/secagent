"""Service/component IO mapping.

Builds a directed graph of how parts of the system connect: internal import edges
between components, HTTP endpoints exposed, outbound calls, datastore/broker usage,
and environment inputs. This is the backbone of the architecture diagrams (UC1) and
of the reviewer's "what else does this change touch?" reasoning (UC100).
"""

from __future__ import annotations

from .languages import is_code
from .models import Component, FileRecord, FileSummary, IOEdge


def module_name_for(rel_path: str) -> str | None:
    """Dotted Python module name for a file, or None if not a .py file.

    A leading ``src/`` (src-layout) is stripped; ``__init__.py`` maps to its package.
    """
    if not rel_path.endswith(".py"):
        return None
    parts = rel_path.split("/")
    if parts and parts[0] == "src":
        parts = parts[1:]
    if not parts:
        return None
    parts[-1] = parts[-1][:-3]  # drop .py
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(p for p in parts if p)


def build_module_index(records: list[FileRecord]) -> dict[str, str]:
    """Map dotted module name -> repo-relative path for Python files."""
    index: dict[str, str] = {}
    for r in records:
        mod = module_name_for(r.path)
        if mod:
            index[mod] = r.path
    return index


def resolve_import(imp: str, importer_module: str | None, index: dict[str, str]) -> str | None:
    """Resolve an import string to an internal file path, or None if external.

    Handles absolute (``pkg.mod``) and relative (``.mod`` / ``..pkg.mod``) imports.
    """
    target = imp
    if imp.startswith("."):
        # Relative import: count leading dots, walk up from importer's package.
        level = len(imp) - len(imp.lstrip("."))
        suffix = imp[level:]
        base_parts = (importer_module or "").split(".")
        # importer_module is the module; its package is one level up.
        base_parts = base_parts[: max(0, len(base_parts) - level)]
        target = ".".join([*base_parts, suffix]) if suffix else ".".join(base_parts)
    # Try progressively shorter prefixes (import may name a symbol inside a module).
    parts = target.split(".")
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        if cand in index:
            return index[cand]
    return None


def rust_module_name_for(rel_path: str) -> str | None:
    """``crate``-rooted module path for a Rust file, or None if not a ``.rs`` file.

    The crate root (``src/lib.rs`` / ``src/main.rs``) and any ``mod.rs`` map to their
    enclosing module. Files are prefixed with their crate directory (the path before
    ``src/``) so a workspace's crates don't collide; a crate at the repo root is ``crate``.
    E.g. ``crate_a/src/foo/bar.rs`` -> ``crate_a::foo::bar``, ``src/lib.rs`` -> ``crate``.
    """
    if not rel_path.endswith(".rs"):
        return None
    parts = rel_path.split("/")
    if "src" in parts:
        idx = parts.index("src")
        crate_parts = parts[:idx]
        mod_parts = parts[idx + 1 :]
    else:
        crate_parts, mod_parts = [], parts
    crate = "::".join(crate_parts) if crate_parts else "crate"
    if not mod_parts:
        return crate
    stem = mod_parts[-1][:-3]  # drop .rs
    # lib/main/mod represent the enclosing module, not a submodule named for the file.
    mod_parts = mod_parts[:-1] if stem in ("lib", "main", "mod") else [*mod_parts[:-1], stem]
    return "::".join([crate, *mod_parts])


def build_rust_module_index(records: list[FileRecord]) -> dict[str, str]:
    """Map ``crate``-rooted module path -> repo-relative path for Rust files."""
    index: dict[str, str] = {}
    for r in records:
        mod = rust_module_name_for(r.path)
        if mod:
            index[mod] = r.path
    return index


def resolve_rust_import(imp: str, importer_module: str | None, index: dict[str, str]) -> str | None:
    """Resolve a Rust ``use``/``mod`` path to an internal file, or None if external.

    ``crate::`` is rebased onto the importer's own crate; ``super::``/``self::`` walk up
    from the importer's module. Anything else (``std``, third-party crates) is external.
    Tries progressively shorter prefixes, since a path may name an item inside a module.
    """
    imp = imp.strip()
    segs = imp.split("::")
    importer_parts = (importer_module or "crate").split("::")
    if segs[0] == "crate":
        full = [importer_parts[0], *segs[1:]]
    elif segs[0] in ("self", "super"):
        base = importer_parts[:]
        i = 0
        while i < len(segs) and segs[i] in ("self", "super"):
            if segs[i] == "super" and base:
                base = base[:-1]
            i += 1
        full = [*base, *segs[i:]]
    else:
        return None  # std / external crate — no internal edge
    for j in range(len(full), 0, -1):
        cand = "::".join(full[:j])
        if cand in index:
            return index[cand]
    return None


def component_for(rel_path: str, depth: int = 2) -> str:
    """Group a file into a component by its directory prefix (capped at ``depth``)."""
    dir_parts = rel_path.split("/")[:-1]  # drop the filename
    if not dir_parts:
        return "(root)"
    # For src-layout, group under the package below src/ rather than bare "src".
    if dir_parts[0] == "src" and len(dir_parts) > 1:
        return "/".join(dir_parts[1 : depth + 1])
    return "/".join(dir_parts[:depth])


# Share of a component's files that must be code before the code languages get to decide
# its label on their own. Set from the two measured cases this rule has to separate:
# cFS `apps/fm` is 67 C/header against 128 JSON tables — 34% code, and genuinely a C
# component; a Sphinx `docs/` directory is one `conf.py` against 37 Markdown files — 2.6%
# code, and genuinely documentation. Anything between those is a judgement call, so this
# sits an order of magnitude below the case that must win and well above the one that
# must lose.
_MIN_CODE_SHARE = 0.10


def build_components(records: list[FileRecord], depth: int = 2) -> list[Component]:
    groups: dict[str, Component] = {}
    lang_counts: dict[str, dict[str, int]] = {}
    for r in records:
        name = component_for(r.path, depth)
        comp = groups.get(name)
        if comp is None:
            comp = Component(name=name, path=name, kind="package")
            groups[name] = comp
            lang_counts[name] = {}
        comp.files.append(r.path)
        lang_counts[name][r.language] = lang_counts[name].get(r.language, 0) + 1
    # Assign each component its dominant language, preferring the most frequent *code*
    # language. A raw file-count vote mislabels a code component that ships many data
    # files — e.g. cFS apps/fm (67 C/.h vs 128 JSON tables) would bin as "JSON" and get
    # the wrong per-language toolchain.
    #
    # But "any code at all outvotes every non-code file" overcorrects. A documentation
    # directory of 37 Markdown files plus one Sphinx `conf.py` rendered as "38 Python
    # file(s)" — an authoritative, specific, false statistic, produced by a single file
    # beating 37. Code has to hold a minimum share, not merely be present.
    for name, comp in groups.items():
        counts = lang_counts[name]
        code = {lang: n for lang, n in counts.items() if is_code(lang)}
        total = sum(counts.values())
        code_share = sum(code.values()) / total if total else 0.0
        pool = code if code and code_share >= _MIN_CODE_SHARE else counts
        comp.language = max(pool, key=lambda k: pool[k]) if pool else ""
    return [groups[k] for k in sorted(groups)]


def build_io_map(
    records: list[FileRecord],
    summaries: dict[str, FileSummary],
    depth: int = 2,
) -> tuple[list[Component], list[IOEdge]]:
    """Return (components, edges) for the whole repo."""
    components = build_components(records, depth)
    py_index = build_module_index(records)
    rust_index = build_rust_module_index(records)
    edges: dict[tuple[str, str, str, str], IOEdge] = {}

    def add(edge: IOEdge) -> None:
        edges[edge.key()] = edge

    for r in records:
        comp = component_for(r.path, depth)
        summary = summaries.get(r.path)
        if summary is None:
            continue
        # Internal import edges (resolver dispatched by language).
        if r.language == "Rust":
            importer_mod = rust_module_name_for(r.path)
            resolve = resolve_rust_import
            imp_index = rust_index
        else:
            importer_mod = module_name_for(r.path)
            resolve = resolve_import
            imp_index = py_index
        for imp in summary.imports:
            target_path = resolve(imp, importer_mod, imp_index)
            if target_path:
                dst = component_for(target_path, depth)
                if dst != comp:
                    add(IOEdge(comp, dst, "import"))
        # Exposed endpoints.
        for ep in summary.endpoints:
            add(IOEdge(comp, f"HTTP {ep}", "http_endpoint", detail=ep))
        # Outbound calls.
        for host in summary.outbound_calls:
            add(IOEdge(comp, host, "http_call", detail=host))
        # Datastores.
        for ds in summary.datastores:
            add(IOEdge(comp, ds, "datastore", detail=ds))
        # Message queues / brokers / messaging libraries.
        for ms in summary.messaging:
            add(IOEdge(comp, ms, "messaging", detail=ms))
        # Raw network sockets (TCP/UDP/Unix).
        for sk in summary.sockets:
            add(IOEdge(comp, sk, "socket", detail=sk))
        # Environment inputs (recorded; diagrams may choose to summarize these).
        for ev in summary.env_vars:
            add(IOEdge("env", comp, "env", detail=ev))

    return components, list(edges.values())


_IO_KINDS = ("import", "http_endpoint", "http_call", "datastore", "messaging",
             "socket", "env")
_IO_PER_KIND = 30


def summarize_io(edges: list[IOEdge], max_lines: int = 120) -> str:
    """A compact, agent-readable IO summary.

    Both caps announce themselves. The line budget is the dangerous one: it stops the
    whole loop, so a repo with many imports could silently lose entire categories —
    reading as "this system touches no datastores" when it touches several.
    """
    by_kind: dict[str, list[IOEdge]] = {}
    for e in edges:
        by_kind.setdefault(e.kind, []).append(e)
    lines: list[str] = ["# IO map"]
    for i, kind in enumerate(_IO_KINDS):
        items = by_kind.get(kind, [])
        if not items:
            continue
        lines.append(f"{kind} ({len(items)}):")
        for e in items[:_IO_PER_KIND]:
            lines.append(f"  {e.src} -> {e.dst}" + (f" [{e.detail}]" if e.detail else ""))
        if len(items) > _IO_PER_KIND:
            lines.append(f"  … +{len(items) - _IO_PER_KIND} more {kind} edge(s) not shown")
        if len(lines) >= max_lines:
            omitted = [k for k in _IO_KINDS[i + 1:] if by_kind.get(k)]
            if omitted:
                lines.append("# TRUNCATED at the line budget — these kinds are NOT shown: "
                             + ", ".join(f"{k} ({len(by_kind[k])})" for k in omitted))
            break
    return "\n".join(lines)
