"""Verbose progress: the step helper, idempotent handler, and that index emits."""

from __future__ import annotations

import logging

from secagent import progress
from secagent.affordances.api import index_repo
from secagent.config import Settings
from secagent.progress import progress_step


def test_progress_step():
    assert progress_step(100) == 5      # ~20 updates
    assert progress_step(3) == 1        # never zero
    assert progress_step(0) == 1


def test_enable_verbose_is_idempotent():
    saved = (progress.log.handlers[:], progress.log.level, progress.log.propagate,
             progress._enabled)
    try:
        progress.log.handlers.clear()
        progress._enabled = False
        progress.enable_verbose()
        progress.enable_verbose()
        assert len(progress.log.handlers) == 1  # only one handler, despite two calls
    finally:
        progress.log.handlers[:] = saved[0]
        progress.log.setLevel(saved[1])
        progress.log.propagate = saved[2]
        progress._enabled = saved[3]


def test_index_emits_progress(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / ".store")

    seen: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: seen.append(r.getMessage())  # type: ignore[method-assign]
    plog = logging.getLogger("secagent")
    plog.addHandler(handler)
    plog.setLevel(logging.INFO)
    try:
        index_repo(tmp_path, s)
    finally:
        plog.removeHandler(handler)

    joined = " ".join(seen)
    assert "Indexing" in joined
    assert "Indexed" in joined
