"""Component language binning (from the cFS docs review).

A component's primary language must be its dominant *code* language, so a C application
that ships many JSON tables (cFS apps/fm: 67 C/.h vs 128 JSON) is binned as "C" and gets
the C toolchain — not mislabeled "JSON" by a raw file-count vote. Components with no code
fall back to the overall plurality.
"""

from __future__ import annotations

from secagent.affordances.io_map import build_components
from secagent.affordances.models import FileRecord


def _rec(path: str, lang: str) -> FileRecord:
    return FileRecord(path=path, language=lang, size=10, sha256="x", loc=5)


def test_code_app_with_more_data_files_bins_as_code():
    recs = [_rec(f"apps/fm/tables/t{i}.json", "JSON") for i in range(128)]
    recs += [_rec(f"apps/fm/fsw/src/fm_{i}.c", "C") for i in range(28)]
    recs += [_rec(f"apps/fm/fsw/inc/fm_{i}.h", "C") for i in range(39)]
    comps = {c.name: c for c in build_components(recs, depth=2)}
    assert comps["apps/fm"].language == "C"  # not "JSON", despite JSON outnumbering C


def test_docs_only_component_falls_back_to_plurality():
    recs = [_rec(f"cfe/docs/g{i}.md", "Markdown") for i in range(5)]
    recs += [_rec("cfe/docs/data.json", "JSON")]
    comps = {c.name: c for c in build_components(recs, depth=2)}
    assert comps["cfe/docs"].language == "Markdown"  # no code → overall plurality
