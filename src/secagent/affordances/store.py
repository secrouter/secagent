"""The affordance store: SQLite metadata + JSON artifacts.

Holds everything the engine derives so both agents (and the MCP server) read from a
single place. Indexing is incremental: a file whose SHA-256 is unchanged keeps its
existing summary/symbols and is skipped.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, TypeVar

from ..security import harden_path
from .cache import ContentCache
from .io_map import summarize_io
from .models import (
    CallEdge,
    Component,
    FileRecord,
    FileSummary,
    IOEdge,
    ProjectMap,
    Symbol,
    TypeRecord,
)

_T = TypeVar("_T")


def _from_dict(cls: type[_T], data: dict[str, Any]) -> _T:
    """Build a dataclass from a JSON dict, ignoring keys the schema no longer has.

    Artifacts persisted by an older/newer secagent may carry fields this build dropped or
    hasn't added; ``cls(**data)`` would raise ``TypeError`` on any unknown key and take
    down every read. Dropping unknown keys degrades gracefully to whatever this build
    understands. (Missing keys still surface — those are genuine required-field errors.)
    """
    fields = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    return cls(**{k: v for k, v in data.items() if k in fields})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, language TEXT, size INTEGER, sha256 TEXT,
    loc INTEGER, n_symbols INTEGER
);
CREATE TABLE IF NOT EXISTS summaries (path TEXT PRIMARY KEY, json TEXT);
CREATE TABLE IF NOT EXISTS symbols (
    path TEXT, name TEXT, kind TEXT, lineno INTEGER, signature TEXT, parent TEXT,
    doc TEXT DEFAULT '', qualified_name TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
CREATE TABLE IF NOT EXISTS calls (
    src_file TEXT, dst_file TEXT, caller TEXT, callee TEXT,
    edge_kind TEXT DEFAULT 'direct', line INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_src ON calls(src_file);
CREATE INDEX IF NOT EXISTS idx_calls_dst ON calls(dst_file);
CREATE TABLE IF NOT EXISTS types (
    qualified_name TEXT, kind TEXT, file TEXT, lineno INTEGER,
    bases TEXT DEFAULT '[]', interfaces TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_types_file ON types(file);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# How long a write waits for a contended lock before giving up. Generous because pi runs
# several secagent processes against one store; with per-file commits a real wait is brief,
# so this is headroom for an occasional long write (a big call-map rebuild, slow disk).
_DB_TIMEOUT_S = 60.0


class AffordanceStore:
    def __init__(self, repo_root: str | Path, store_dir: str = ".secagent") -> None:
        self.repo_root = Path(repo_root).resolve()
        sd = Path(store_dir)
        self.store_dir = sd if sd.is_absolute() else self.repo_root / sd
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = self.store_dir / "artifacts"
        self.artifacts_dir.mkdir(exist_ok=True)
        self.cache = ContentCache(self.store_dir / "cache")
        self._db_path = self.store_dir / "index.db"
        self._open_db()

    def _open_db(self) -> None:
        db_path = self._db_path
        # Concurrency: pi runs several secagent processes at once, all opening this store.
        # WAL lets readers run without blocking the single writer; a generous busy timeout
        # makes a contended write wait instead of failing with "database is locked"; and
        # the schema is written only when missing, so a normal open of an existing store
        # takes no write lock at all (the previous unconditional CREATE/commit on every
        # open was the contention point).
        self.db = sqlite3.connect(db_path, timeout=_DB_TIMEOUT_S)
        self.db.row_factory = sqlite3.Row
        self.db.execute(f"PRAGMA busy_timeout={int(_DB_TIMEOUT_S * 1000)}")
        self.db.execute("PRAGMA synchronous=NORMAL")  # safe under WAL, fewer fsyncs
        # WAL persists in the file header once any connection sets it; a mid-transition
        # race here is harmless (busy timeout + the index lock still apply).
        with contextlib.suppress(sqlite3.OperationalError):
            self.db.execute("PRAGMA journal_mode=WAL")
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"
        ).fetchone():
            self.db.executescript(_SCHEMA)
        self._migrate()
        self.db.commit()
        # At-rest hardening (CMMC-2): restrict the store to the owner.
        for d in (self.store_dir, self.artifacts_dir):
            harden_path(d, 0o700)
        harden_path(db_path, 0o600)

    def reopen(self) -> None:
        """Re-establish the connection so writes from another process become visible.

        A long-lived reader (the MCP server) holds its own connection. After an indexer
        run in a separate connection, reconnecting is the unambiguous way to guarantee
        the next query sees the new data rather than a stale snapshot.
        """
        with contextlib.suppress(sqlite3.Error):
            self.db.close()
        self._open_db()

    def _migrate(self) -> None:
        """Add columns introduced after a store's schema version, idempotently."""
        def add_col(table: str, col: str, decl: str) -> None:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

        add_col("symbols", "doc", "TEXT DEFAULT ''")
        add_col("symbols", "qualified_name", "TEXT DEFAULT ''")
        add_col("calls", "edge_kind", "TEXT DEFAULT 'direct'")
        # Existing rows get 0, which `callers` reads as "unknown" and omits rather than
        # reporting as line 0 — a store indexed before this column existed must not
        # start fabricating call sites. The key reappears after `secagent index`.
        add_col("calls", "line", "INTEGER DEFAULT 0")
        # `types` was added after the first schema version. A store created before it
        # existed ran _SCHEMA only once (that run is gated on the `files` table being
        # absent), so the table never appears on upgrade and load_types()/set_types()
        # crash with "no such table: types". Create it here, idempotently.
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='types'"
        ).fetchone():
            self.db.executescript(
                "CREATE TABLE IF NOT EXISTS types ("
                "  qualified_name TEXT, kind TEXT, file TEXT, lineno INTEGER,"
                "  bases TEXT DEFAULT '[]', interfaces TEXT DEFAULT '[]');"
                "CREATE INDEX IF NOT EXISTS idx_types_file ON types(file);"
            )

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> AffordanceStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- incremental helpers -------------------------------------------------
    def existing_sha(self, path: str) -> str | None:
        row = self.db.execute("SELECT sha256 FROM files WHERE path=?", (path,)).fetchone()
        return row["sha256"] if row else None

    def known_paths(self) -> set[str]:
        return {r["path"] for r in self.db.execute("SELECT path FROM files")}

    # -- writes --------------------------------------------------------------
    def upsert_file(self, rec: FileRecord, summary: FileSummary, symbols: list[Symbol]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
            (rec.path, rec.language, rec.size, rec.sha256, rec.loc, len(symbols)),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO summaries VALUES (?,?)",
            (rec.path, json.dumps(summary.to_dict())),
        )
        self.db.execute("DELETE FROM symbols WHERE path=?", (rec.path,))
        self.db.executemany(
            "INSERT INTO symbols "
            "(path, name, kind, lineno, signature, parent, doc, qualified_name) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(s.file, s.name, s.kind, s.lineno, s.signature, s.parent, s.doc,
              s.qualified_name) for s in symbols],
        )

    def set_symbols(self, path: str, symbols: list[Symbol]) -> None:
        """Replace the symbols for one file (used by the clang pass for C/C++)."""
        self.db.execute("DELETE FROM symbols WHERE path=?", (path,))
        self.db.executemany(
            "INSERT INTO symbols "
            "(path, name, kind, lineno, signature, parent, doc, qualified_name) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(s.file, s.name, s.kind, s.lineno, s.signature, s.parent, s.doc,
              s.qualified_name) for s in symbols],
        )
        self.db.execute("UPDATE files SET n_symbols=? WHERE path=?", (len(symbols), path))

    def set_symbol_doc(self, path: str, name: str, doc: str) -> None:
        """Persist a generated one-line description for a function/symbol."""
        self.db.execute(
            "UPDATE symbols SET doc=? WHERE path=? AND name=?", (doc, path, name)
        )

    def delete_file(self, path: str) -> None:
        self.db.execute("DELETE FROM files WHERE path=?", (path,))
        self.db.execute("DELETE FROM summaries WHERE path=?", (path,))
        self.db.execute("DELETE FROM symbols WHERE path=?", (path,))
        # Drop edges on both ends — a stale `dst_file` leaves dangling inbound callers —
        # and any types the file declared.
        self.db.execute("DELETE FROM calls WHERE src_file=? OR dst_file=?", (path, path))
        self.db.execute("DELETE FROM types WHERE file=?", (path,))

    def commit(self) -> None:
        self.db.commit()

    def set_project_map(self, pm: ProjectMap) -> None:
        self._write_artifact("project_map.json", pm.to_dict())
        self._set_meta("root", pm.root)

    def set_io_map(self, components: list[Component], edges: list[IOEdge]) -> None:
        self._write_artifact(
            "io_map.json",
            {
                "components": [c.to_dict() for c in components],
                "edges": [e.to_dict() for e in edges],
            },
        )
        self._write_text_artifact("io_map.txt", summarize_io(edges))

    def set_call_map(self, edges: list[CallEdge]) -> None:
        self.db.execute("DELETE FROM calls")
        self.db.executemany(
            "INSERT INTO calls (src_file, dst_file, caller, callee, edge_kind, line) "
            "VALUES (?,?,?,?,?,?)",
            [(e.src_file, e.dst_file, e.caller, e.callee, e.edge_kind, e.line)
             for e in edges],
        )
        self._write_artifact("call_map.json", {"edges": [e.to_dict() for e in edges]})

    def set_types(self, types: list[TypeRecord]) -> None:
        """Replace the type/inheritance records (from a semantic/heavy backend)."""
        self.db.execute("DELETE FROM types")
        self.db.executemany(
            "INSERT INTO types (qualified_name, kind, file, lineno, bases, interfaces) "
            "VALUES (?,?,?,?,?,?)",
            [(t.qualified_name, t.kind, t.file, t.lineno,
              json.dumps(t.bases), json.dumps(t.interfaces)) for t in types],
        )
        self._write_artifact("types.json", {"types": [t.to_dict() for t in types]})

    def replace_types_for_files(self, files: list[str], types: list[TypeRecord]) -> None:
        """Replace type records for exactly ``files``, leaving every other file's
        records — including a heavy backend's (Roslyn for C#, rust-analyzer for Rust)
        — untouched.

        ``set_types()`` replaces the WHOLE table; it exists for a heavy-backend ingest
        that intentionally owns the complete picture for its language in one shot (see
        ``analysis.ingest_report``). A per-file *light* pass (e.g. the Python ``ast``
        backend deriving class bases while indexing one file at a time) must never call
        it: every `index_repo` run would silently DELETE FROM types with no WHERE
        clause and wipe out C#/Rust records that this run never re-derives. This is the
        file-scoped analogue, matching the incremental semantics `upsert_file` /
        `set_symbols` already use elsewhere in this store.

        Deliberately does NOT rewrite the ``types.json`` artifact — a caller looping
        this per file (as `index_repo` does, once per Python file) would otherwise pay
        a full `load_types()` + file write on every single call, turning an O(files)
        index into O(files^2). Call `write_types_artifact()` once, after the loop that
        calls this is done, to publish the finished snapshot.
        """
        if not files:
            return
        files = list(files)
        placeholders = ",".join("?" for _ in files)
        self.db.execute(f"DELETE FROM types WHERE file IN ({placeholders})", files)
        file_set = set(files)
        scoped = [t for t in types if t.file in file_set]
        self.db.executemany(
            "INSERT INTO types (qualified_name, kind, file, lineno, bases, interfaces) "
            "VALUES (?,?,?,?,?,?)",
            [(t.qualified_name, t.kind, t.file, t.lineno,
              json.dumps(t.bases), json.dumps(t.interfaces)) for t in scoped],
        )

    def write_types_artifact(self) -> None:
        """Publish the current ``types`` table as ``types.json``.

        Separate from `replace_types_for_files` so a caller doing many small
        file-scoped replacements (the Python light pass, one file per call) writes the
        artifact once at the end instead of once per file — see that method's
        docstring. `set_types` still writes its own snapshot inline because it is
        called once per whole-table ingest, not in a per-file loop.
        """
        self._write_artifact("types.json", {"types": [t.to_dict() for t in self.load_types()]})

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # -- parse fidelity ------------------------------------------------------
    # Recorded by the C/C++ pass so query results can admit when the underlying data is
    # incomplete, rather than presenting a partial call map as if it were whole.
    def set_parse_health(self, health: dict[str, Any]) -> None:
        self._set_meta("clang_parse_health", json.dumps(health))
        self.db.commit()

    def parse_health(self) -> dict[str, Any]:
        """C/C++ parse fidelity for the last index; ``{}`` if never recorded."""
        raw = self.get_meta("clang_parse_health")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _atomic_write(self, name: str, text: str) -> None:
        """Write via a temp file + os.replace so a concurrent reader never sees a
        half-written artifact (write_text truncates then streams)."""
        dest = self.artifacts_dir / name
        tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)

    def _write_artifact(self, name: str, data: dict[str, Any]) -> None:
        self._atomic_write(name, json.dumps(data, indent=2))

    def _write_text_artifact(self, name: str, text: str) -> None:
        self._atomic_write(name, text)

    # -- reads ---------------------------------------------------------------
    def file_records(self) -> list[FileRecord]:
        rows = self.db.execute("SELECT * FROM files ORDER BY path").fetchall()
        return [
            FileRecord(r["path"], r["language"], r["size"], r["sha256"], r["loc"], r["n_symbols"])
            for r in rows
        ]

    def load_summary(self, path: str) -> FileSummary | None:
        row = self.db.execute("SELECT json FROM summaries WHERE path=?", (path,)).fetchone()
        if not row:
            return None
        return _from_dict(FileSummary, json.loads(row["json"]))

    def all_summaries(self) -> dict[str, FileSummary]:
        rows = self.db.execute("SELECT path, json FROM summaries").fetchall()
        return {r["path"]: _from_dict(FileSummary, json.loads(r["json"])) for r in rows}

    @staticmethod
    def _row_to_symbol(r: sqlite3.Row) -> Symbol:
        # `doc`/`qualified_name` are guaranteed by _migrate(); SELECT * includes them.
        return Symbol(r["name"], r["kind"], r["path"], r["lineno"], r["signature"],
                      r["parent"], r["doc"], r["qualified_name"])

    def search_symbols(self, name: str, limit: int = 25) -> list[Symbol]:
        rows = self.db.execute(
            "SELECT * FROM symbols WHERE name LIKE ? ORDER BY name LIMIT ?",
            (f"%{name}%", limit),
        ).fetchall()
        return [self._row_to_symbol(r) for r in rows]

    def symbol_names_by_file(self) -> dict[str, list[str]]:
        """``path -> [symbol name, ...]`` for the whole repo, in one query.

        Retrieval needs the REAL symbol index, not a summary's ``key_symbols`` — that is
        a display field (capped, and excluding constants), so ranking against it makes
        every macro-only header invisible to ``search``.
        """
        out: dict[str, list[str]] = {}
        for r in self.db.execute("SELECT path, name FROM symbols"):
            out.setdefault(r["path"], []).append(r["name"])
        return out

    def all_symbol_names(self) -> set[str]:
        """Every indexed symbol name, plain and qualified.

        Used to check whether a name a model wrote down actually exists in this repo.
        """
        names: set[str] = set()
        for r in self.db.execute("SELECT name, qualified_name FROM symbols"):
            if r["name"]:
                names.add(r["name"])
            if r["qualified_name"]:
                names.add(r["qualified_name"])
                names.add(r["qualified_name"].rsplit("::", 1)[-1])
        for r in self.db.execute("SELECT qualified_name FROM types"):
            if r["qualified_name"]:
                names.add(r["qualified_name"])
                names.add(r["qualified_name"].rsplit(".", 1)[-1])
                names.add(r["qualified_name"].rsplit("::", 1)[-1])
        return names

    def symbols_for_file(self, path: str) -> list[Symbol]:
        rows = self.db.execute(
            "SELECT * FROM symbols WHERE path=? ORDER BY lineno", (path,)
        ).fetchall()
        return [self._row_to_symbol(r) for r in rows]

    def function_symbol_names(self) -> list[tuple[str, str]]:
        """``(name, path)`` for every function/method symbol — for entrypoint detection."""
        rows = self.db.execute(
            "SELECT name, path FROM symbols WHERE kind IN ('function', 'method')"
        ).fetchall()
        return [(r["name"], r["path"]) for r in rows]

    def load_call_edges(self) -> list[CallEdge]:
        rows = self.db.execute("SELECT * FROM calls").fetchall()
        return [CallEdge(r["src_file"], r["dst_file"], r["caller"], r["callee"],
                         r["edge_kind"], r["line"]) for r in rows]

    def load_types(self) -> list[TypeRecord]:
        rows = self.db.execute("SELECT * FROM types ORDER BY qualified_name").fetchall()
        return [TypeRecord(r["qualified_name"], r["kind"], r["file"], r["lineno"],
                           json.loads(r["bases"]), json.loads(r["interfaces"]))
                for r in rows]

    def load_project_map(self) -> ProjectMap | None:
        p = self.artifacts_dir / "project_map.json"
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        data["components"] = [_from_dict(Component, c) for c in data.get("components", [])]
        return _from_dict(ProjectMap, data)

    def load_io_edges(self) -> list[IOEdge]:
        p = self.artifacts_dir / "io_map.json"
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        return [_from_dict(IOEdge, e) for e in data.get("edges", [])]

    def load_components(self) -> list[Component]:
        p = self.artifacts_dir / "io_map.json"
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        return [_from_dict(Component, c) for c in data.get("components", [])]
