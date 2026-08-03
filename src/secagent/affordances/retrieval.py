"""Budget-aware context assembly.

Given a token budget sized to the deployed Gemma window, select the *smallest*
affordance set that answers the task: the project outline, the IO summary, and the
most relevant file summaries/symbols — never raw files unless explicitly requested.
This is the mechanism that keeps local-model prompts small.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..llm.tokenizer import TokenCounter
from .io_map import summarize_io
from .models import FileSummary
from .store import AffordanceStore

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

# How much a file DEFINING a queried name outweighs merely mentioning it. Large enough
# to dominate prose term-frequency: the defining file should come first, deterministically.
_DEFINES_WEIGHT = 100
_STEM_WEIGHT = 25

# Ceiling on file blocks when the caller does not specify one; the token budget is the
# real constraint below this. Sized so a large local window (100k+) can actually be used
# without turning a focused answer into a repository dump.
_MAX_CONTEXT_FILES = 60


@dataclass
class ContextBlock:
    title: str
    text: str
    tokens: int


class ContextBuilder:
    def __init__(self, store: AffordanceStore, budget_tokens: int,
                 counter: TokenCounter | None = None, max_files: int = 0) -> None:
        self.store = store
        self.budget = budget_tokens
        self.counter = counter or TokenCounter()
        self.max_files = max_files if max_files > 0 else _MAX_CONTEXT_FILES

    def overview(self) -> str:
        """Project outline + IO summary, trimmed to the budget."""
        blocks = self._overview_blocks()
        return self._pack(blocks, self.budget)

    def for_query(
        self, query: str, max_files: int = 0, extra_paths: list[str] | None = None
    ) -> str:
        """Overview plus the file summaries most relevant to ``query``.

        ``max_files=0`` lets the token budget decide. It used to default to a fixed 12,
        which quietly became the *only* constraint: pointed at a server serving 117,976
        tokens, raising the window 14x grew the assembled block by 7% because both runs
        stopped at twelve files. ``_pack`` already enforces the budget, so the cap only
        ever prevented us from using a budget we had been given.
        """
        blocks = self._overview_blocks()
        # Reserve roughly 40% of budget for the overview, rest for relevant files.
        overview_text = self._pack(blocks, int(self.budget * 0.4))
        spent = self.counter.count(overview_text)
        remaining = max(0, self.budget - spent)

        # Still bounded. The point of this module is the SMALLEST context that answers
        # the task, not the largest that fits — a ceiling keeps a huge window from
        # burying the relevant files under marginal ones, and ranking decides the order.
        limit = max_files if max_files > 0 else self.max_files
        ranked = self.rank_summaries(query, extra_paths=extra_paths)[:limit]
        # Same reason as in rank_summaries: key_symbols is a capped, constants-excluding
        # display field, so a macro-only header would rank correctly here and then show
        # no symbols at all. Fetch the real index once and share it.
        symbols_by_file = self.store.symbol_names_by_file()
        file_blocks = [self._summary_block(s, symbols_by_file.get(s.path, []))
                       for s in ranked]
        files_text = self._pack(file_blocks, remaining)
        return overview_text + ("\n\n" + files_text if files_text else "")

    def rank_summaries(self, query: str, extra_paths: list[str] | None = None) -> list[FileSummary]:
        raw_terms = set(_WORD_RE.findall(query))
        terms = {t.lower() for t in raw_terms}
        summaries = self.store.all_summaries()
        # The REAL symbol index, not the summary's key_symbols: that field is capped at
        # 12 and excludes constants, so ranking against it hid every macro-only header —
        # measured at 17 of 53 files on a cFS app. The symbol table has them all.
        symbols_by_file = self.store.symbol_names_by_file()
        extra = set(extra_paths or [])

        # Build each file's haystack once, then weight terms by INVERSE DOCUMENT
        # FREQUENCY. Raw term frequency lets the common words of a natural-language
        # question drown the one word that identifies the answer: asking "how do I add a
        # new Unicore message type to this parser?" ranked an unrelated protocol's files
        # top because "message"/"type"/"parser" appear in every driver, while "unicore"
        # — present in only a few — counted the same as each of them.
        haystacks = {
            path: " ".join(
                [path, s.purpose, " ".join(symbols_by_file.get(path, [])),
                 " ".join(s.endpoints)]
            ).lower()
            for path, s in summaries.items()
        }
        n_docs = max(1, len(haystacks))
        weight = {}
        for t in terms:
            df = sum(1 for h in haystacks.values() if t in h)
            # BM25's IDF: a term in *every* file lands at ~0, so the filler words of a
            # natural-language question ("how", "add", "this") stop voting entirely,
            # while a term unique to a few files carries the ranking.
            weight[t] = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))

        scored: list[tuple[float, FileSummary]] = []
        for path, s in summaries.items():
            names = symbols_by_file.get(path, [])
            haystack = haystacks[path]
            # Sublinear term frequency: the 50th mention of "message" says far less
            # than the 1st. Without this a merely *large* file outranks the right one on
            # volume alone.
            score = sum(
                (1 + math.log(c)) * weight[t]
                for t in terms
                if (c := haystack.count(t))
            )
            # A file that DEFINES a name the query asked for is the answer, not merely a
            # mention of it. Without this, raw term frequency lets the generic words in
            # "APP_FILE_INFO_TLM_MID macro definition" outvote the one term that
            # identifies the file uniquely.
            #
            # Matched CASE-SENSITIVELY on purpose. Someone naming a symbol types its case
            # ("APP_FILE_INFO_TLM_MID", "AppGetFileInfoCmd_t"); someone writing prose does
            # not. A case-insensitive match made the ordinary word "user" in "user database
            # sqlite" claim the bonus for a class named `User`, outranking the file the
            # query was actually about.
            score += _DEFINES_WEIGHT * sum(1 for n in names if n in raw_terms)
            # Naming a FILE after something is as deliberate a signal as defining a
            # symbol: `unicore.cpp` is about Unicore in a way no amount of prose
            # matching proves. Scaled by the same IDF weight, so a generic stem
            # ("test", "types", "parser") that every repo has earns ~nothing while a
            # distinctive one carries real weight. That scaling is what keeps this from
            # repeating the loose-match mistake the DEFINES comment above describes.
            stem_tokens = set(_WORD_RE.findall(path.rsplit("/", 1)[-1].lower()))
            score += _STEM_WEIGHT * sum(weight[t] for t in terms & stem_tokens)
            # Explicitly requested paths (e.g. a diff's files) always rank highly.
            if path in extra:
                score += 1000
            if score > 0:
                scored.append((score, s))
        scored.sort(key=lambda x: (-x[0], x[1].path))
        return [s for _, s in scored]

    # -- internals -----------------------------------------------------------
    def _overview_blocks(self) -> list[ContextBlock]:
        blocks: list[ContextBlock] = []
        pm = self.store.load_project_map()
        if pm:
            blocks.append(self._block("Project structure", pm.outline()))
        edges = self.store.load_io_edges()
        if edges:
            blocks.append(self._block("IO map", summarize_io(edges)))
        return blocks

    def _summary_block(self, s: FileSummary, symbol_names: list[str] | None = None
                       ) -> ContextBlock:
        lines = [f"### {s.path}", s.purpose]
        # Prefer key_symbols (signatures — richer) but fall back to the real symbol
        # names, so files whose entire content is constants still show what they define.
        shown = s.key_symbols[:8] or (symbol_names or [])[:8]
        if shown:
            lines.append("symbols: " + "; ".join(shown))
        if s.endpoints:
            lines.append("endpoints: " + ", ".join(s.endpoints))
        if s.datastores:
            lines.append("datastores: " + ", ".join(s.datastores))
        return self._block(s.path, "\n".join(lines))

    def _block(self, title: str, text: str) -> ContextBlock:
        return ContextBlock(title, text, self.counter.count(text))

    def _pack(self, blocks: list[ContextBlock], budget: int) -> str:
        out: list[str] = []
        used = 0
        for b in blocks:
            if used + b.tokens > budget:
                # Try a truncated version so we include *something* from the block.
                room = budget - used
                if room > 50:
                    out.append(self._truncate(b.text, room))
                break
            out.append(b.text)
            used += b.tokens
        return "\n\n".join(out)

    def _truncate(self, text: str, token_budget: int) -> str:
        lines = text.splitlines()
        kept: list[str] = []
        used = 0
        for line in lines:
            t = self.counter.count(line) + 1
            if used + t > token_budget:
                kept.append("… (truncated)")
                break
            kept.append(line)
            used += t
        return "\n".join(kept)
