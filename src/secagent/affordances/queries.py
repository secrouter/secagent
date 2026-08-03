"""Affordance query functions — the single source of truth for tool behaviour.

Both the ``secagent affordance`` CLI (which pi drives via its bash tool) and the
optional MCP server call these. Each returns a compact string (text or JSON) so the
agent's observations stay small — the whole point of the affordance layer.
"""

from __future__ import annotations

import fnmatch
import itertools
import json
import sys
from pathlib import Path

from ..llm.tokenizer import TokenCounter
from .io_map import summarize_io
from .models import CallEdge
from .retrieval import ContextBuilder
from .store import AffordanceStore

_MAX_SLICE_LINES = 200
# Cap list-shaped affordance output. A hot symbol (thousands of callers) or a huge
# generated file (thousands of functions) would otherwise flood the agent's context —
# the opposite of what the affordance layer is for.
_MAX_ROWS = 200

# Filenames that commonly hold secrets. read_slice refuses these so a committed key or
# .env under the repo root can't be exfiltrated through the file-reading affordance.
_SECRET_GLOBS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.pkcs12", "*.der",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "*.keystore", "*.jks",
    ".npmrc", ".pypirc", ".netrc", "credentials", "*.pgpass", "*_rsa", "*_ed25519",
)


def _capped(items: list[dict], noun: str) -> str:
    """JSON-encode a list of rows, capping the count so output stays bounded. When
    truncated, a trailing ``note`` element records how many rows were dropped."""
    if len(items) > _MAX_ROWS:
        shown = items[:_MAX_ROWS]
        shown.append({"note": f"+{len(items) - _MAX_ROWS} more {noun} not shown "
                              f"(showing first {_MAX_ROWS}); refine your query"})
        return json.dumps(shown)
    return json.dumps(items)


def _looks_secret(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in _SECRET_GLOBS)


def _fidelity_caveat(store: AffordanceStore) -> str:
    """A warning to append when the C/C++ call map is known to be incomplete.

    Empty string when the parse was clean. This exists because an empty ``callers``
    result is otherwise indistinguishable from "this function genuinely has no callers"
    — the failure mode that makes an engineer conclude live code is dead.
    """
    h = store.parse_health()
    degraded = int(h.get("degraded", 0) or 0)
    if not degraded:
        return ""
    total = int(h.get("total", 0) or 0)
    top = ", ".join(f"{name}" for name, _n in (h.get("top_missing") or [])[:3])
    msg = (f" WARNING: the call map is INCOMPLETE — {degraded} of {total} C/C++ file(s) "
           f"missing PROJECT headers, so calls declared in them are absent. "
           f"An empty result here does NOT prove there are no callers.")
    if top:
        msg += f" Most-missed headers: {top}."
    if not h.get("compile_db"):
        msg += (" No compile_commands.json was used; set affordances.clang_compile_db "
                "for full fidelity.")
    return msg


def _unknown_path(store: AffordanceStore, path: str) -> str | None:
    """A note explaining an unindexed path, or None when the path IS indexed.

    Distinguishing "that file is not in the index" from "that file has nothing to show"
    matters more than it looks. Both used to render as "No functions indexed for 'X'",
    so a caller who simply used the wrong path base — pi's built-in read/bash tools are
    relative to the CWD while the secagent tools are relative to the repo root — got a
    result indistinguishable from a real empty one. Observed consequence: the model
    concluded secagent had no data and silently abandoned it for grep for the rest of the
    task, still producing a correct answer, so nothing surfaced the mistake.
    """
    known = store.known_paths()
    if path in known:
        return None
    # Suggest the intended file: an indexed path that ends with what was asked for, or
    # failing that, one with the same basename.
    tail = path.lstrip("./")
    suggestions = [k for k in known if k.endswith("/" + tail) or k == tail]
    if not suggestions:
        base = tail.rsplit("/", 1)[-1]
        suggestions = [k for k in known if k.rsplit("/", 1)[-1] == base]
    msg = (f"'{path}' is not in the index. Paths are repo-relative (root: "
           f"{store.repo_root}), not relative to your shell's working directory.")
    if suggestions:
        shown = ", ".join(repr(x) for x in sorted(suggestions)[:3])
        msg += f" Did you mean: {shown}?"
    else:
        msg += " Use `search` or `structure` to find the right path."
    return msg


def _note(message: str) -> str:
    """A not-found / empty result for a JSON affordance, as valid JSON.

    The success paths of these affordances return JSON (a list or object), so a bare
    string on the empty path makes ``json.loads`` on the output crash for a structured
    consumer (the MCP server, an eval harness). This keeps the output always-parseable —
    ``{"note": "<guidance>"}`` — while preserving the human-readable message. Consumers
    distinguish "no data" from a result by the ``note`` key / non-list shape.
    """
    return json.dumps({"note": message})


def structure(store: AffordanceStore) -> str:
    pm = store.load_project_map()
    return pm.outline() if pm else "No project map. Run `secagent index` first."


def io(store: AffordanceStore) -> str:
    edges = store.load_io_edges()
    return summarize_io(edges) if edges else "No IO map available."


def _is_placeholder_purpose(purpose: str) -> bool:
    """A non-readable file's stub purpose, e.g. "Other file (11357 bytes)." — not real."""
    return purpose.endswith(" bytes).")


def components(store: AffordanceStore) -> str:
    comps = store.load_components()
    summaries = store.all_summaries()
    out = []
    for c in comps:
        seg = c.name.rsplit("/", 1)[-1].lower()

        # Representative purpose: prefer an LLM-written purpose, then any real purpose, from
        # the most representative file. Order: code/source over docs (a README describes
        # itself, not the component), then files whose name matches the component, then
        # shortest. Never a "(N bytes)" placeholder — those would win for an app shipping a
        # short LICENSE.
        def _rep_key(p: str, seg: str = seg) -> tuple[bool, bool, int]:
            pl = p.lower()
            # A doc file, not any source that merely contains "readme" in its name
            # (e.g. `readme_parser.c` is a code file, not documentation).
            stem = pl.rsplit("/", 1)[-1].split(".", 1)[0]
            is_doc = pl.endswith((".md", ".rst", ".txt")) or stem == "readme"
            return (is_doc, seg not in pl, len(p))

        ordered = sorted(c.files, key=_rep_key)
        candidates = [
            s for f in ordered
            if (s := summaries.get(f)) and s.purpose and not _is_placeholder_purpose(s.purpose)
        ]
        llm = next((s.purpose for s in candidates if s.source == "llm"), "")
        purpose = llm or (candidates[0].purpose if candidates else "")
        out.append({"name": c.name, "language": c.language,
                    "files": len(c.files), "purpose": purpose})
    return json.dumps(out)


# UC0: which secagent tools to run on a component, by language. Commands are templates the
# agent fills in (<repo>, <out>, <file>).
_LANG_PLAYBOOK: dict[str, list[str]] = {
    "C": [
        "secagent affordance calls <repo>        # clang inter-file call map",
        "secagent scan <repo> -o <out>/scan      # memory/stability (Power of Ten/MISRA/CERT)",
        "secagent analyze run <repo> <file.c>    # IKOS static analysis (optional)",
    ],
    "C#": [
        "secagent analyze deep <repo>            # Roslyn: qualified calls + type hierarchy"
        " (needs `make analyzer-dotnet`)",
        "secagent affordance calls <repo>        # tree-sitter call map (fallback)",
    ],
    "Rust": [
        "secagent affordance calls <repo>        # tree-sitter inter-file call map",
        "secagent affordance io <repo>           # use/mod module + architecture graph",
        "cargo clippy --all-targets            # lints (correctness/perf/style)",
        "cargo audit                           # RustSec advisory scan of dependencies",
    ],
}
_LANG_PLAYBOOK["C++"] = _LANG_PLAYBOOK["C"]
_DEFAULT_PLAYBOOK: list[str] = [
    "secagent affordance summaries <repo>    # file purposes + function descriptions",
    "secagent affordance calls <repo>        # inter-file call map, where available",
]


def plan(store: AffordanceStore) -> str:
    """UC0 analysis plan: components binned by language + the secagent tools to run on each.

    A deterministic dispatch so the agent doesn't have to infer the toolchain — drop pi
    in, read this, and run the per-language tools, then synthesize a summary.
    """
    pm = store.load_project_map()
    comps = store.load_components()
    bins: dict[str, list[str]] = {}
    for c in comps:
        bins.setdefault(c.language or "Other", []).append(c.name)
    return json.dumps({
        "languages": pm.languages if pm else {},
        "entrypoints": pm.entrypoints[:10] if pm else [],
        "bins": {lang: sorted(names) for lang, names in sorted(bins.items())},
        "per_language_tools": {
            lang: _LANG_PLAYBOOK.get(lang, _DEFAULT_PLAYBOOK) for lang in sorted(bins)
        },
        "always_run": [
            "secagent index <repo>",
            "secagent affordance structure <repo>",
            "secagent affordance io <repo>",
            "secagent docs build <repo> -o <out>/docs    # architecture + summaries site",
        ],
    }, indent=2)


def summary(store: AffordanceStore, path: str) -> str:
    s = store.load_summary(path)
    if not s:
        return _note(_unknown_path(store, path)
                     or f"No summary stored for '{path}'.")
    return json.dumps(s.to_dict())


def find_symbol(store: AffordanceStore, name: str) -> str:
    syms = store.search_symbols(name)
    if not syms:
        return _note(f"No symbols matching '{name}'.")
    return json.dumps([
        # `path`, not `file`: every PARAMETER on every surface — CLI argv, MCP schema,
        # pi tool — is already `path`, and this value's next stop is `read_slice(path=…)`.
        # Emitting `file` made the model translate the key at each hop; emitting `path`
        # makes chaining a literal copy.
        {"name": s.name, "kind": s.kind, "path": s.file, "line": s.lineno,
         "signature": s.signature, "doc": s.doc,
         **({"qualified_name": s.qualified_name} if s.qualified_name else {})}
        for s in syms
    ])


def types(store: AffordanceStore, name: str = "") -> str:
    """Declared types and their inheritance (bases/interfaces).

    Filter by ``name`` (case-insensitive substring). Python's light ``ast`` backend
    populates this from `class Foo(Bar)` statements on every `secagent index`; for other
    languages it's populated by a heavy backend — ``secagent analyze deep`` (C# Roslyn) or
    the heavy C/C++ build — and stays empty without one. Python's bases are recorded
    exactly as written in source (e.g. ``Command``, not a resolved qualified name);
    other languages' bases/interfaces come qualified from their backend.
    """
    ts = store.load_types()
    if not ts:
        return _note("No type graph indexed. Python is populated by `secagent index`; "
                     "other languages need a heavy backend, e.g. "
                     "`secagent analyze deep <repo>` for C#.")
    if name:
        nl = name.lower()
        ts = [t for t in ts if nl in t.qualified_name.lower()]
        if not ts:
            return _note(f"No types matching '{name}'.")
    return _capped(
        # `path`, for the same reason as `find_symbol` above.
        [{"name": t.qualified_name, "kind": t.kind, "path": t.file, "line": t.lineno,
          "bases": t.bases, "interfaces": t.interfaces} for t in ts],
        "types",
    )


def functions(store: AffordanceStore, path: str) -> str:
    """Functions defined in a file, with signatures and (if generated) descriptions."""
    unknown = _unknown_path(store, path)
    if unknown:
        return _note(unknown)
    syms = [s for s in store.symbols_for_file(path) if s.kind in ("function", "method")]
    if not syms:
        return _note(f"'{path}' is indexed but defines no functions.")
    return _capped([
        {"name": s.name, "signature": s.signature, "line": s.lineno, "doc": s.doc,
         **({"qualified_name": s.qualified_name} if s.qualified_name else {})}
        for s in syms
    ], "functions")


def calls(store: AffordanceStore, path: str = "") -> str:
    """The inter-file call map (file -> file: callees). Filter to a file with ``path``."""
    from .call_map import inter_file, summarize_calls

    # `calls` is the file->file map, so intra-file edges (kept in the store for
    # `callers`) are filtered out here.
    edges = inter_file(store.load_call_edges())
    caveat = _fidelity_caveat(store)
    if not edges:
        return ("No call map. Run `secagent index` (C/C++ needs libclang installed)."
                + caveat)
    if path:
        edges = [e for e in edges if path in (e.src_file, e.dst_file)]
    out = summarize_calls(edges)
    # Always surface the caveat on a partial map — the risk is silent under-reporting,
    # which looks identical to a complete answer.
    return out + ("\n#" + caveat if caveat else "")


def callers(store: AffordanceStore, symbol: str) -> str:
    """Who calls a function — the call edges whose callee is ``symbol`` (impact analysis).

    The inverse of ``calls``: answers "what depends on this?" before you change it. Needs
    the call map (C/C++ via libclang, C# via tree-sitter); the name must match a called
    function exactly.

    Each row is ``{path, caller, line, dispatch}``. ``path`` and ``line`` are exactly
    what ``read_slice`` takes, so "who calls X, now show me the call site" is two calls
    with no translation in between. ``line`` is absent when the store predates the
    call-site column — run ``secagent index`` and it appears; it is never reported as 0.
    """
    edges = [e for e in store.load_call_edges() if e.callee == symbol]
    if not edges:
        return _note(f"No callers found for '{symbol}'. Needs the call map, and the name "
                     "must match a called function exactly (try `find-symbol`)."
                     + _fidelity_caveat(store))
    # One row per calling function, keeping the LOWEST known call-site line — a caller
    # that calls the symbol from several places is one answer to "who calls this", and
    # the first site is the representative.
    first: dict[tuple[str, str], CallEdge] = {}
    for e in edges:
        k = (e.src_file, e.caller)
        prev = first.get(k)
        if prev is None or (e.line and (not prev.line or e.line < prev.line)):
            first[k] = e
    out: list[dict] = []
    for e in first.values():
        # `dispatch`, not `kind`: `find_symbol` and `types` already emit `kind` for
        # SYMBOL kinds (function/class/…), and this holds direct/virtual/interface.
        # One key with two disjoint vocabularies is a guess a reader should not have
        # to make.
        row: dict = {"path": e.src_file, "caller": e.caller}
        # Absent, not 0. A store indexed before the call-site column existed would
        # otherwise report every call as happening on line 0 — a fabricated location
        # is worse than a missing one, because it sends a reader somewhere.
        if e.line:
            row["line"] = e.line
        row["dispatch"] = e.edge_kind
        out.append(row)
    return _capped(out, "callers")


def summaries_manifest(store: AffordanceStore) -> dict:
    """Per-model record of generated summaries: file purposes + function descriptions.

    Used to evaluate models — diff two builds' manifests (or the JSON ``summaries``
    output) instead of eyeballing rendered HTML. ``model`` is the model that produced
    the summaries (recorded at index time).
    """
    model = store.get_meta("summary_model") or ""
    files = {p: s.purpose for p, s in sorted(store.all_summaries().items())}
    functions: dict[str, dict[str, str]] = {}
    for rec in store.file_records():
        docs = {s.name: s.doc for s in store.symbols_for_file(rec.path)
                if s.kind in ("function", "method") and s.doc}
        if docs:
            functions[rec.path] = docs
    return {"model": model, "files": files, "functions": functions}


def summaries(store: AffordanceStore, component: str = "") -> str:
    """The summaries manifest as pretty JSON (file purposes + function docs).

    Primarily a model-evaluation view — the whole repo's purposes are large, so for agent
    use pass ``component`` (a path prefix) to scope it to one component.
    """
    data = summaries_manifest(store)
    if component:
        data["files"] = {p: v for p, v in data["files"].items() if p.startswith(component)}
        data["functions"] = {p: v for p, v in data["functions"].items()
                             if p.startswith(component)}
    return json.dumps(data, indent=2)


def raw_cache(store: AffordanceStore) -> str:
    """Every raw cached LLM value (file purposes + function docs) as pretty JSON.

    The cache is keyed by an opaque SHA-256 of (content, prompt version, model), so
    entries are NOT attributable to a file/function — this is the literal cache content.
    Use ``summaries`` for attributed, model-labelled output.
    """
    purposes: list[dict] = []
    docs: list[dict] = []
    other: list[dict] = []
    for key, value in store.cache.items():
        item = {"key": key[:12], **value}
        if "purpose" in value:
            purposes.append(item)
        elif "doc" in value:
            docs.append(item)
        else:
            other.append(item)
    return json.dumps(
        {
            "cache_dir": str(store.cache.root),
            "counts": {
                "purpose": len(purposes),
                "doc": len(docs),
                "other": len(other),
                "total": len(purposes) + len(docs) + len(other),
            },
            "purposes": purposes,
            "function_docs": docs,
            "other": other,
        },
        indent=2,
    )


def search(store: AffordanceStore, query: str, limit: int = 15,
           counter: TokenCounter | None = None) -> str:
    builder = ContextBuilder(store, 4096, counter or TokenCounter())
    all_ranked = builder.rank_summaries(query)
    ranked = all_ranked[:limit]
    out: list[dict] = [{"path": s.path, "purpose": s.purpose} for s in ranked]
    # A ranked list that stops at `limit` looks like "these are all the relevant files".
    if len(all_ranked) > limit:
        out.append({"note": f"+{len(all_ranked) - limit} more file(s) matched but are not "
                            f"shown (showing the top {limit}); narrow the query or raise "
                            f"--limit"})
    return json.dumps(out)


def context(store: AffordanceStore, query: str, budget_tokens: int,
            counter: TokenCounter | None = None, max_files: int = 0) -> str:
    builder = ContextBuilder(store, budget_tokens, counter or TokenCounter())
    return builder.for_query(query, max_files=max_files)


def read_slice(store: AffordanceStore, rel_path: str, start: int, end: int) -> str:
    """Read a bounded slice of a real file (1-indexed inclusive), traversal-guarded."""
    repo_root = store.repo_root
    target = (repo_root / rel_path).resolve()
    try:
        target.relative_to(repo_root)
    except ValueError:
        return "ERROR: path escapes repository root."
    if not target.is_file():
        return f"ERROR: not a file: {rel_path}"
    if _looks_secret(target.name):
        return f"ERROR: refusing to read a likely secret file: {rel_path}"
    start = max(1, start)
    requested_end = end
    end = max(start, min(end, start + _MAX_SLICE_LINES - 1))
    clamped = requested_end > end
    # Stream only the requested window (islice stops after `end` lines) so a multi-GB
    # file isn't loaded into memory to return a few lines.
    try:
        with target.open(encoding="utf-8") as fh:
            chunk = [ln.rstrip("\r\n") for ln in itertools.islice(fh, start - 1, end)]
    except (OSError, UnicodeDecodeError) as exc:
        return f"ERROR reading {rel_path}: {exc}"
    numbered = [f"{start + i}: {ln}" for i, ln in enumerate(chunk)]
    if not numbered:
        return "(empty range)"
    if clamped:
        # Asking for 1-500 and silently getting 1-200 reads as "the file ends at 200".
        numbered.append(f"… stopped at line {end}: a slice is capped at "
                        f"{_MAX_SLICE_LINES} lines (you asked for {requested_end}). "
                        f"Request the next range to continue.")
    return "\n".join(numbered)


def ensure_indexed(repo: str | Path, settings) -> AffordanceStore:
    """Open the store, building a heuristic-only index if none exists yet.

    Convenience for pi/CLI usage so an affordance query never fails just because
    ``secagent index`` hasn't been run; a full (LLM-summarised) index is still better.
    """
    from .api import index_repo

    store = AffordanceStore(repo, settings.affordances.store_dir)
    if store.load_project_map() is None:
        store.close()
        # Signal the implicit work on stderr (stdout carries the affordance result) so a
        # first-call latency spike isn't a silent mystery. Run `secagent index` for a full
        # LLM-summarized index instead of this heuristic-only one.
        print(f"secagent: no index for {repo}; building a heuristic index first…",
              file=sys.stderr, flush=True)
        light = settings.model_copy(deep=True)
        light.affordances.llm_summaries = False
        index_repo(repo, light)
        store = AffordanceStore(repo, settings.affordances.store_dir)
    return store
