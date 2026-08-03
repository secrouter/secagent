"""Per-file summaries.

A summary is the compact stand-in for a file in agent context: its purpose, key
symbol signatures, and IO signals. The structural parts are always heuristic; the
one-line *purpose* is heuristic by default and optionally refined by the LLM (cached
by content hash so it is computed at most once per file version).
"""

from __future__ import annotations

import ast
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from ..llm.client import LLMClient
from ..sanitize import harden_system_prompt, wrap_untrusted
from .cache import ContentCache
from .models import FileSummary, Symbol
from .signals import (
    find_datastores,
    find_endpoints,
    find_env_vars,
    find_messaging,
    find_outbound,
    find_sockets,
    strip_comments,
)
from .symbols import python_imports, rust_imports

log = logging.getLogger(__name__)

_COMMENT_RE = re.compile(r"^\s*(?:#|//|/\*|\*)\s?(.*)")

# Comment lines that are boilerplate, not a description of the file's purpose: license /
# copyright / SPDX banners (which open most C/C++ headers) and include-guard directives.
# The first *meaningful* comment in a header is very often the copyright line.
_PURPOSE_JUNK_RE = re.compile(
    r"copyright|\bspdx\b|licen[sc]e|all rights reserved|permission is hereby|"
    r"redistribution and use|this file (?:is part of|was)|www\.|https?://|"
    r"^(?:ifndef|ifdef|define|endif|pragma once|include)\b",
    re.IGNORECASE,
)

# A license notice is a *block*, and its first line often carries no license keyword at
# all — NASA/cFS headers open with "NASA Docket No. GSC-19,200-1, ..." and only mention
# copyright three lines later. Filtering line-by-line therefore lets the first line
# through and every file in the repo gets that as its "purpose". So: if a contiguous
# comment block contains any of these markers ANYWHERE, discard the whole block.
_LICENSE_BLOCK_RE = re.compile(
    r"copyright|\bspdx\b|licen[sc]e|all rights reserved|permission is hereby|"
    r"redistribution and use|\bdocket\b|united states government|"
    r"warranties or conditions|distributed under|"
    r"government as represented by|apache|\bgnu\b|\bmit license\b",
    re.IGNORECASE,
)


# Doxygen/Javadoc tags. Two kinds, handled differently:
#  - structural tags whose whole line is metadata (`@ingroup fm`, `@param x ...`) -> drop
#  - descriptive tags that PREFIX real prose (`@brief Does X`, or a bare `@file`) -> strip
#    the tag and keep what follows (a bare `@file` reduces to empty and is then skipped).
_DOXY_DROP_LINE_RE = re.compile(
    r"^[@\\](?:ingroup|defgroup|addtogroup|param|retval|returns?|throws?|"
    r"author|date|version|since|see|sa|note|warning|copyright)\b", re.IGNORECASE)
_DOXY_PREFIX_RE = re.compile(r"^[@\\](?:file|brief|details|short)\b\s*", re.IGNORECASE)

# C#/VB XML documentation comments. The convention puts the tag on its own line:
#     /// <summary>
#     /// Represents an item ordered from the catalog.
#     /// </summary>
# so stripping `///` leaves a bare `<summary>` — which was then taken as the file's
# purpose. Measured on eShop: 5 of 5 spot-checked files had the literal string
# "<summary>" as their description, including the ones a developer needs first.
_XML_DOC_TAG_ONLY_RE = re.compile(r"^</?\w+(?:\s[^>]*)?/?>$")
_XML_DOC_WRAPPED_RE = re.compile(r"^<(\w+)(?:\s[^>]*)?>(.*)</\1>$", re.DOTALL)

# C/C++ preprocessor directives — code, not comment prose (see _comment_blocks).
_C_PREPROC_RE = re.compile(
    r"^\s*#\s*(?:ifndef|ifdef|define|endif|include|pragma|if|else|elif|undef)\b")


def _is_own_filename(content: str, rel_path: str) -> bool:
    """True when a comment line is just the file restating its own name."""
    base = rel_path.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    candidate = content.strip().rstrip(":").strip().lower()
    return candidate in {base.lower(), stem.lower()}


_MAX_NAMED_DEFINITIONS = 6


def _top_level_definitions(tree: ast.Module) -> list[str]:
    """Public classes and functions defined at module level, classes first, each named
    once.

    Classes lead because they are what a module is usually *for*; a private helper says
    nothing about why the file exists.

    A name is listed once no matter how many times it is defined. `@t.overload` stub
    groups made this line spend its cap on duplicates: real Click's `termui.py` read
    "defining hidden_prompt_func, prompt, prompt, prompt, confirm and get_pager_file" —
    three of six slots on one name, so the sentence never mentioned `style`, `secho`,
    `edit` or `launch`, arguably the module's most-used API. The symbol-extraction path
    that feeds `api.rst` was fixed for exactly this and this one was not.

    Deduplicating by name rather than detecting the `@overload` decorator is deliberate:
    it costs nothing and also covers conditional redefinition (`if TYPE_CHECKING:`,
    platform branches), which decorator-matching would miss. This is a list of names, so
    which definition a name came from does not change the output.
    """
    classes, funcs = [], []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and not node.name.startswith("_"):
            funcs.append(node.name)
    seen: set[str] = set()
    unique: list[str] = []
    for name in classes + funcs:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique[:_MAX_NAMED_DEFINITIONS]


def _join_names(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _clean_comment(raw: str) -> str:
    """Strip comment syntax from one line of comment text.

    `!` and `/` are included because they are DOC markers, not prose: Rust writes inner
    docs as `//!` / `/*!` and outer docs as `///`, so leaving them attached puts a stray
    `!` at the front of the extracted purpose.
    """
    return raw.strip().lstrip("*-=/#! ").rstrip("*/ ").strip()


def _comment_blocks(text: str, max_lines: int = 60) -> list[list[str]]:
    """Split the head of a file into contiguous comment blocks (decoded comment text).

    A blank line or any code line ends a block, so a license notice and the real
    file-header comment below it are separate blocks and can be judged independently.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    in_block = False   # inside an unterminated /* ... */
    for line in text.splitlines()[:max_lines]:
        # `_COMMENT_RE` accepts a leading `#` (Python/shell comments), which in C also
        # matches preprocessor directives. Those are CODE: treating them as comment text
        # would glue an include-guard onto the license block above it and swallow the
        # real header description with it. So they end a block.
        if not in_block and _C_PREPROC_RE.match(line):
            if current:
                blocks.append(current)
                current = []
            continue
        if in_block:
            # Continuation lines inside /* ... */ carry no prefix of their own, so a
            # purely line-oriented scan ended the block at the delimiter and never saw
            # the text. Rust's idiomatic module docs are written exactly this way
            # (`/*!` then bare prose), which left every module in a crate with no usable
            # purpose; plain C block comments without leading `*` lost theirs too.
            body, closed = (line.split("*/", 1)[0], True) if "*/" in line else (line, False)
            in_block = not closed
            cleaned = _clean_comment(body)
            if not cleaned and not closed:
                # A blank line still separates blocks, so a license notice and the
                # description below it stay independently judgeable.
                if current:
                    blocks.append(current)
                    current = []
                in_block = False
                continue
            current.append(cleaned)
            continue
        m = _COMMENT_RE.match(line)
        if m:
            stripped = line.lstrip()
            if stripped.startswith("/*") and "*/" not in stripped:
                in_block = True
            current.append(_clean_comment(m.group(1)))
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks

# Bump when the LLM summary prompt changes, to invalidate cached summaries.
_SUMMARY_PROMPT_VERSION = "v1"

# Prose/documentation languages: their text is narrative, so mining it for runtime-wiring
# signals (endpoints, brokers, datastores, env vars) produces false positives — e.g. a
# README that merely *mentions* PyZMQ would otherwise attribute ZeroMQ to its component.
# Code and structured config (YAML/JSON/TOML/Build) are still scanned.
_DOC_LANGS = {"Markdown", "reStructuredText"}


def heuristic_purpose(rel_path: str, text: str, language: str, symbols: list[Symbol]) -> str:
    """Derive a one-line purpose without calling a model."""
    if language == "Python":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            doc = ast.get_docstring(tree)
            if doc:
                return _first_sentence(doc)
            # No module docstring. Do NOT fall through to scavenging comments: Python
            # puts its documentation in docstrings, so any comment found further down is
            # about a local detail. Click's core.py — the module defining Command, Group,
            # Context, Option and Argument — was described as "Reserved storage name of
            # the automatic help option", a remark about an internal constant, stated
            # with complete confidence.
            #
            # The module's own top-level definitions are a purpose that cannot be wrong,
            # and they name exactly what a reader is looking for: `testing.py` should
            # mention CliRunner, `utils.py` should mention echo.
            defined = _top_level_definitions(tree)
            if defined:
                return f"Python module defining {_join_names(defined)}."
    # First meaningful comment near the top of the file, judged BLOCK by block: a license
    # notice is discarded whole (its first line may carry no license keyword), then within
    # a surviving block we skip decorative banner lines — rows of *, =, -, etc. with no
    # words — and per-line junk such as include guards.
    for block in _comment_blocks(text):
        joined = " ".join(block)
        if _LICENSE_BLOCK_RE.search(joined):
            continue  # whole-block boilerplate (license/copyright notice)
        for content in block:
            if _DOXY_DROP_LINE_RE.match(content):
                continue  # structural Doxygen metadata, never a description
            if _XML_DOC_TAG_ONLY_RE.match(content):
                continue  # a bare <summary> / </summary> line is markup, not prose
            if _is_own_filename(content, rel_path):
                # Embedded C headers conventionally open with the file's own name
                # ("fm_app.c"), which then became the description — telling a reader
                # exactly nothing while looking like a real answer.
                continue
            wrapped = _XML_DOC_WRAPPED_RE.match(content)
            if wrapped:
                content = wrapped.group(2).strip()
            content = _DOXY_PREFIX_RE.sub("", content).strip()
            if not content or not any(ch.isalnum() for ch in content):
                continue
            if _PURPOSE_JUNK_RE.search(content):
                continue
            return _first_sentence(content)
    # Fall back to describing the top-level symbols.
    names = [s.name for s in symbols if s.kind in {"class", "function"}][:4]
    if names:
        return f"Defines {', '.join(names)}."
    return f"{language} file {rel_path}."


_MAX_KEY_SYMBOLS = 12


def _key_symbols(symbols: list[Symbol]) -> list[str]:
    """A bounded sample of a file's symbols, which SAYS when it is a sample.

    The cap keeps summaries context-frugal, but a silent one is actively misleading:
    both devs in an agentic test run treated a 12-entry list as a file's complete API
    and missed the very functions they were looking for (fm_cmds.c has 20; the 8 hidden
    ones were the directory commands one of them needed). Naming the omission costs one
    line and points at the affordance that gives the full list.
    """
    names = [s.signature or s.name for s in symbols if s.kind != "constant"]
    if len(names) <= _MAX_KEY_SYMBOLS:
        return names
    hidden = len(names) - _MAX_KEY_SYMBOLS
    return [*names[:_MAX_KEY_SYMBOLS],
            f"… +{hidden} more not shown (use `secagent affordance functions <file>`)"]


def summarize_file(
    rel_path: str,
    text: str,
    language: str,
    symbols: list[Symbol],
    *,
    llm: LLMClient | None = None,
    cache: ContentCache | None = None,
    content_sha: str = "",
    refresh_summaries: bool = False,
) -> FileSummary:
    if language == "Python":
        imports = python_imports(text)
    elif language == "Rust":
        imports = rust_imports(text)
    else:
        imports = []
    # Don't mine prose for runtime-wiring signals (see _DOC_LANGS).
    scan_signals = language not in _DOC_LANGS
    # Strip comments first so a commented-out or documented endpoint/URL/broker doesn't
    # register as real wiring (string-literal aware, so genuine URLs survive).
    signal_text = strip_comments(text) if scan_signals else ""
    summary = FileSummary(
        path=rel_path,
        purpose=heuristic_purpose(rel_path, text, language, symbols),
        key_symbols=_key_symbols(symbols),
        imports=imports,
        endpoints=find_endpoints(signal_text) if scan_signals else [],
        outbound_calls=find_outbound(signal_text) if scan_signals else [],
        env_vars=find_env_vars(signal_text) if scan_signals else [],
        datastores=find_datastores(signal_text) if scan_signals else [],
        messaging=find_messaging(signal_text) if scan_signals else [],
        sockets=find_sockets(signal_text) if scan_signals else [],
        source="heuristic",
    )
    summary.side_effects = _infer_side_effects(summary)

    if llm is not None:
        refined = _llm_purpose(rel_path, text, language, summary, llm, cache, content_sha,
                               refresh=refresh_summaries)
        if refined:
            summary.purpose = refined
            summary.source = "llm"
    return summary


def _infer_side_effects(summary: FileSummary) -> list[str]:
    effects: list[str] = []
    if summary.endpoints:
        effects.append(f"exposes {len(summary.endpoints)} HTTP endpoint(s)")
    if summary.outbound_calls:
        effects.append("makes outbound network calls")
    if summary.datastores:
        effects.append("accesses " + ", ".join(summary.datastores))
    if summary.messaging:
        effects.append("uses messaging: " + ", ".join(summary.messaging))
    if summary.sockets:
        effects.append("uses sockets: " + ", ".join(summary.sockets))
    if summary.env_vars:
        effects.append(f"reads {len(summary.env_vars)} env var(s)")
    return effects


def _first_sentence(text: str) -> str:
    text = " ".join(text.strip().split())
    for sep in (". ", "\n"):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    return text[:200]


_FN_DOC_VERSION = "fn-v1"


def describe_functions(
    rel_path: str,
    text: str,
    symbols: list[Symbol],
    *,
    llm: LLMClient,
    cache: ContentCache | None = None,
    content_sha: str = "",
    refresh: bool = False,
    workers: int = 4,
    timeout: float | None = None,
) -> dict[str, str]:
    """One-line LLM descriptions of each function ('what it does').

    Cached per (content hash, function name, model) so each function is described at
    most once per (file version, model) — switching models regenerates them, and each
    model's output is kept separately for comparison. ``refresh=True`` ignores the
    cache on read (still writes it), to re-evaluate the same model. Used by the docs
    build, scoped to the documented files.
    """
    lines = text.splitlines()
    out: dict[str, str] = {}
    pending: list[tuple[Symbol, str | None, str]] = []

    # Resolve the cache serially first: a hit costs nothing and must not occupy a worker.
    for s in symbols:
        if s.kind not in ("function", "method"):
            continue
        key = None
        if cache is not None and content_sha:
            key = ContentCache.make_key(content_sha, f"{_FN_DOC_VERSION}:{s.name}",
                                        llm.config.model)
            if not refresh:
                hit = cache.get(key)
                if hit and "doc" in hit:
                    out[s.name] = hit["doc"]
                    continue
        start = max(0, s.lineno - 1)
        pending.append((s, key, "\n".join(lines[start:start + 40])))

    cached = len(out)
    if not pending:
        if cached:
            log.info("describe_functions: %s — all %d from cache", rel_path, cached)
        return out
    # Say what is cached versus what will be paid for. Two things follow from it: the
    # user can see progress at all, and — because each description is cached the moment
    # it lands — they can tell that killing the run is safe. A re-run costs zero calls
    # for work already done; a run killed halfway resumes from exactly where it stopped.
    log.info("describe_functions: %s — %d cached, %d to generate",
             rel_path, cached, len(pending))

    sys_prompt = harden_system_prompt(
        "You describe what ONE function does in a single concise sentence. State "
        "its responsibility/effect; do not restate the signature or list callees."
    )

    def _describe(item: tuple[Symbol, str | None, str]) -> tuple[Symbol, str | None, str]:
        s, key, body = item
        user = (
            f"Function `{s.signature or s.name}` in {rel_path}.\n"
            f"Source (untrusted):\n{wrap_untrusted(body, 'source')}\n"
            "One-sentence description:"
        )
        try:
            resp = llm.chat(
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": user}],
                # No local cap. 768 was chosen as "generous" before anyone measured:
                # the deployed Gemma spends 13-15k output tokens on reasoning before
                # emitting a character, so EVERY call burned a doomed attempt and then
                # retried. That is what made a docs build sit for 28 minutes on a
                # 20-file repo without producing a single page.
                timeout=timeout,
            )
            return s, key, _first_sentence(resp.content) if resp.content else ""
        except Exception as exc:  # noqa: BLE001 - one function must not kill the build
            log.warning("describe_functions: %s in %s failed: %s: %s",
                        s.name, rel_path, exc.__class__.__name__, exc)
            return s, key, ""

    # One model call per function, run concurrently. Measured against the deployed
    # endpoint, four concurrent requests finish in roughly half the sequential time; a
    # docs build makes hundreds of these calls, so this is the difference between a
    # coffee break and an overnight run. Cache writes happen here on the caller's
    # thread once results are back.
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for s, key, doc in pool.map(_describe, pending):
            if doc:
                out[s.name] = doc
                if key is not None and cache is not None:
                    cache.put(key, {"doc": doc})
    return out


def _llm_purpose(
    rel_path: str,
    text: str,
    language: str,
    summary: FileSummary,
    llm: LLMClient,
    cache: ContentCache | None,
    content_sha: str,
    *,
    refresh: bool = False,
) -> str | None:
    key = None
    if cache is not None and content_sha:
        # Model is part of the key so a different model regenerates the summary (and
        # each model's output is cached separately for A/B comparison).
        key = ContentCache.make_key(content_sha, _SUMMARY_PROMPT_VERSION, llm.config.model)
        if not refresh:
            hit = cache.get(key)
            if hit and "purpose" in hit:
                return hit["purpose"]

    # Keep the prompt small — the heuristic context plus a head of the file is enough
    # for a one-line purpose, and respects small local-model windows.
    head = "\n".join(text.splitlines()[:80])
    sys = harden_system_prompt(
        "You summarize a source file in ONE sentence describing its responsibility. "
        "Be concrete and specific. Do not list functions; state the file's role."
    )
    user = (
        f"File: {rel_path} ({language})\n"
        f"Key symbols: {', '.join(summary.key_symbols[:8])}\n"
        f"Source (untrusted):\n{wrap_untrusted(head, 'source')}\n"
        "One-sentence purpose:"
    )
    try:
        resp = llm.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            # Headroom for reasoning models: they spend output tokens on hidden
            # reasoning before emitting any content, so a tight cap yields empty content
            # and silently falls back to the heuristic. 768 was not headroom — measured,
            # this model needs 13-15k — so the client's budget applies instead. We keep
            # only the first sentence below, so a large cap costs nothing either way.
        )
    except Exception:
        return None
    purpose = _first_sentence(resp.content) if resp.content else None
    if purpose and key is not None and cache is not None:
        cache.put(key, {"purpose": purpose})
    return purpose
