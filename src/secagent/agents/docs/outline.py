"""Documentation information architecture + content generation.

Builds the page set from affordances. Structural facts (components, symbols, IO) are
rendered deterministically; narrative prose is written by the LLM from a
budget-bounded affordance context, with a heuristic fallback when no model is
reachable. This keeps output useful whether or not a Gemma endpoint is available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ...affordances.io_map import summarize_io
from ...affordances.languages import detect_language
from ...affordances.store import AffordanceStore
from ...llm.client import LLMClient
from ...llm.tokenizer import TokenCounter
from ...sanitize import harden_system_prompt, wrap_untrusted


@dataclass
class Page:
    filename: str  # e.g. "overview.rst"
    title: str
    body: str  # reStructuredText (without the title header)


def _h(title: str, char: str = "=") -> str:
    return f"{title}\n{char * len(title)}\n"


# RST inline-markup characters that, left raw, cause stray emphasis, broken roles, or
# -W build failures. Model descriptions and code signatures are arbitrary text, so they
# must be neutralized before going into reStructuredText.
_RST_INLINE_SPECIAL = re.compile(r"([\\*`|_])")


def _rst_escape(text: str) -> str:
    """Backslash-escape RST inline markup so arbitrary text renders literally."""
    return _RST_INLINE_SPECIAL.sub(r"\\\1", text)


def _rst_literal(text: str) -> str:
    """Render text as an inline literal, neutralizing backticks that would end it early."""
    return "``" + text.replace("`", "'") + "``"


# A line that is only adornment punctuation reads as an RST transition or section
# underline; a line beginning ``.. ``/``:: `` reads as a directive/comment. Either can
# fail a -W Sphinx build when it turns up inside model prose.
_RST_ADORNMENT_RE = re.compile(r"^[=\-~^\"'`#*+.:_]{2,}\s*$")

# Page caps. Both announce what they omit: a generated reference that silently stops
# reads as a COMPLETE API listing, which is how a reader concludes a symbol does not exist.
_MAX_CALLEES_PER_PAIR = 12
_MAX_API_ROWS = 300

# Symbol kinds the API reference lists. This is a whitelist, so it is worth saying what it
# leaves out and why — it silently dropped `export` for a whole release.
#
#   export   — INCLUDED. PEP 562 `__getattr__` re-exports. Excluding them was worse than
#              never indexing them: only the private `_BaseCommand` implementations were
#              listed, so the page implied Click had no public `BaseCommand` at all — the
#              name a user actually writes in `from click import BaseCommand`.
#   constant — EXCLUDED, deliberately. Measured before deciding: 16 of 454 symbols on the
#              Click fixture, but 378 of 591 on the real C++ GPS driver target, mostly
#              `#define` include guards and magic numbers. They would take two thirds of
#              the row budget and the round-robin cap would push out the functions and
#              types the page is read for.
_API_KINDS = ("function", "method", "class", "export")


def _safe_prose(text: str) -> str:
    """Make arbitrary LLM prose safe to drop into reStructuredText.

    The model is asked for plain narrative, but a stray backtick/asterisk, a Markdown
    ``---`` rule, or a line starting ``.. `` would otherwise break (or silently distort)
    a warnings-as-errors Sphinx build. Escape inline markup and neutralize block-level
    constructs; normal prose is unaffected.
    """
    out: list[str] = []
    for line in _rst_escape(text).splitlines():
        stripped = line.strip()
        if _RST_ADORNMENT_RE.match(stripped) or stripped.startswith((".. ", ":: ")):
            line = "\\" + line.lstrip()  # a leading backslash defeats the block parse
        out.append(line)
    return "\n".join(out)


def build_pages(
    store: AffordanceStore,
    *,
    diagrams: list[str],
    llm: LLMClient | None = None,
    budget_tokens: int = 2048,
    rendered_svgs: set[str] | None = None,
    call_edges: list | None = None,
    function_docs_note: str = "",
) -> list[Page]:
    counter = TokenCounter()
    pm = store.load_project_map()
    edges = store.load_io_edges()
    components = store.load_components()
    summaries = store.all_summaries()

    pages: list[Page] = []
    pages.append(_overview_page(pm, llm, counter, budget_tokens))
    pages.append(
        _architecture_page(pm, edges, diagrams, llm, counter, budget_tokens,
                           rendered_svgs or set())
    )
    pages.append(_components_page(components, summaries, llm, counter, budget_tokens))
    pages.append(_dataflow_page(edges))
    if call_edges:
        pages.append(_callmap_page(call_edges))
    pages.append(_api_reference_page(store, components, function_docs_note))
    return pages


def _callmap_page(call_edges: list) -> Page:
    """Render the inter-file call map: which file calls into which, via which functions."""
    by_pair: dict[tuple[str, str], list[str]] = {}
    for e in call_edges:
        by_pair.setdefault((e.src_file, e.dst_file), []).append(e.callee)
    parts = [
        "Calls between source files, resolved from the C/C++ AST (clang). Each edge "
        "lists the callee functions the source file invokes in the target file.", "",
    ]
    cur_src = None
    for (src, dst), callees in sorted(by_pair.items()):
        if src != cur_src:
            parts.append(_h(src, "-"))
            cur_src = src
        uniq = sorted(set(callees))
        names = ", ".join(_rst_literal(c) for c in uniq[:_MAX_CALLEES_PER_PAIR])
        if len(uniq) > _MAX_CALLEES_PER_PAIR:
            names += f", … +{len(uniq) - _MAX_CALLEES_PER_PAIR} more"
        parts.append(f"* → {_rst_literal(dst)} ({names})")
    parts.append("")
    return Page("callmap.rst", "Call Map", "\n".join(parts))


def _overview_page(pm, llm, counter, budget) -> Page:
    if pm:
        langs = ", ".join(f"{k} ({v})" for k, v in sorted(
            pm.languages.items(), key=lambda kv: -kv[1]))
        facts = (
            f"Files: {pm.file_count}\n\nLines of code: {pm.total_loc}\n\n"
            f"Languages: {langs}\n\nComponents: {len(pm.components)}\n"
        )
        context = pm.outline()
    else:
        facts, context = "", ""
    prose = _prose(
        llm, counter, budget,
        "Write a 2-3 sentence overview of what this software project is and does.",
        context,
        fallback="This document set is generated by secagent from the repository's "
                 "structure, file summaries, and inter-component IO map.",
    )
    body = f"{_safe_prose(prose)}\n\n{_h('Key facts', '-')}\n{facts}"
    return Page("overview.rst", "Overview", body)


def _architecture_page(pm, edges, diagrams, llm, counter, budget, rendered_svgs) -> Page:
    context = (pm.outline() if pm else "") + "\n\n" + summarize_io(edges)
    prose = _prose(
        llm, counter, budget,
        "Describe the high-level architecture: the main components and how they "
        "interact. 1-2 short paragraphs.",
        context,
        fallback="The architecture below is derived from detected imports, HTTP "
                 "endpoints, outbound calls, and datastore usage.",
    )
    parts = [_safe_prose(prose), ""]
    if diagrams:
        parts.append(_h("Diagrams", "-"))
        for fname in diagrams:
            stem = fname.rsplit(".", 1)[0]
            svg = f"{stem}.svg"
            if svg in rendered_svgs:
                # Pre-rendered static image — no Sphinx-time render step needed.
                parts.append(f".. image:: /_diagrams/{svg}")
                parts.append(f"   :alt: {stem}")
                parts.append("")
                parts.append(f"Editable source: :download:`{fname} </_diagrams/{fname}>`.")
            else:
                # No image was produced: keep the build working and offer the source.
                parts.append(f".. note:: Diagram **{stem}** — render unavailable.")
                parts.append(f"   Source: :download:`{fname} </_diagrams/{fname}>`.")
            parts.append("")
    return Page("architecture.rst", "Architecture", "\n".join(parts))


def _components_page(components, summaries, llm, counter, budget) -> Page:
    parts: list[str] = [
        "Each component below is a cohesive directory of the codebase.", ""
    ]
    for c in components:
        parts.append(_h(c.name, "-"))
        ctx_lines = [f"Component {c.name} ({c.language}), files:"]
        for f in c.files[:30]:
            s = summaries.get(f)
            if s:
                ctx_lines.append(f"- {f}: {s.purpose}")
        context = "\n".join(ctx_lines)
        prose = _prose(
            llm, counter, budget,
            f"Summarize the responsibility of the '{c.name}' component in 1-2 sentences.",
            context,
            fallback=f"The ``{c.name}`` component groups {len(c.files)} {c.language} file(s).",
        )
        parts.append(_safe_prose(prose))
        parts.append("")
        parts.append("Files:")
        parts.append("")
        # Omit non-code asset/binary files ("Other" language: PDFs, LICENSE, .mk, …) —
        # they only add "Other file (N bytes)" noise. They still count toward the total.
        listed = [f for f in c.files if detect_language(Path(f)) != "Other"]
        for f in listed[:50]:
            s = summaries.get(f)
            purpose = f" — {_rst_escape(s.purpose)}" if s and s.purpose else ""
            parts.append(f"* {_rst_literal(f)}{purpose}")
        hidden = len(c.files) - min(len(listed), 50)
        if hidden > 0:
            parts.append(
                f"* … and {hidden} more file(s) not shown "
                "(non-code assets, or list capped at 50)."
            )
        parts.append("")
    return Page("components.rst", "Components", "\n".join(parts))


def _dataflow_page(edges) -> Page:
    parts = ["This page lists the detected inter-component and external IO edges, "
             "including message queues / brokers and raw network sockets.", ""]

    # Dedicated messaging summary (Kafka, MQTT, RabbitMQ, ZeroMQ, …): which component
    # uses which broker, so the messaging backbone is called out, not buried in the map.
    messaging = [e for e in edges if e.kind == "messaging"]
    if messaging:
        parts.append(_h("Messaging", "-"))
        by_system: dict[str, set[str]] = {}
        for e in messaging:
            by_system.setdefault(e.dst, set()).add(e.src)
        for system, comps in sorted(by_system.items()):
            users = ", ".join(_rst_literal(c) for c in sorted(comps))
            parts.append(f"* **{_rst_escape(system)}** — used by {users}")
        parts.append("")

    # Raw sockets (TCP/UDP/Unix) — the lower-level network IO the broker list doesn't cover.
    sockets = [e for e in edges if e.kind == "socket"]
    if sockets:
        parts.append(_h("Sockets", "-"))
        by_type: dict[str, set[str]] = {}
        for e in sockets:
            by_type.setdefault(e.dst, set()).add(e.src)
        for stype, comps in sorted(by_type.items()):
            users = ", ".join(_rst_literal(c) for c in sorted(comps))
            parts.append(f"* **{_rst_escape(stype)}** — used by {users}")
        parts.append("")

    parts.append(_h("All IO edges", "-"))
    parts.append(".. code-block:: text")
    parts.append("")
    for line in summarize_io(edges).splitlines():
        parts.append(f"   {line}")
    parts.append("")
    return Page("dataflow.rst", "Data Flow & IO", "\n".join(parts))


def _cap_counts_round_robin(lengths: list[int], cap: int) -> list[int]:
    """How many symbols to keep from each file so the total is ``min(sum(lengths), cap)``.

    Spends the budget one symbol per file per round — file 0's first symbol, then file
    1's first, … then file 0's second, and so on — instead of exhausting the cap on
    whichever file comes first. That was the actual defect: rows were laid end to end
    in file order and sliced at the cap, so the cap fell in the middle of one file and
    every file after it contributed nothing at all (see the module docstring in
    ``affordances/priority.py`` for four earlier, differently-shaped versions of this
    same bug — this is the fifth, and `path_rank` does not apply here because every
    dropped file was equally-ranked library code in the same component).

    When the cap can't reach one symbol per file (``cap < len(lengths)``), the files
    earliest in ``lengths`` still win — there is no way to give a fractional symbol to
    every file, so this is the one case that still favors list order. That only bites
    when a component has more files than the row cap, which is a much narrower failure
    mode than the one this function fixes (the cap binding inside a single file).
    """
    counts = [0] * len(lengths)
    remaining = max(cap, 0)
    while remaining > 0:
        progressed = False
        for i, length in enumerate(lengths):
            if remaining <= 0:
                break
            if counts[i] < length:
                counts[i] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break  # every file's symbols are already fully included
    return counts


def _parse_health_warning(store: AffordanceStore) -> list[str]:
    """Say on the page when the signatures below may be wrong, and why.

    `ParsedUnit` has tracked `missing_project_headers` precisely so this could be said,
    and the docs never asked. The PX4 GPS drivers do not ship `definitions.h` by design —
    the parent project supplies it — so `sensor_gps_s *gps_position` was extracted as
    `int * gps_position` on every one of the seven driver constructors: the most important
    parameter of the most important method of every class in the library, printed as fact
    with a `file:line` citation. The warning existed, in the build's stderr and its JSON
    report. A reader of the generated site had no way to reach either.

    Nothing is emitted for a clean parse. A caveat on every page is one nobody reads.
    """
    health = store.parse_health()
    degraded = int(health.get("degraded") or 0)
    if not degraded:
        return []
    total = int(health.get("total") or 0)
    missing = [str(h) for h, _n in (health.get("top_missing") or [])]
    named = ", ".join(f"``{m}``" for m in missing[:3])
    detail = f" The most-missed are {named}." if named else ""
    return [
        ".. warning::",
        f"   **Signatures on this page may be wrong.** {degraded} of {total} C/C++ file(s)"
        " were parsed without headers the project expects to be supplied from outside it,"
        " so unresolved types collapse to ``int`` and some calls are missing from the call"
        f" map.{detail} A parameter shown as ``int *`` may really be a project struct"
        " pointer. This affects what was extracted, not how it was described.",
        "",
    ]


def _api_reference_page(
    store: AffordanceStore,
    components,
    function_docs_note: str = "",
    *,
    max_rows: int = _MAX_API_ROWS,
) -> Page:
    parts = ["Functions and types extracted from the codebase, with a one-line "
             "description of what each function does where one was generated.", ""]
    parts += _parse_health_warning(store)
    if function_docs_note:
        parts += [".. note::", f"   {_rst_escape(function_docs_note)}", ""]
    # Inheritance, from the same index the `affordance types` query reads. The docs
    # pipeline contained zero calls to `load_types()`: the bases were recorded, verified
    # against the store by three reviewers, and never rendered, so the generated site
    # showed the flat unrelated classes the original defect report described. Keyed by
    # (file, bare name) because a qualified name is not unique across a repo.
    bases_by_symbol = {
        (t.file, t.qualified_name.rsplit(".", 1)[-1]): t.bases
        for t in store.load_types() if t.bases
    }
    for c in components:
        # Symbols per file, in file order — files with none are dropped up front so
        # they neither take a round-robin turn nor render an empty section.
        file_rows = [
            [s for s in store.symbols_for_file(f) if s.kind in _API_KINDS]
            for f in c.files
        ]
        file_rows = [rows for rows in file_rows if rows]
        total = sum(len(rows) for rows in file_rows)
        if total == 0:
            continue
        parts.append(_h(c.name, "-"))
        # The SELECTION is round-robin (fair across files); the RENDER order stays
        # file-by-file, lineno order within each file — interleaving the listing itself
        # would make a page that keeps every module harder to read than one that drops
        # some, which is a real cost even though it no longer drops modules outright.
        counts = _cap_counts_round_robin([len(rows) for rows in file_rows], max_rows)
        for rows, n in zip(file_rows, counts, strict=True):
            # Which symbols a clipped file gives up. Round-robin alone was not enough:
            # measured against real Click, it restored `echo` and eight of the nine
            # missing modules but still dropped `CliRunner`, because `testing.py` lists
            # it 34th behind the methods of five earlier classes and a file's share of a
            # 300-row budget across ~20 files is about 15. A reference page is read to
            # find a type or a top-level function, so those come first and members of
            # types fill whatever is left. Declaration order is preserved within each
            # group, so the listing still reads top-to-bottom.
            chosen = sorted(
                enumerate(rows),
                key=lambda i_s: (i_s[1].kind == "method", i_s[0]),
            )[:n]
            for _, s in sorted(chosen, key=lambda i_s: i_s[0]):
                text = s.signature or s.name
                bases = bases_by_symbol.get((s.file, s.name)) if s.kind == "class" else None
                if bases:
                    # Rendered into the literal rather than appended as prose: a reader
                    # scanning the reference for a type wants to see what it extends in
                    # the same glance as its name. Classes with no bases are left alone —
                    # an empty `()` on every root class is new noise, not information.
                    text = f"{text}({', '.join(bases)})"
                sig = _rst_literal(text)
                desc = f" — {_rst_escape(s.doc)}" if s.doc else ""
                parts.append(f"* {sig} ({_rst_escape(s.file)}:{s.lineno}){desc}")
        kept = sum(counts)
        if total > kept:
            parts.append(f"* … and {total - kept} more symbol(s) in this "
                         "component, not listed (page limit).")
        parts.append("")
    return Page("api.rst", "API Reference", "\n".join(parts))


def _prose(
    llm: LLMClient | None,
    counter: TokenCounter,
    budget: int,
    instruction: str,
    context: str,
    *,
    fallback: str,
) -> str:
    if llm is None:
        return fallback
    # Trim context to the budget so small models are not overwhelmed.
    ctx = context
    while counter.count(ctx) > budget and "\n" in ctx:
        ctx = ctx[: ctx.rfind("\n")]
    sys = harden_system_prompt(
        "You are a precise technical writer. Use ONLY the provided context. "
        "Do not invent APIs or behavior. Write clear reStructuredText prose."
    )
    user = f"{instruction}\n\n{wrap_untrusted(ctx, 'context')}"
    try:
        resp = llm.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            # Headroom for reasoning models, which consume output tokens on hidden
            # reasoning before any prose; too tight a cap returns empty content and
            # silently falls back. Plain models stop early, so this costs nothing.
        )
        text = resp.content.strip()
        # If the model hit its output cap, the tail is a partial sentence (e.g. ending
        # mid-word at "…cfs-cosmos-"). Trim to the last complete sentence rather than
        # shipping a fragment; fall back if nothing complete was produced.
        if resp.finish_reason == "length" and text:
            m = re.search(r"^(.*[.!?])(?:\s|$)", text, re.S)
            text = m.group(1) if m else ""
        return text or fallback
    except Exception:
        return fallback
