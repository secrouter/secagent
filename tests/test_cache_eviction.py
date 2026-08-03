"""ContentCache housekeeping: stats, clear, and age-based prune. Regression coverage
for the Tier-3 unbounded-cache item."""

from __future__ import annotations

import os
import time

from secagent.affordances.cache import ContentCache


def test_stats_counts_entries_and_bytes(tmp_path):
    c = ContentCache(tmp_path / "cache")
    assert c.stats() == {"entries": 0, "bytes": 0}
    c.put(ContentCache.make_key("a"), {"purpose": "x"})
    c.put(ContentCache.make_key("b"), {"purpose": "y"})
    s = c.stats()
    assert s["entries"] == 2 and s["bytes"] > 0


def test_clear_removes_all(tmp_path):
    c = ContentCache(tmp_path / "cache")
    for k in ("a", "b", "c"):
        c.put(ContentCache.make_key(k), {"doc": k})
    assert c.clear() == 3
    assert c.stats()["entries"] == 0
    # A cleared entry is simply a miss afterward.
    assert c.get(ContentCache.make_key("a")) is None


def test_prune_removes_only_old_entries(tmp_path):
    c = ContentCache(tmp_path / "cache")
    old_key = ContentCache.make_key("old")
    new_key = ContentCache.make_key("new")
    c.put(old_key, {"purpose": "stale"})
    c.put(new_key, {"purpose": "fresh"})
    # Backdate the "old" entry's mtime by 10 days.
    old_path = c._path_for(old_key)
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old_path, (ten_days_ago, ten_days_ago))

    removed = c.prune(older_than_days=7)
    assert removed == 1
    assert c.get(old_key) is None       # pruned
    assert c.get(new_key) is not None    # kept


def test_prune_rejects_negative(tmp_path):
    c = ContentCache(tmp_path / "cache")
    try:
        c.prune(-1)
    except ValueError:
        return
    raise AssertionError("expected ValueError for negative older_than_days")
