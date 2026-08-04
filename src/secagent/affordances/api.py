"""High-level affordance operations: indexing a repository end to end."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TextIO

from ..audit import get_audit_logger
from ..config import Settings
from ..llm.client import LLMClient
from ..progress import progress_step
from ..security import enforce_fips_policy, file_hash
from .cache import ContentCache
from .call_map import (
    build_definition_table,
    build_local_definitions,
    inter_file,
    resolve_calls,
)
from .clang_ast import (
    CLANG_LANGS,
    CLANG_PARSE_VERSION,
    CLANG_SOURCE_EXTS,
    clang_available,
    discover_compile_db,
    is_system_header,
    parse_file,
    unit_from_cache,
    unit_to_cache,
)
from .csharp_ast import (
    CSHARP_PARSE_VERSION,
    csharp_available,
    is_generated_csharp,
    parse_csharp_file,
)
from .file_summary import summarize_file
from .io_map import build_io_map
from .languages import detect_language, is_code, rel_posix, walk_files
from .models import FileRecord, FileSummary, Symbol
from .project_map import build_project_map, entrypoint_files_from_symbols
from .rust_ast import RUST_PARSE_VERSION, parse_rust_file, rust_available
from .store import AffordanceStore
from .symbols import extract_symbols, python_types

log = logging.getLogger(__name__)


class _IndexLock:
    """Cross-process exclusive lock so only one index runs per store at a time.

    pi spawns several secagent processes concurrently; two full indexes writing the same
    store contend on the SQLite write lock and time out ("database is locked"). This
    serializes them via ``flock`` (released automatically if a holder dies, so it can't
    wedge). Best-effort: a no-op where ``fcntl`` is unavailable (non-POSIX), where WAL +
    the busy timeout still apply.
    """

    def __init__(self, store_dir: Path) -> None:
        store_dir.mkdir(parents=True, exist_ok=True)
        self._path = store_dir / "index.lock"
        self._fh: TextIO | None = None

    def acquire(self) -> None:
        try:
            import fcntl
        except ImportError:
            return
        fh = open(self._path, "w")  # noqa: SIM115 - kept open for the lock's lifetime
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            self._fh = fh
        except OSError:
            fh.close()

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        self._fh.close()
        self._fh = None


# Languages whose text we read for symbols/summaries (code + lightweight markup).
_TEXT_LANGS_EXTRA = {"YAML", "TOML", "JSON", "Markdown", "reStructuredText", "Build", "SQL",
                     "MSBuild"}


def index_repo(
    path: str | Path,
    settings: Settings,
    *,
    refresh: bool = False,
    llm: LLMClient | None = None,
    llm_scope: set[str] | None = None,
) -> dict[str, Any]:
    """Build or update the affordance store for ``path``.

    Incremental: unchanged files (by SHA-256) keep their existing artifacts. Returns
    a JSON-serializable report.

    ``llm_scope``, when given, is a set of repo-relative paths (as produced by
    `..languages.rel_posix`) OUTSIDE of which the LLM is never called for a file's
    purpose summary — every file is still walked and structurally indexed (symbols,
    language, size, the project/IO/call maps), only the *expensive* per-file model
    call is confined to the set. This is how a git-delta-scoped `docs build` keeps
    the whole-repo index (and the site it renders) complete while spending model
    budget only on the files that changed; see `agents/docs/agent.py`'s
    `build_docs(scope=...)`. ``None`` (the default) means no restriction at all —
    every eligible file may use the LLM, exactly as before this parameter existed.
    A file outside ``llm_scope`` whose content is unchanged is skipped exactly like
    today (its cached summary is reused); one whose content changed (typically only
    on a first-ever index, since a delta by construction covers every content change
    on the current branch) is still re-indexed structurally, with a heuristic
    summary. ``refresh``/``settings.affordances.refresh_summaries`` — "regenerate
    even if cached" — apply only to files inside ``llm_scope``: forcing a
    re-evaluation of the model must not spend that same budget on files a scoped run
    deliberately excluded.
    """
    root = Path(path).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    enforce_fips_policy(settings.fips.require_fips, settings.fips.allow_non_fips)

    if settings.affordances.llm_summaries:
        from ..netpolicy import enforce

        enforce(settings)  # validate the LLM endpoint before any outbound call

    cfg = settings.affordances
    owns_llm = False
    if llm is None and cfg.llm_summaries:
        llm = LLMClient(settings.llm)
        owns_llm = True

    updated = skipped = 0
    seen: set[str] = set()
    # Only populated (and only reported) when `llm_scope` is not None — see the
    # `report` assembly below. Tracked by the SUMMARY's own `source`, not by whether
    # the LLM was merely offered: `cfg.llm_summaries=False` (`--no-llm`) or a failed
    # call both leave `source == "heuristic"` even for an in-scope file, and this
    # list must answer "did this file's purpose actually come from the model this
    # run", not "was it eligible to".
    llm_refreshed: list[str] = []
    log.info("Indexing %s%s", root, "" if cfg.llm_summaries else " (heuristic, no LLM)")
    sd = Path(cfg.store_dir)
    store_dir = sd if sd.is_absolute() else root / sd
    lock = _IndexLock(store_dir)
    lock.acquire()  # serialize concurrent indexes (pi runs several secagent processes)
    try:
        store = AffordanceStore(root, cfg.store_dir)
        try:
            files = list(walk_files(root, cfg.ignore_globs, cfg.ignore_vcs))
            total = len(files)
            step = progress_step(total)
            log.info("Scanning %d files…", total)
            for i, fpath in enumerate(files, start=1):
                rel = rel_posix(fpath, root)
                seen.add(rel)
                sha = file_hash(fpath)
                in_llm_scope = llm_scope is None or rel in llm_scope
                # --refresh-summaries must also bypass the incremental skip. The skip
                # short-circuits before _process_file, which is the ONLY place
                # refresh_summaries has any effect — so on an already-indexed repo the
                # flag documented to "regenerate LLM summaries even if cached" silently
                # did nothing at all. But a refresh request must not itself widen a
                # scoped run's LLM budget: it only applies within `llm_scope`, so a file
                # this run deliberately excluded stays excluded even with --refresh.
                force_refresh = (refresh or cfg.refresh_summaries) and in_llm_scope
                if not force_refresh and store.existing_sha(rel) == sha:
                    skipped += 1
                else:
                    file_llm = llm if in_llm_scope else None
                    rec, summary, symbols, types = _process_file(
                        rel, fpath, sha, cfg, store, file_llm)
                    store.upsert_file(rec, summary, symbols)
                    # File-scoped, not `set_types` — see `replace_types_for_files`'s
                    # docstring: this must not wipe a heavy backend's rows for every
                    # OTHER file just because this one Python file was re-indexed. Only
                    # Python currently produces light-backend type records, so this is
                    # skipped (no DELETE, no query) for every non-Python file rather
                    # than run as a guaranteed-empty no-op on each of them.
                    if rec.language == "Python":
                        store.replace_types_for_files([rel], types)
                    # Commit per file so the write lock is released between files rather
                    # than held across the per-file LLM summary call — otherwise a
                    # concurrent secagent process gets "database is locked".
                    store.commit()
                    updated += 1
                    if summary.source == "llm":
                        llm_refreshed.append(rel)
                if i % step == 0 or i == total:
                    log.info("  %d/%d files (%d summarized, %d cached)",
                             i, total, updated, skipped)

            # Drop files that disappeared since the last index.
            removed = store.known_paths() - seen
            for rel in removed:
                store.delete_file(rel)

            store.commit()

            # Rebuild the whole-repo views from current records.
            log.info("Building project / IO maps…")
            records = store.file_records()
            summaries = store.all_summaries()
            pm = build_project_map(str(root), records)
            components, edges = build_io_map(records, summaries)
            store.set_project_map(pm)
            store.set_io_map(components, edges)
            # One snapshot write for the whole run, not one per file — the per-file
            # `replace_types_for_files` calls above deliberately don't write it (see its
            # docstring). Unconditional so a run where every file was skipped by the
            # SHA check still republishes the current table rather than leaving a stale
            # artifact from whenever types last actually changed.
            store.write_types_artifact()
            store.commit()

            # AST passes: accurate functions + the inter-file call map. clang for
            # C/C++; tree-sitter for C# and Rust. All feed the same call-map machinery.
            n_funcs, n_calls = _index_clang(root, records, cfg, store, refresh)
            cs_funcs, cs_calls = _index_csharp(root, records, cfg, store, refresh)
            rs_funcs, rs_calls = _index_rust(root, records, cfg, store, refresh)
            # Framework entrypoints (e.g. cFS apps' *_AppMain) are *functions*, not
            # main.py/index.js files, so they only surface once the AST passes have
            # populated function symbols. Fold them into the filename-based entrypoints.
            ep_files = entrypoint_files_from_symbols(store.function_symbol_names())
            if ep_files - set(pm.entrypoints):
                pm.entrypoints = sorted(set(pm.entrypoints) | ep_files)
                store.set_project_map(pm)
            # Record which model produced the LLM summaries/descriptions (for the
            # per-model summaries manifest used to evaluate models).
            if llm is not None:
                store._set_meta("summary_model", settings.llm.model)
            store.commit()
            log.info("Indexed %d files (%d summarized, %d cached, %d removed)",
                     total, updated, skipped, len(removed))

            report = {
                "root": str(root),
                "files_indexed": len(records),
                "updated": updated,
                "skipped": skipped,
                "removed": len(removed),
                "languages": pm.languages,
                "components": len(components),
                "io_edges": len(edges),
                "clang_functions": n_funcs,
                # Fidelity of the C/C++ pass: a degraded count > 0 means symbols and the
                # call map are incomplete for those files (see _record_parse_health).
                "clang_parse_health": store.parse_health(),
                "csharp_functions": cs_funcs,
                "rust_functions": rs_funcs,
                # Inter-file edges (unchanged meaning); intra-file edges are also
                # stored now so `callers` can answer same-file questions.
                "call_edges": len(inter_file(store.load_call_edges())),
                "intra_file_call_edges": len(store.load_call_edges())
                - len(inter_file(store.load_call_edges())),
                "entrypoints": pm.entrypoints,
                "store_dir": str(store.store_dir),
            }
            # Only present when a caller actually asked for LLM scoping — an
            # unscoped index (every existing caller: `secagent index`, `scan`,
            # `testgen`, and an unscoped `docs build`) must get the exact same
            # report shape it always has.
            if llm_scope is not None:
                report["llm_scope_requested"] = sorted(llm_scope)
                report["llm_refreshed"] = sorted(llm_refreshed)
            get_audit_logger(settings).record(
                "index",
                target={"repo": str(root), "files_indexed": len(records),
                        "updated": updated, "removed": len(removed)},
                model=settings.llm.model if cfg.llm_summaries else "",
                endpoint=settings.llm.base_url if cfg.llm_summaries else "",
            )
            return report
        finally:
            store.close()
    finally:
        lock.release()
        if owns_llm and llm is not None:
            llm.close()


def _process_file(
    rel: str,
    fpath: Path,
    sha: str,
    cfg,
    store: AffordanceStore,
    llm: LLMClient | None,
) -> tuple[FileRecord, FileSummary, list, list]:
    language = detect_language(fpath)
    size = fpath.stat().st_size
    rec = FileRecord(path=rel, language=language, size=size, sha256=sha)

    readable = is_code(language) or language in _TEXT_LANGS_EXTRA
    should_read = readable and size <= cfg.max_file_bytes
    if not should_read:
        summary = FileSummary(path=rel, purpose=f"{language} file ({size} bytes).")
        return rec, summary, [], []

    text = _read_text(fpath)
    if text is None:
        summary = FileSummary(path=rel, purpose=f"{language} binary/large file.")
        return rec, summary, [], []

    rec.loc = text.count("\n") + 1
    symbols = extract_symbols(rel, text, language) if is_code(language) else []
    rec.n_symbols = len(symbols)
    # Class-hierarchy records for the `types` table. Only the Python ast backend
    # populates this from the light per-file pass (see `store.replace_types_for_files`
    # for why it must go through that, not `set_types`); other light backends don't
    # produce type/inheritance data at all, and the heavy C#/Rust passes populate
    # `types` separately via `analysis.ingest_report`.
    types = python_types(rel, text) if language == "Python" else []
    summary = summarize_file(
        rel, text, language, symbols,
        llm=llm if cfg.llm_summaries else None,
        cache=store.cache,
        content_sha=sha,
        refresh_summaries=cfg.refresh_summaries,
    )
    return rec, summary, symbols, types


def _index_clang(root: Path, records, cfg, store: AffordanceStore,
                 refresh: bool = False) -> tuple[int, int]:
    """Parse C/C++ files with clang to populate accurate functions + the call map.

    Returns (n_functions, n_call_edges). No-op (0, 0) when libclang is unavailable, so
    the index still succeeds — callers fall back to regex symbols / the LLM.

    Each file's parse (CPU-bound) is cached by its content hash, so re-indexing — e.g.
    repeated docs builds while evaluating different LLM models — does not re-parse
    unchanged C/C++. ``refresh=True`` bypasses the cache. (The key is the source file's
    own hash; editing a header without touching a dependent .c reuses the cached parse
    until ``--refresh``.)
    """
    if not clang_available():
        n_cxx = sum(1 for r in records if r.language in CLANG_LANGS
                    and Path(r.path).suffix.lower() in CLANG_SOURCE_EXTS)
        if n_cxx:
            log.warning(
                "libclang not available: %d C/C++ file(s) indexed with the regex fallback "
                "(top-level defs only) — no accurate call map. Install the `clang` extra "
                "for full C/C++ analysis.", n_cxx)
        return 0, 0
    compile_db = discover_compile_db(root, cfg.clang_compile_db)
    db_sig = f"{compile_db or ''}|{','.join(cfg.clang_extra_includes)}"
    sources = [r for r in records if r.language in CLANG_LANGS
               and Path(r.path).suffix.lower() in CLANG_SOURCE_EXTS]
    if not sources:
        return 0, 0
    total = len(sources)
    step = progress_step(total)
    log.info("clang: parsing %d C/C++ file(s)%s…", total,
             " (compile DB)" if compile_db else " (best-effort)")
    all_funcs: list = []
    all_calls: list = []
    degraded: list[str] = []          # files whose parse lost fidelity
    missing: dict[str, int] = {}      # PROJECT header -> how many files missed it
    missing_system: dict[str, int] = {}  # std-lib headers (env note, not a defect)
    for i, rec in enumerate(sources, start=1):
        key = ContentCache.make_key(rec.sha256, CLANG_PARSE_VERSION, db_sig)
        unit = None
        if not refresh:
            hit = store.cache.get(key)
            if hit is not None:
                unit = unit_from_cache(hit)
        if unit is None:
            unit = parse_file(root / rec.path, root, compile_db,
                              tuple(cfg.clang_extra_includes))
            if unit.parsed:
                store.cache.put(key, unit_to_cache(unit))
        if i % step == 0 or i == total:
            log.info("  clang: %d/%d files (%d functions)", i, total, len(all_funcs))
        if not unit.parsed:
            continue
        # Only missing PROJECT headers mark a file degraded and drive the warning;
        # missing system headers are near-universal and tracked separately as an
        # environment note (see _record_parse_health).
        if unit.degraded:
            degraded.append(rec.path)
        for h in unit.missing_project_headers:
            missing[h] = missing.get(h, 0) + 1
        for h in unit.missing_headers:
            if is_system_header(h):
                missing_system[h] = missing_system.get(h, 0) + 1
        store.set_symbols(
            rec.path,
            [Symbol(f.name, "function", f.file, f.line, f.signature) for f in unit.functions],
        )
        all_funcs.extend(unit.functions)
        all_calls.extend(unit.calls)
    _record_parse_health(store, total, degraded, missing, missing_system, bool(compile_db))
    if not all_funcs:
        return 0, 0
    edges = resolve_calls(all_calls, build_definition_table(all_funcs),
                          include_intra_file=True,
                          local_defs=build_local_definitions(all_funcs))
    store.set_call_map(edges)
    log.info("clang: %d functions, %d inter-file call edges", len(all_funcs), len(edges))
    return len(all_funcs), len(edges)


def _record_parse_health(store: AffordanceStore, total: int, degraded: list[str],
                         missing: dict[str, int], missing_system: dict[str, int],
                         had_compile_db: bool) -> None:
    """Persist (and warn about) C/C++ parse fidelity.

    A degraded parse is not a crash — clang returns *something* — but unresolved types
    collapse to implicit ``int`` and calls into unparsed declarations disappear from the
    call map. Left unreported, an incomplete call graph is indistinguishable from a
    complete one, and ``callers X`` returning nothing reads as "X has no callers".
    Recording it lets the query layer say "I may not know" instead of "there are none".
    """
    # Most-wanted headers first: this is the actionable part for the user.
    top_missing = sorted(missing.items(), key=lambda kv: -kv[1])[:5]
    health: dict[str, Any] = {
        "total": total,
        "degraded": len(degraded),
        "compile_db": had_compile_db,
        "top_missing": top_missing,
        "sample": degraded[:10],
        # Informational: a pip-installed libclang ships no libc headers, so these
        # are usually absent. They do not degrade extraction (verified on cFS).
        "missing_system_headers": sorted(missing_system)[:8],
    }
    store.set_parse_health(health)
    if not degraded:
        return
    top = ", ".join(f"{h} ({n})" for h, n in top_missing)
    log.warning(
        "clang: %d/%d C/C++ file(s) are missing PROJECT headers — symbols and the call "
        "map are INCOMPLETE for them (types may show as `int`, calls may be absent). "
        "Most-missed: %s.%s",
        len(degraded), total, top,
        "" if had_compile_db else " No compile_commands.json was used; point secagent at "
        "one via affordances.clang_compile_db for full fidelity.",
    )


def _index_csharp(root: Path, records, cfg, store: AffordanceStore,
                  refresh: bool = False) -> tuple[int, int]:
    """Parse C# files with tree-sitter to extend the inter-file call map (.NET analog
    of the clang pass).

    Returns (n_methods, n_call_edges). No-op (0, 0) when tree-sitter / the C# grammar
    is unavailable, so the index still succeeds with regex symbols + the LLM. Each
    file's parse is cached by content hash (``refresh=True`` bypasses it). C# edges are
    merged into the existing call map rather than replacing it, so a mixed C/C++/C#
    repo keeps both.
    """
    if not csharp_available():
        return 0, 0
    sources = [r for r in records if r.language == "C#"
               and Path(r.path).suffix.lower() == ".cs"
               and not is_generated_csharp(r.path)]
    if not sources:
        return 0, 0
    total = len(sources)
    step = progress_step(total)
    log.info("tree-sitter: parsing %d C# file(s)…", total)
    all_funcs: list = []
    all_calls: list = []
    for i, rec in enumerate(sources, start=1):
        key = ContentCache.make_key(rec.sha256, CSHARP_PARSE_VERSION)
        unit = None
        if not refresh:
            hit = store.cache.get(key)
            if hit is not None:
                unit = unit_from_cache(hit)
        if unit is None:
            unit = parse_csharp_file(root / rec.path, rec.path)
            if unit.parsed:
                store.cache.put(key, unit_to_cache(unit))
        if i % step == 0 or i == total:
            log.info("  tree-sitter: %d/%d files (%d methods)", i, total, len(all_funcs))
        if not unit.parsed:
            continue
        all_funcs.extend(unit.functions)
        all_calls.extend(unit.calls)
    if not all_funcs:
        return 0, 0
    edges = resolve_calls(all_calls, build_definition_table(all_funcs),
                          include_intra_file=True,
                          local_defs=build_local_definitions(all_funcs))
    if edges:
        existing = store.load_call_edges()
        seen = {e.key() for e in existing}
        store.set_call_map(existing + [e for e in edges if e.key() not in seen])
    log.info("tree-sitter: %d C# methods, %d inter-file call edges", len(all_funcs), len(edges))
    return len(all_funcs), len(edges)


def _index_rust(root: Path, records, cfg, store: AffordanceStore,
                refresh: bool = False) -> tuple[int, int]:
    """Parse Rust files with tree-sitter to extend the inter-file call map (the Rust
    analog of the clang / C# passes).

    Returns (n_functions, n_call_edges). No-op (0, 0) when tree-sitter / the Rust grammar
    is unavailable, so the index still succeeds with regex symbols + the LLM. Each file's
    parse is cached by content hash (``refresh=True`` bypasses it). Rust edges are merged
    into the existing call map rather than replacing it, so a mixed-language repo keeps
    every language's edges.
    """
    if not rust_available():
        return 0, 0
    sources = [r for r in records if r.language == "Rust"
               and Path(r.path).suffix.lower() == ".rs"]
    if not sources:
        return 0, 0
    total = len(sources)
    step = progress_step(total)
    log.info("tree-sitter: parsing %d Rust file(s)…", total)
    all_funcs: list = []
    all_calls: list = []
    for i, rec in enumerate(sources, start=1):
        key = ContentCache.make_key(rec.sha256, RUST_PARSE_VERSION)
        unit = None
        if not refresh:
            hit = store.cache.get(key)
            if hit is not None:
                unit = unit_from_cache(hit)
        if unit is None:
            unit = parse_rust_file(root / rec.path, rec.path)
            if unit.parsed:
                store.cache.put(key, unit_to_cache(unit))
        if i % step == 0 or i == total:
            log.info("  tree-sitter: %d/%d files (%d functions)", i, total, len(all_funcs))
        if not unit.parsed:
            continue
        all_funcs.extend(unit.functions)
        all_calls.extend(unit.calls)
    if not all_funcs:
        return 0, 0
    edges = resolve_calls(all_calls, build_definition_table(all_funcs),
                          include_intra_file=True,
                          local_defs=build_local_definitions(all_funcs))
    if edges:
        existing = store.load_call_edges()
        seen = {e.key() for e in existing}
        store.set_call_map(existing + [e for e in edges if e.key() not in seen])
    log.info("tree-sitter: %d Rust functions, %d inter-file call edges",
             len(all_funcs), len(edges))
    return len(all_funcs), len(edges)


def _read_text(fpath: Path) -> str | None:
    try:
        return fpath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def purge_store(path: str | Path, settings: Settings) -> dict[str, Any]:
    """Securely delete the affordance store for a repo (CMMC-2 / MP.3.8.3).

    Overwrites each file then removes the store directory. Returns a report. Note:
    overwrite-in-place is best-effort on CoW/SSD media; volume encryption remains the
    primary at-rest control.
    """
    import shutil

    from ..security import secure_delete_file

    root = Path(path).resolve()
    sd = Path(settings.affordances.store_dir)
    store_dir = sd if sd.is_absolute() else root / sd
    existed = store_dir.exists()
    removed = 0
    if existed:
        for p in sorted(store_dir.rglob("*")):
            if p.is_file():
                secure_delete_file(p)
                removed += 1
        shutil.rmtree(store_dir, ignore_errors=True)
    return {"store_dir": str(store_dir), "existed": existed, "removed_files": removed}
