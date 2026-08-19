"""The knowledge-graph store: a Tier-1 push-retrieval graph in the shared index.db.

Three shapes — a thing exists (``kg_entities``), two things connect (``kg_relations``),
a thing has another name (``kg_aliases``) — so three tables. They live in the SAME
``.secagent/index.db`` the affordance store uses, so the KG inherits that store's WAL +
busy-timeout + owner-hardening discipline (pi runs several secagent processes at once)
and a code projector can read affordances and write the graph against one file. The
tables are ``kg_``-prefixed and created idempotently, so this class never touches or
depends on :class:`~secagent.affordances.store.AffordanceStore`'s schema.

Reads are the deterministic hot path (seed + walk); writes (extraction) are the
expensive, deferred step. See :mod:`secagent.kg.recall` for how seed + walk compose.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ..security import harden_path
from .hashing import entity_id
from .models import Entity, Fact, Relation

# Matches AffordanceStore._DB_TIMEOUT_S — the same contended file, same headroom.
_DB_TIMEOUT_S = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    description TEXT DEFAULT '',
    source TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_kg_entities_name ON kg_entities(name);
CREATE INDEX IF NOT EXISTS idx_kg_entities_type ON kg_entities(type);
CREATE TABLE IF NOT EXISTS kg_relations (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    source TEXT DEFAULT '',
    PRIMARY KEY (source_id, target_id, predicate)
);
CREATE INDEX IF NOT EXISTS idx_kg_relations_source ON kg_relations(source_id);
CREATE INDEX IF NOT EXISTS idx_kg_relations_target ON kg_relations(target_id);
CREATE TABLE IF NOT EXISTS kg_aliases (
    entity_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_kg_aliases_alias ON kg_aliases(alias);
"""


class KnowledgeGraph:
    """Read/write access to the graph tables in a repo's ``.secagent/index.db``."""

    def __init__(self, repo_root: str | Path, store_dir: str = ".secagent") -> None:
        self.repo_root = Path(repo_root).resolve()
        sd = Path(store_dir)
        self.store_dir = sd if sd.is_absolute() else self.repo_root / sd
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.store_dir / "index.db"
        self._open_db()

    def _open_db(self) -> None:
        # Mirror AffordanceStore's connection setup: WAL so readers never block the
        # single writer, a generous busy timeout so a contended write waits instead of
        # failing, and NORMAL sync (safe under WAL). Our tables are created only when
        # missing, so opening an existing store takes no write lock.
        self.db = sqlite3.connect(self._db_path, timeout=_DB_TIMEOUT_S)
        self.db.row_factory = sqlite3.Row
        self.db.execute(f"PRAGMA busy_timeout={int(_DB_TIMEOUT_S * 1000)}")
        self.db.execute("PRAGMA synchronous=NORMAL")
        with contextlib.suppress(sqlite3.OperationalError):
            self.db.execute("PRAGMA journal_mode=WAL")
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_entities'"
        ).fetchone():
            self.db.executescript(_SCHEMA)
            self.db.commit()
        # The DB file may not exist yet if the affordance store never ran; harden it the
        # same way (owner-only) now that we've created it.
        harden_path(self.store_dir, 0o700)
        if self._db_path.exists():
            harden_path(self._db_path, 0o600)

    def reopen(self) -> None:
        """Reconnect so writes from another process become visible (see AffordanceStore)."""
        with contextlib.suppress(sqlite3.Error):
            self.db.close()
        self._open_db()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> KnowledgeGraph:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def commit(self) -> None:
        self.db.commit()

    # -- writes --------------------------------------------------------------
    def add_entity(
        self, name: str, type_: str, *, description: str = "", source: str = ""
    ) -> str:
        """Upsert an entity; returns its computed id.

        Idempotent by id: a re-extraction of the same (type, name) updates name/type and
        fills a blank description/source, so re-runs merge rather than duplicate. Both
        ``description`` and ``source`` are first-non-empty-wins and must stay in lockstep:
        when several same-named symbols across files collide onto one entity, the note's
        file and its signature have to describe the SAME definition (the first projected),
        not a file from one and a signature from another. A blank pass never erases a
        populated field, so a later call-edge write (whose file is a *calling* site, not a
        definition) cannot overwrite the defining file the projector recorded.
        """
        eid = entity_id(type_, name)
        self.db.execute(
            """
            INSERT INTO kg_entities (id, name, type, description, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                description = CASE WHEN kg_entities.description = '' AND excluded.description != ''
                                   THEN excluded.description ELSE kg_entities.description END,
                source = CASE WHEN kg_entities.source = '' AND excluded.source != ''
                              THEN excluded.source ELSE kg_entities.source END
            """,
            (eid, name, type_, description, source),
        )
        return eid

    def add_relation(
        self, source_id: str, target_id: str, predicate: str, *, source: str = ""
    ) -> None:
        """Add a directed edge; a duplicate (source, target, predicate) is a no-op."""
        self.db.execute(
            "INSERT OR IGNORE INTO kg_relations (source_id, target_id, predicate, source) "
            "VALUES (?, ?, ?, ?)",
            (source_id, target_id, predicate, source),
        )

    def add_alias(self, entity_id_: str, alias: str) -> None:
        """Record an alternate name for an entity; duplicates are a no-op."""
        self.db.execute(
            "INSERT OR IGNORE INTO kg_aliases (entity_id, alias) VALUES (?, ?)",
            (entity_id_, alias),
        )

    def clear(self) -> None:
        """Drop every graph row (a full-rebuild reset).

        A deterministic projector rebuilds the whole graph from the affordance store, and
        edges for deleted code would otherwise linger (the hash merges *live* facts but
        never removes stale ones). ``clear`` before a full re-project keeps the graph a
        faithful mirror. Touches only the ``kg_`` tables — affordance data is untouched.
        """
        self.db.execute("DELETE FROM kg_relations")
        self.db.execute("DELETE FROM kg_aliases")
        self.db.execute("DELETE FROM kg_entities")

    def counts(self) -> dict[str, int]:
        return {
            "entities": self.db.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0],
            "relations": self.db.execute("SELECT COUNT(*) FROM kg_relations").fetchone()[0],
            "aliases": self.db.execute("SELECT COUNT(*) FROM kg_aliases").fetchone()[0],
        }

    # -- primitive reads -----------------------------------------------------
    def get_entity(self, eid: str) -> Entity | None:
        row = self.db.execute(
            "SELECT id, name, type, description, source FROM kg_entities WHERE id=?", (eid,)
        ).fetchone()
        return self._row_to_entity(row) if row else None

    def entities(self, ids: Iterable[str]) -> list[Entity]:
        ids = list(ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.execute(
            "SELECT id, name, type, description, source FROM kg_entities "
            f"WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return [self._row_to_entity(r) for r in rows]

    def relations(self) -> list[Relation]:
        rows = self.db.execute(
            "SELECT source_id, target_id, predicate, source FROM kg_relations"
        ).fetchall()
        return [
            Relation(r["source_id"], r["target_id"], r["predicate"], r["source"]) for r in rows
        ]

    @staticmethod
    def _row_to_entity(r: sqlite3.Row) -> Entity:
        return Entity(r["id"], r["name"], r["type"], r["description"], r["source"])

    # -- traversal primitives (see recall.py) --------------------------------
    def seed(
        self, terms: Iterable[str], *, limit: int = 20, max_matches_per_term: int = 6
    ) -> list[str]:
        """Ids of entities whose name or an alias exactly matches one of ``terms``.

        The first traversal job: turn the question's words into starting points. Matching
        is exact on the normalised term (case-insensitive) against ``name`` and against
        ``kg_aliases.alias`` — lexical seeding, the documented Tier-1 limit.

        A term that matches more than ``max_matches_per_term`` entities is a generic word
        (a bare ``run``/``close`` many symbols share), not a useful anchor, so it is
        dropped rather than seeding a large, unfocused subgraph — the complement of the
        stopword list in recall, which drops generic words before they ever reach here.
        """
        terms = [t for t in {t.strip().lower() for t in terms} if t]
        if not terms:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for term in terms:
            rows = self.db.execute(
                "SELECT id FROM kg_entities WHERE lower(name)=? "
                "UNION SELECT entity_id FROM kg_aliases WHERE lower(alias)=?",
                (term, term),
            ).fetchall()
            if len(rows) > max_matches_per_term:
                continue
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r["id"])
                    if len(out) >= limit:
                        return out
        return out

    def walk(self, seed_ids: Iterable[str], *, hops: int) -> dict[str, int]:
        """The second traversal job: from the seeds, collect every connected entity hop
        by hop, returning ``entity_id -> shallowest depth reached`` (0 for a seed).

        One recursive CTE walks edges in both directions to bounded depth. ``UNION``
        (not ``UNION ALL``) dedups, so a cycle terminates. Depth is what recall ranks by:
        facts nearer the seeds are more likely to answer the question.
        """
        seed_ids = list(dict.fromkeys(seed_ids))  # de-dup, preserve order
        if not seed_ids:
            return {}
        placeholders = ",".join("?" for _ in seed_ids)
        rows = self.db.execute(
            f"""
            WITH RECURSIVE walk(entity_id, depth) AS (
                SELECT id, 0 FROM kg_entities WHERE id IN ({placeholders})
                UNION
                SELECT CASE WHEN r.source_id = w.entity_id
                            THEN r.target_id ELSE r.source_id END,
                       w.depth + 1
                FROM kg_relations r JOIN walk w
                  ON w.entity_id IN (r.source_id, r.target_id)
                WHERE w.depth < ?
            )
            SELECT entity_id, MIN(depth) AS depth FROM walk GROUP BY entity_id
            """,
            (*seed_ids, hops),
        ).fetchall()
        return {r["entity_id"]: r["depth"] for r in rows}

    def facts_within(self, reached: dict[str, int]) -> list[Fact]:
        """Every edge whose BOTH endpoints are in the reached set, as flattened triples.

        ``depth`` on each fact is the shallower of its two endpoints' depths — its
        nearness to the seeds — which recall uses to rank and truncate to a budget.
        """
        ids = list(reached)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.execute(
            f"""
            SELECT e1.name AS subject, r.predicate AS predicate, e2.name AS obj,
                   r.source AS source, r.source_id AS sid, r.target_id AS tid
            FROM kg_relations r
            JOIN kg_entities e1 ON e1.id = r.source_id
            JOIN kg_entities e2 ON e2.id = r.target_id
            WHERE r.source_id IN ({placeholders}) AND r.target_id IN ({placeholders})
            """,
            (*ids, *ids),
        ).fetchall()
        facts: list[Fact] = []
        for r in rows:
            depth = min(reached.get(r["sid"], 0), reached.get(r["tid"], 0))
            facts.append(Fact(r["subject"], r["predicate"], r["obj"], r["source"], depth))
        return facts
