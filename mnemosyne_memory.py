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
from typing import Any


# Tier constants — match eternal-context's ICMS L1/L2/L3
L1_HOT = 1
L2_WARM = 2
L3_COLD = 3

_TIER_NAMES = {L1_HOT: "L1_hot", L2_WARM: "L2_warm", L3_COLD: "L3_cold"}


try:
    from mnemosyne_config import utcnow_iso as _utcnow
except ImportError:  # pragma: no cover
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


_FTS5_CHECK_CACHE: bool | None = None
_FTS5_CHECK_LOCK = threading.Lock()


def _check_fts5(conn: sqlite3.Connection) -> bool:
    """Return True if the SQLite binary has FTS5 compiled in.

    Cached at module scope: FTS5 availability is a property of the
    Python/SQLite binary, not of this connection or this DB file, so
    one probe per interpreter is enough. Caching also dodges a race
    we'd otherwise hit under high-concurrency MemoryStore() opens
    where simultaneous probes collided on the `_fts5_probe` name.
    """
    global _FTS5_CHECK_CACHE
    if _FTS5_CHECK_CACHE is not None:
        return _FTS5_CHECK_CACHE
    with _FTS5_CHECK_LOCK:
        if _FTS5_CHECK_CACHE is not None:
            return _FTS5_CHECK_CACHE
        # Use an in-memory connection so the probe table never races
        # with a real DB's schema even if we're called outside the
        # cache. Also faster than roundtripping to disk.
        import sqlite3 as _s
        try:
            probe = _s.connect(":memory:")
            probe.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
            probe.close()
            _FTS5_CHECK_CACHE = True
        except _s.OperationalError:
            _FTS5_CHECK_CACHE = False
    return _FTS5_CHECK_CACHE


# Module-level lock: concurrent CREATE VIRTUAL TABLE USING fts5 on the
# same DB file can fail with "vtable constructor failed: memories_fts"
# even with busy_timeout, because FTS5 module registration isn't
# coordinated across connections. Serialize schema init across threads
# in one process. Different processes are still vulnerable; users who
# parallelize across processes should `mnemosyne-memory stats` once
# before spawning workers to pre-create the schema.
_SCHEMA_INIT_LOCK = threading.Lock()


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
        import time as _t
        self.path = Path(path) if path else _default_memory_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Retry the cold-connect + initial PRAGMAs — under concurrent
        # MemoryStore() calls on a fresh file, sqlite3.connect() itself
        # can hit "database is locked" during file creation before any
        # busy_timeout can help.
        last_err: sqlite3.OperationalError | None = None
        for attempt in range(5):
            try:
                self._conn = sqlite3.connect(
                    str(self.path),
                    check_same_thread=False,
                    isolation_level=None,  # autocommit
                )
                self._conn.row_factory = sqlite3.Row
                # busy_timeout MUST be set before any other PRAGMA.
                self._conn.execute("PRAGMA busy_timeout=10000")
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                break
            except sqlite3.OperationalError as e:
                last_err = e
                msg = str(e).lower()
                if "database is locked" in msg or "busy" in msg:
                    _t.sleep(0.1 * (2 ** attempt))
                    continue
                raise
        else:
            if last_err is not None:
                raise last_err
        self._has_fts5 = _check_fts5(self._conn)
        self._telemetry = telemetry
        self._init_schema()

    # ---- schema / migrations ------------------------------------------------

    def _init_schema(self) -> None:
        # The outer _SCHEMA_INIT_LOCK serializes DDL across MemoryStore
        # instances on the same interpreter. Per-instance _lock still
        # guards transactional semantics of the shared _conn object.
        #
        # Under extreme concurrency (>8 simultaneous opens) FTS5 can
        # still transiently fail with 'vtable constructor failed' even
        # with the lock because module registration crosses connections.
        # Retry up to 3 times with short backoff.
        import time as _t
        last_err: sqlite3.OperationalError | None = None
        for attempt in range(3):
            try:
                self._do_init_schema()
                return
            except sqlite3.OperationalError as e:
                last_err = e
                msg = str(e).lower()
                if ("vtable constructor" in msg
                        or "database is locked" in msg):
                    _t.sleep(0.05 * (attempt + 1))
                    continue
                raise
        if last_err is not None:
            raise last_err

    def _do_init_schema(self) -> None:
        with _SCHEMA_INIT_LOCK, self._lock:
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
        """Insert a new memory row. Returns the row id.

        Retries on SQLite transient lock errors (up to 3 attempts with
        exponential backoff). Under extreme concurrency (12+
        simultaneous writers on the same file), WAL's single-writer
        serialization can still surface `database is locked` even
        with `busy_timeout=5s` — the retry covers that edge.
        """
        import time as _t
        now = _utcnow()
        meta_json = json.dumps(metadata, default=str) if metadata else None
        last_err: sqlite3.OperationalError | None = None
        # 5 retries with exponential backoff: 100/200/400/800/1600 ms
        # = ~3 s total retry window, well under the 10 s busy_timeout.
        for attempt in range(5):
            try:
                with self._lock:
                    cur = self._conn.execute(
                        """INSERT INTO memories
                           (created_utc, updated_utc, source, tier, kind, content, metadata_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (now, now, source, tier, kind, content, meta_json),
                    )
                break
            except sqlite3.OperationalError as e:
                last_err = e
                msg = str(e).lower()
                if "database is locked" in msg or "busy" in msg:
                    _t.sleep(0.1 * (2 ** attempt))
                    continue
                raise
        else:
            if last_err is not None:
                raise last_err
        rid = cur.lastrowid
        self._emit("memory_write", memory_id=rid, source=source, kind=kind,
                   tier=tier, content_len=len(content))
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

    # ---- git-backed autobiography export -----------------------------------

    def export_to_git(
        self,
        target_dir: Path,
        *,
        tier_min: int = L2_WARM,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Dump memories to a directory as markdown files + git commit.

        Each memory becomes one `<tier>/<id>-<slug>.md` file with yaml-ish
        frontmatter and the content as the body. The target directory is
        initialized as a git repo on first export; subsequent exports
        amend the repo with new commits — a browsable agent autobiography.

        Inspired by agentic-stack's "git history of .agent/memory/ as
        the agent's autobiography" pattern. Works without git installed
        (just skips the commit step) but is much more useful with it.

        Returns {"count", "repo", "commit" (optional)}.
        """
        import re as _re
        import shutil as _sh
        import subprocess as _sp
        target_dir = Path(target_dir).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

        # Initialize git repo if the target isn't already one. Configure
        # user identity locally so `git commit` works even when the
        # system has no global git config (CI, containers, fresh VMs).
        git_ok = _sh.which("git") is not None
        is_git = (target_dir / ".git").exists()
        if git_ok and not is_git:
            _sp.run(["git", "init", "--quiet", "--initial-branch=main"],
                    cwd=str(target_dir), capture_output=True)
            _sp.run(["git", "config", "user.email",
                     "autobiography@mnemosyne.local"],
                    cwd=str(target_dir), capture_output=True)
            _sp.run(["git", "config", "user.name",
                     "Mnemosyne Autobiography"],
                    cwd=str(target_dir), capture_output=True)

        # Query memories
        with self._lock:
            sql = ("SELECT id, created_utc, updated_utc, source, tier, kind, "
                   "content, metadata_json, access_count "
                   "FROM memories WHERE tier >= ?")
            params: list[Any] = [tier_min]
            if since:
                sql += " AND created_utc >= ?"
                params.append(since)
            sql += " ORDER BY created_utc ASC"
            rows = self._conn.execute(sql, params).fetchall()

        # Write each memory as a markdown file under tier subdir
        count = 0
        for r in rows:
            tier = r["tier"]
            tier_dir = target_dir / f"L{tier}"
            tier_dir.mkdir(parents=True, exist_ok=True)
            # Slug from first ~8 words of content
            slug_src = (r["content"] or "").split()[:8]
            slug = "-".join(_re.sub(r"[^a-zA-Z0-9]+", "", w)
                            for w in slug_src).lower()[:40] or "memory"
            fname = f"{r['id']:06d}-{slug}.md"
            fpath = tier_dir / fname
            fm = [
                "---",
                f"id: {r['id']}",
                f"tier: L{r['tier']}",
                f"kind: {r['kind']}",
                f"source: {r['source']}",
                f"created_utc: {r['created_utc']}",
                f"updated_utc: {r['updated_utc']}",
                f"access_count: {r['access_count']}",
                "---",
                "",
                r["content"] or "",
                "",
            ]
            fpath.write_text("\n".join(fm), encoding="utf-8")
            count += 1

        # Commit — only if git is available AND there's content to commit
        commit_sha: str | None = None
        if git_ok and (target_dir / ".git").exists() and count:
            _sp.run(["git", "add", "-A"], cwd=str(target_dir),
                    capture_output=True)
            # Ensure identity is set (pre-existing repos may have no config)
            check = _sp.run(["git", "config", "user.email"],
                             cwd=str(target_dir), capture_output=True,
                             text=True)
            if not check.stdout.strip():
                _sp.run(["git", "config", "user.email",
                         "autobiography@mnemosyne.local"],
                        cwd=str(target_dir), capture_output=True)
                _sp.run(["git", "config", "user.name",
                         "Mnemosyne Autobiography"],
                        cwd=str(target_dir), capture_output=True)
            now_iso = _utcnow()
            r = _sp.run(
                ["git", "commit", "-m",
                 f"export: {count} memories ({now_iso})"],
                cwd=str(target_dir), capture_output=True, text=True,
            )
            if r.returncode == 0:
                rr = _sp.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(target_dir), capture_output=True,
                             text=True)
                commit_sha = rr.stdout.strip() or None

        return {
            "count": count,
            "repo": str(target_dir),
            "commit": commit_sha,
            "git_available": git_ok,
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

    ep = sub.add_parser("export",
                          help="export memories to a git-backed "
                               "autobiography (one markdown file per row)")
    ep.add_argument("--to-git", required=True,
                     help="target directory; initialized as a git repo if new")
    ep.add_argument("--tier-min", type=int, default=2,
                     help="lowest tier to include (default: 2 = L2 warm and up)")
    ep.add_argument("--since", default=None,
                     help="ISO date; only export memories newer than this")

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
    elif args.cmd == "export":
        result = mem.export_to_git(Path(args.to_git).expanduser(),
                                     tier_min=args.tier_min,
                                     since=args.since)
        print(f"exported {result['count']} memories to {result['repo']}")
        print(f"commit: {result.get('commit', 'no commit (git not available)')}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
