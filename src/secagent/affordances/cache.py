"""Content-addressed cache for expensive derived artifacts (e.g. LLM summaries).

Keys are SHA-256 (FIPS-approved) derived from the inputs that determine the output —
typically a file's content hash plus a prompt/version tag — so re-indexing an
unchanged file is a cache hit and never re-invokes the model.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..security import text_hash


class ContentCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        # Shard by first two hex chars to avoid huge flat directories.
        return self.root / key[:2] / f"{key}.json"

    @staticmethod
    def make_key(*parts: str) -> str:
        return text_hash("\x00".join(parts))

    def get(self, key: str) -> dict[str, Any] | None:
        p = self._path_for(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        p = self._path_for(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value), encoding="utf-8")

    def items(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield (key, value) for every cached entry (key = the SHA-256 filename stem).

        The key is opaque (a hash of the inputs), so entries are not attributable to a
        source file/function — use for inspecting the raw cached values.
        """
        for p in sorted(self.root.rglob("*.json")):
            try:
                yield p.stem, json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

    # -- housekeeping --------------------------------------------------------
    # The cache is keyed by a hash of (content, prompt version, model), so entries from
    # superseded file versions / prompt bumps / retired models are never overwritten —
    # they just accumulate. These let an operator report and reclaim that space. Keys are
    # opaque (no stored version), so age (file mtime) is the practical eviction axis.
    def stats(self) -> dict[str, int]:
        """Return ``{"entries": N, "bytes": B}`` for the whole cache."""
        entries = total = 0
        for p in self.root.rglob("*.json"):
            try:
                total += p.stat().st_size
            except OSError:
                continue
            entries += 1
        return {"entries": entries, "bytes": total}

    def clear(self) -> int:
        """Delete every cached entry. Returns the number removed."""
        removed = 0
        for p in self.root.rglob("*.json"):
            try:
                p.unlink()
            except OSError:
                continue
            removed += 1
        return removed

    def prune(self, older_than_days: float) -> int:
        """Delete entries not modified in the last ``older_than_days`` days.

        Uses file mtime; ``put`` rewrites (and so refreshes) an entry on every cache hit's
        miss, so an actively-used entry stays young. Returns the number removed.
        """
        if older_than_days < 0:
            raise ValueError("older_than_days must be >= 0")
        cutoff = time.time() - older_than_days * 86400
        removed = 0
        for p in self.root.rglob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
