"""Affordance ergonomics: representative-purpose file ranking no longer misclassifies
`readme_*` source as docs, and auto-indexing announces itself. Tier-3 review items."""

from __future__ import annotations

import json

from secagent.affordances import queries
from secagent.affordances.models import Component, FileRecord, FileSummary
from secagent.affordances.store import AffordanceStore
from secagent.config import Settings


def _rec(path: str, lang: str) -> FileRecord:
    return FileRecord(path=path, language=lang, size=1, sha256=path, loc=1, n_symbols=0)


def test_components_prefers_code_over_readme_but_not_readme_named_source(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        readme_summary = FileSummary(path="svc/README.md", purpose="Docs for the svc dir")
        code_summary = FileSummary(path="svc/readme_parser.c", purpose="Parses README files")
        store.upsert_file(_rec("svc/README.md", "Markdown"), readme_summary, [])
        store.upsert_file(_rec("svc/readme_parser.c", "C"), code_summary, [])
        store.set_io_map(
            [Component("svc", "svc/", "package",
                       ["svc/README.md", "svc/readme_parser.c"], "C")],
            [],
        )
        store.commit()
        comps = {c["name"]: c for c in json.loads(queries.components(store))}
    finally:
        store.close()
    # readme_parser.c is source, not documentation, so it wins the representative purpose
    # over the actual README.
    assert comps["svc"]["purpose"] == "Parses README files"


def test_ensure_indexed_announces_auto_build(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("def hi():\n    return 1\n")
    settings = Settings()
    settings.affordances.llm_summaries = False
    settings.affordances.store_dir = ".secagent"

    store = queries.ensure_indexed(repo, settings)
    try:
        err = capsys.readouterr().err
        assert "building a heuristic index" in err
        # And it really did index (project map now exists).
        assert store.load_project_map() is not None
    finally:
        store.close()
