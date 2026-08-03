"""UC1 orchestration: index → diagrams → pages → Sphinx build."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ...affordances.api import index_repo
from ...affordances.file_summary import describe_functions
from ...affordances.priority import path_rank
from ...affordances.store import AffordanceStore
from ...audit import get_audit_logger
from ...config import Settings
from ...llm.client import LLMClient
from ...security import file_hash
from .drawio_gen import build_diagrams, to_drawio_xml
from .outline import build_pages
from .render import render_diagrams
from .sphinx_gen import run_sphinx_build, write_project

log = logging.getLogger(__name__)



def build_docs(
    repo: str | Path,
    out_dir: str | Path,
    settings: Settings,
    *,
    run_sphinx: bool = True,
    reindex: bool = True,
) -> dict[str, Any]:
    """Generate a comprehensive Sphinx doc set with architecture diagrams.

    Returns a JSON-serializable report. The diagram backend (``diagrams.renderer``)
    defaults to ``svg`` (no external binary) and faithful backends fall back to it,
    so diagrams are always produced; a failing Sphinx build is reported, not raised.
    """
    repo = Path(repo).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    from ...security import enforce_fips_policy

    enforce_fips_policy(settings.fips.require_fips, settings.fips.allow_non_fips)
    if settings.affordances.llm_summaries:
        from ...netpolicy import enforce

        enforce(settings)

    report: dict[str, Any] = {"repo": str(repo), "out_dir": str(out_dir)}

    llm: LLMClient | None = None
    if settings.affordances.llm_summaries:
        llm = LLMClient(settings.llm)
    try:
        if reindex:
            report["index"] = index_repo(repo, settings, llm=llm)

        store = AffordanceStore(repo, settings.affordances.store_dir)
        try:
            components = store.load_components()
            edges = store.load_io_edges()
            from ...affordances.call_map import inter_file

            # The Call Map page renders file->file relationships.
            call_edges = inter_file(store.load_call_edges())
            diagrams = build_diagrams(components, edges)
            diagram_sources = {f"{stem}.drawio": to_drawio_xml(d)
                               for stem, d in diagrams.items()}

            # Write the editable .drawio sources first (faithful backends read them),
            # then render images with the configured backend into the same dir.
            diagrams_dir = out_dir / "source" / "_diagrams"
            diagrams_dir.mkdir(parents=True, exist_ok=True)
            for fname, xml in diagram_sources.items():
                (diagrams_dir / fname).write_text(xml, encoding="utf-8")
            log.info("Rendering diagrams (%s)…", settings.diagrams.renderer)
            render = render_diagrams(
                diagrams, diagrams_dir,
                renderer=settings.diagrams.renderer,
                chromium_path=settings.diagrams.chromium_binary,
            )
            report["render"] = {
                "backend": render.backend,
                "rendered": render.rendered,
                "skipped": render.skipped,
            }

            fn_note = ""
            if llm is not None and settings.affordances.max_function_docs:
                cap = settings.affordances.max_function_docs
                described, total_fns = _describe_functions(
                    repo, store, llm, cap,
                    refresh=settings.affordances.refresh_summaries,
                )
                report["function_docs"] = described
                if cap >= 0 and total_fns > described:
                    fn_note = (
                        f"Descriptions were generated for {described} of {total_fns} "
                        f"functions (affordances.max_function_docs = {cap}). Raise it — "
                        "or set it to -1 to describe all — to cover the rest."
                    )

            log.info("Writing pages and building the Sphinx site…")
            pages = build_pages(
                store,
                diagrams=list(diagram_sources),
                llm=llm,
                budget_tokens=settings.llm.context_budget_tokens,
                rendered_svgs=set(render.rendered),
                call_edges=call_edges,
                function_docs_note=fn_note,
            )
            project_name = _project_name(store.load_project_map(), repo)
            report["write"] = write_project(
                out_dir, project_name, pages, diagram_sources,
                banner=settings.marking.banner,
            )
            # Per-model record of the generated summaries, for evaluating models.
            report["summaries_manifest"] = _write_summaries_manifest(out_dir, store)
        finally:
            store.close()

        if run_sphinx:
            report["sphinx"] = run_sphinx_build(out_dir)
            if report["sphinx"].get("ok"):
                log.info("Docs written to %s", report["sphinx"].get("index_html", out_dir))
            else:
                log.info("Sphinx build reported a problem (see report['sphinx'])")
        else:
            report["sphinx"] = {"ok": None, "skipped": True}

        get_audit_logger(settings).record(
            "docs_build",
            target={"repo": str(repo), "out_dir": str(out_dir),
                    "sphinx_ok": report["sphinx"].get("ok")},
            model=settings.llm.model if llm is not None else "",
            endpoint=settings.llm.base_url if llm is not None else "",
            outcome="ok" if report["sphinx"].get("ok") in (True, None) else "error",
        )
        return report
    finally:
        if llm is not None:
            llm.close()


def _describe_functions(repo, store: AffordanceStore, llm: LLMClient, max_docs: int,
                        refresh: bool = False) -> tuple[int, int]:
    """Generate + persist one-line LLM descriptions for functions (real source first).

    ``max_docs`` caps the number described; ``-1`` means describe every function.
    Returns ``(described, total_candidate_functions)`` so the caller can flag (and the
    docs can note) when the cap truncated coverage.
    """
    from ...affordances.call_map import is_test_path

    repo = Path(repo)
    recs = [r for r in store.file_records() if r.n_symbols and not is_test_path(r.path)]
    # Candidate functions per file (symbols only here; source text is read lazily below).
    candidates: list[tuple] = []
    total_fns = 0
    for rec in recs:
        syms = [s for s in store.symbols_for_file(rec.path)
                if s.kind in ("function", "method")]
        if syms:
            candidates.append((rec, syms))
            total_fns += len(syms)

    # Library code before demos. The budget used to follow path order, so on Click the
    # entire 120-function allowance went to `examples/*` and never reached the library —
    # the same defect as the triage and testgen budgets. Alphabetical is not a priority.
    candidates.sort(key=lambda rc: path_rank(rc[0].path))

    unlimited = max_docs < 0
    described = 0
    log.info("Describing functions with the model (%s of %d, cached)…",
             "all" if unlimited else f"up to {max_docs}", total_fns)
    for rec, syms in candidates:
        if not unlimited and described >= max_docs:
            break
        fpath = repo / rec.path
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        take = syms if unlimited else syms[: max_docs - described]
        docs = describe_functions(
            rec.path, text, take,
            llm=llm, cache=store.cache, content_sha=file_hash(fpath), refresh=refresh,
        )
        for name, doc in docs.items():
            store.set_symbol_doc(rec.path, name, doc)
        described += len(docs)
        # Commit per file so the write lock is released between the (slow, LLM-driven)
        # files instead of held for the whole describe phase — otherwise a concurrent
        # secagent process gets "database is locked".
        store.commit()
        log.info("  described %d/%s functions (%s)",
                 described, "all" if unlimited else max_docs, rec.path)
    store.commit()
    if not unlimited and total_fns > described:
        log.info("Function descriptions capped at %d of %d — raise "
                 "affordances.max_function_docs (or set -1 for all) to describe more.",
                 max_docs, total_fns)
    return described, total_fns


def _write_summaries_manifest(out_dir: Path, store: AffordanceStore) -> dict[str, str]:
    """Write the per-model summaries manifest (JSON + Markdown) for model evaluation."""
    from ...affordances import queries

    manifest = queries.summaries_manifest(store)
    (out_dir / "summaries.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    model = manifest["model"] or "(heuristic — no model)"
    lines = [f"# Generated summaries — model: {model}", "", "## File purposes", ""]
    for path, purpose in manifest["files"].items():
        lines.append(f"- `{path}` — {purpose}")
    lines += ["", "## Function descriptions", ""]
    for path, fns in manifest["functions"].items():
        lines.append(f"### {path}")
        lines += [f"- `{name}` — {doc}" for name, doc in fns.items()]
        lines.append("")
    (out_dir / "summaries.md").write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(out_dir / "summaries.json"), "md": str(out_dir / "summaries.md")}


def _project_name(pm, repo: Path) -> str:
    return Path(pm.root).name if pm else repo.name
