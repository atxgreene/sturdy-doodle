"""
mnemosyne_memory.py — SQLite+FTS5 memory with ICMS 3-tier integration.

Purpose
-------
Provides the persistent memory layer for Mnemosyne. Matches the expressive
power of Hermes's hermes_state.py (SQLite + FTS5 full-text search) while
integrating natively with eternal-context's ICMS 3-tier memory model (L1 hot,
L2 warm, L3 cold). Every memory row carries a `tier` column; promotion and
eviction are explicit operations the brain can issue.

Design choices
--------------
- Stdlib only: sqlite3 from Python's standard library. FTS5 is compiled into
  most distributed sqlite3 binaries (including the one shipped with CPython
  3.9+ on all major platforms). A runtime check verifies availability and
  falls back to LIKE-based search if FTS5 is missing (rare, but possible on
  minimal systems).

- One database per run context, living under $PROJECTS_DIR/memory.db by
  default. The path can be overridden for testing.

- Plain SQLite means the memory is grep-navigable with `sqlite3 memory.db
  '.dump'` and the schema is inspectable by humans and by an agentic
  proposer doing Meta-Harness-style optimization.

- Every memory event can be logged simultaneously to harness_telemetry, so
  the observability substrate sees memory operations as first-class events
  (memory_write, memory_read, memory_promote, memory_evict). No separate
  "memory log" — one observation point.

Schema
------
    memories(
        id INTEGER PRIMARY KEY,
        created_utc TEXT NOT NULL,
        updated_utc TEXT NOT NULL,
        source TEXT NOT NULL,            -- e.g. "conversation", "tool_result", "skill_output"
        tier INTEGER NOT NULL DEFAULT 2, -- 1=L1 hot, 2=L2 warm, 3=L3 cold
        kind TEXT NOT NULL,              -- e.g. "fact", "preference", "event", "skill_outcome"
        content TEXT NOT NULL,
        metadata_json TEXT,              -- free-form JSON string
        access_count INTEGER DEFAULT 0,
        last_accessed_utc TEXT
    );

    memories_fts (virtual FTS5 table)    -- full-text index over content
        content

Triggers keep the FTS index in sync with the base table on INSERT/UPDATE/DELETE.

Usage
-----
    from mnemosyne_memory import MemoryStore

    mem = MemoryStore()              # $PROJECTS_DIR/memory.db
    mid = mem.write(
        content="User prefers dark mode in terminal apps.",
        source="conversation",
        kind="preference",
        tier=1,
    )
    hits = mem.search("dark mode", limit=5)
    mem.promote(mid, to_tier=1)
    mem.evict_l3_older_than(days=30)

ICMS integration
----------------
eternal-context's ICMS decides tier transitions. This module provides the
persistence. Integration pattern:

    # in eternal-context code
    from mnemosyne_memory import MemoryStore
    mem = MemoryStore()

    # on each turn:
    hits = mem.search(user_query, tier_max=2)     # hot+warm retrieval
    # ... LLM generates response ...
    mem.write(content=response_summary, tier=2)   # new warm memory

    # during consciousness layer's dream consolidation:
    mem.promote(high_access_id, to_tier=1)
    mem.evict_l3_older_than(days=90)

Zero deps. Safe to import from any Mnemosyne module.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# Tier constants — match eternal-context's ICMS L1/L2/L3
L1_HOT = 1
L2_WARM = 2
L3_COLD = 3

_TIER_NAMES = {L1_HOT: "L1_hot", L2_WARM: "L2_warm", L3_COLD: "L3_cold"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _default_memory_path() -> Path:
    try:
        from mnemosyne_config import default_projects_dir
        return default_projects_dir() / "memory.db"
    except ImportError:
        import os
        raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
        base = Path(raw).expanduser().resolve() if raw else (
            Path.home() / "projects" / "mnemosyne"
        )
        return base / "memory.db"


def _check_fts5(conn: sqlite3.Connection) -> bool:
    """Return True if the SQLite binary has FTS5 compiled in."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


class MemoryStore:
    """SQLite-backed memory with optional FTS5 acceleration and ICMS tiering.

    Thread-safe via an internal lock. Uses `check_same_thread=False` so a
    single store can be shared across a brain thread and a channel thread.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: str | Path | None = None,
        telemetry: Any | None = None,
    ) -> None:
        self.path = Path(path) if path else _default_memory_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,  # autocommit — we batch explicitly where needed
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._has_fts5 = _check_fts5(self._conn)
        self._telemetry = telemetry
        self._init_schema()

    # ---- schema / migrations ------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    source TEXT NOT NULL,
                    tier INTEGER NOT NULL DEFAULT 2,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    content TEXT NOT NULL,
                    metadata_json TEXT,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    last_accessed_utc TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_memories_tier
                    ON memories(tier, last_accessed_utc);
                CREATE INDEX IF NOT EXISTS idx_memories_kind
                    ON memories(kind);
                CREATE INDEX IF NOT EXISTS idx_memories_source
                    ON memories(source);
            """)
            if self._has_fts5:
                # Contentless FTS5 pointing at memories.content via triggers
                self._conn.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                        USING fts5(content, content='memories', content_rowid='id');
                    CREATE TRIGGER IF NOT EXISTS memories_ai
                        AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_ad
                        AFTER DELETE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content)
                            VALUES ('delete', old.id, old.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_au
                        AFTER UPDATE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content)
                            VALUES ('delete', old.id, old.content);
                        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                """)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    # ---- telemetry hook -----------------------------------------------------

    def _emit(self, event_type: str, **fields: Any) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.log(event_type, metadata=fields)
        except Exception:
            pass  # telemetry must never break memory ops

    # ---- write / read -------------------------------------------------------

    def write(
        self,
        content: str,
        *,
        source: str = "conversation",
        kind: str = "fact",
        tier: int = L2_WARM,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Insert a new memory row. Returns the row id."""
        now = _utcnow()
        meta_json = json.dumps(metadata, default=str) if metadata else None
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO memories
                   (created_utc, updated_utc, source, tier, kind, content, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (now, now, source, tier, kind, content, meta_json),
            )
            rid = cur.lastrowid
        self._emit("memory_write", memory_id=rid, source=source, kind=kind, tier=tier,
                   content_len=len(content))
        return int(rid)  # type: ignore[arg-type]

    def get(self, memory_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---- search -------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        tier_max: int | None = None,
        kind: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Full-text search over memories.

        Uses FTS5 if available, falls back to LIKE substring match otherwise.
        `tier_max` restricts to tiers ≤ N (e.g. tier_max=2 excludes L3 cold).
        """
        results: list[dict[str, Any]] = []
        with self._lock:
            if self._has_fts5 and query.strip():
                sql = """
                    SELECT m.*, memories_fts.rank AS relevance
                    FROM memories_fts
                    JOIN memories m ON m.id = memories_fts.rowid
                    WHERE memories_fts MATCH ?
                """
                params: list[Any] = [_fts5_escape(query)]
                if tier_max is not None:
                    sql += " AND m.tier <= ?"
                    params.append(tier_max)
                if kind:
                    sql += " AND m.kind = ?"
                    params.append(kind)
                if source:
                    sql += " AND m.source = ?"
                    params.append(source)
                sql += " ORDER BY memories_fts.rank LIMIT ?"
                params.append(limit)
            else:
                # Fallback: LIKE scan
                sql = "SELECT *, 0.0 AS relevance FROM memories WHERE 1=1"
                params = []
                if query.strip():
                    sql += " AND content LIKE ?"
                    params.append(f"%{query}%")
                if tier_max is not None:
                    sql += " AND tier <= ?"
                    params.append(tier_max)
                if kind:
                    sql += " AND kind = ?"
                    params.append(kind)
                if source:
                    sql += " AND source = ?"
                    params.append(source)
                sql += " ORDER BY last_accessed_utc DESC NULLS LAST, created_utc DESC LIMIT ?"
                params.append(limit)

            rows = self._conn.execute(sql, params).fetchall()
            # Touch access_count + last_accessed_utc for hits
            now = _utcnow()
            for row in rows:
                results.append(dict(row))
                self._conn.execute(
                    """UPDATE memories
                       SET access_count = access_count + 1, last_accessed_utc = ?
                       WHERE id = ?""",
                    (now, row["id"]),
                )
        self._emit("memory_read", query=query, hits=len(results),
                   tier_max=tier_max, kind=kind)
        return results

    # ---- tier operations ----------------------------------------------------

    def promote(self, memory_id: int, *, to_tier: int) -> None:
        """Move a memory to a hotter (lower-numbered) tier."""
        if to_tier not in (L1_HOT, L2_WARM, L3_COLD):
            raise ValueError(f"invalid tier: {to_tier}")
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET tier = ?, updated_utc = ? WHERE id = ?",
                (to_tier, now, memory_id),
            )
        self._emit("memory_promote", memory_id=memory_id, to_tier=to_tier)

    def evict_l3_older_than(self, *, days: int) -> int:
        """Delete L3 memories last accessed more than N days ago. Returns rows deleted."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with self._lock:
            cur = self._conn.execute(
                """DELETE FROM memories
                   WHERE tier = ?
                   AND (last_accessed_utc IS NULL OR last_accessed_utc < ?)""",
                (L3_COLD, cutoff),
            )
            deleted = cur.rowcount
        self._emit("memory_evict", tier=L3_COLD, cutoff=cutoff, deleted=deleted)
        return deleted

    def demote_unused(self, *, from_tier: int, threshold_days: int) -> int:
        """Demote memories not touched in N days from the given tier.

        L1 → L2 after threshold_days, L2 → L3 after threshold_days.
        Returns rows demoted.
        """
        if from_tier not in (L1_HOT, L2_WARM):
            return 0
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=threshold_days)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        to_tier = from_tier + 1
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE memories SET tier = ?, updated_utc = ?
                   WHERE tier = ?
                   AND (last_accessed_utc IS NULL OR last_accessed_utc < ?)""",
                (to_tier, now, from_tier, cutoff),
            )
            demoted = cur.rowcount
        self._emit("memory_demote", from_tier=from_tier, to_tier=to_tier,
                   cutoff=cutoff, demoted=demoted)
        return demoted

    # ---- stats / admin ------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()[0]
            by_tier = {
                _TIER_NAMES[t]: self._conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE tier = ?", (t,)
                ).fetchone()[0]
                for t in (L1_HOT, L2_WARM, L3_COLD)
            }
            by_kind = dict(
                self._conn.execute(
                    "SELECT kind, COUNT(*) FROM memories GROUP BY kind"
                ).fetchall()
            )
        return {
            "total": total,
            "by_tier": by_tier,
            "by_kind": by_kind,
            "fts5_enabled": self._has_fts5,
            "db_path": str(self.path),
            "schema_version": self.SCHEMA_VERSION,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _fts5_escape(query: str) -> str:
    """Escape an FTS5 query string to avoid syntax errors on user input.

    Quotes each term as a phrase so operators in user input don't break.
    Empty input returns '""' which FTS5 treats as no-match.
    """
    tokens = [t for t in query.split() if t]
    if not tokens:
        return '""'
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


# ---- CLI (smoke test) -------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        prog="mnemosyne_memory",
        description="Smoke-test CLI for mnemosyne_memory. For real usage, "
                    "import MemoryStore.",
    )
    p.add_argument("--db", help="override memory.db path")
    sub = p.add_subparsers(dest="cmd", required=True)

    wp = sub.add_parser("write", help="insert a memory")
    wp.add_argument("content")
    wp.add_argument("--source", default="cli")
    wp.add_argument("--kind", default="fact")
    wp.add_argument("--tier", type=int, default=L2_WARM, choices=[1, 2, 3])

    sp = sub.add_parser("search", help="full-text search")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--tier-max", type=int, default=None)

    sub.add_parser("stats", help="show memory statistics")

    args = p.parse_args(argv)
    mem = MemoryStore(path=args.db)

    if args.cmd == "write":
        mid = mem.write(args.content, source=args.source, kind=args.kind, tier=args.tier)
        print(mid)
    elif args.cmd == "search":
        for r in mem.search(args.query, limit=args.limit, tier_max=args.tier_max):
            print(f"[L{r['tier']}] {r['content']}  ({r['kind']}, {r['source']})")
    elif args.cmd == "stats":
        print(json.dumps(mem.stats(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
