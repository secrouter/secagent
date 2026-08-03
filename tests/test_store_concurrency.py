"""The affordance store must tolerate concurrent access (pi runs several secagent
processes at once) without "database is locked"."""

from __future__ import annotations

import threading

from secagent.affordances.api import index_repo
from secagent.affordances.models import FileRecord, FileSummary
from secagent.affordances.store import AffordanceStore
from secagent.config import Settings


def test_store_uses_wal(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        mode = store.db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        assert store.db.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
    finally:
        store.close()


def test_concurrent_opens_and_writes_do_not_lock(tmp_path):
    # Establish the store once (as `secagent index` does — sets WAL + the schema), then
    # hammer it the way pi's parallel tool/command invocations do: each worker opens a
    # *fresh* store per iteration and writes. Before the fix (no WAL, schema written on
    # every open) this raised sqlite3.OperationalError: database is locked.
    AffordanceStore(tmp_path, ".secagent").close()
    errors: list[Exception] = []

    def worker(wid: int) -> None:
        try:
            for j in range(8):
                store = AffordanceStore(tmp_path, ".secagent")
                try:
                    store.upsert_file(
                        FileRecord(f"f{wid}_{j}.py", "Python", 1, "sha", 1, 0),
                        FileSummary(path=f"f{wid}_{j}.py"), [])
                    store.commit()
                finally:
                    store.close()
        except Exception as exc:  # noqa: BLE001 - capture for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent access raised: {errors}"


def test_concurrent_index_repo_does_not_lock(tmp_path):
    # Several full index_repo runs at once (as pi triggers via parallel tool calls /
    # ensure_indexed) must not raise "database is locked" — the index lock serializes
    # them and each subsequent run is a cheap incremental re-index.
    for i in range(40):
        (tmp_path / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    settings = Settings()
    settings.affordances.llm_summaries = False
    settings.affordances.store_dir = str(tmp_path / ".secagent")

    errors: list[Exception] = []

    def worker() -> None:
        try:
            index_repo(tmp_path, settings)
        except Exception as exc:  # noqa: BLE001 - capture for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent index raised: {errors}"
