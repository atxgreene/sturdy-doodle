"""
mnemosyne_instinct.py — user-pattern distillation (v0.8).

Purpose
-------
Turn recurring user-interaction signals into a small set of fast-path
"user_instinct" rows that the Brain consults on every turn. This is the
substrate-level analogue of procedural / habituated reactions: deliberate
reflection (compactor + dreams + this distiller) gradually shapes
automatic behavior (the system-prompt block injected before query-time
retrieval runs).

Design choices
--------------
- **Not a sixth tier.** Instinct rows live in L4 with `kind="user_instinct"`
  and `source="instinct"`. Same `memories` table, same schema. The "fast
  path" is a Brain-level read pattern, not a storage layer.
- **Idempotent.** Every distill pass first deletes the prior batch of
  user-instinct rows, then writes a fresh batch. Re-running the distiller
  doesn't double-count.
- **Capped.** `max_instincts` (default 20) keeps the system-prompt
  injection budget bounded — typically <500 tokens.
- **Stdlib only.** No clustering deps. We reuse the same token-bag /
  Jaccard machinery the compactor uses; in fact, we delegate to the
  compactor's helpers directly.
- **Source signals.** We look at recently-written rows whose kinds are
  user-pattern-bearing: `preference`, `fact`, `event`, `tool_result`,
  `interest`. We deliberately do NOT scan `failure_note` or `turn` —
  those are operational noise, not user-style signal.

CLI
---
    mnemosyne-instinct distill [--db PATH] [--lookback-days 14]
                               [--min-cluster-size 2]
                               [--max-instincts 20]
                               [--dry-run]

    mnemosyne-instinct list      # show current user-instinct rows
    mnemosyne-instinct clear     # nuke all user-instinct rows

Zero deps, stdlib only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mnemosyne_compactor import _cluster, _signature
from mnemosyne_memory import (
    L4_PATTERN,
    MemoryStore,
    _default_memory_path,
)


# Kinds we consider as user-pattern-bearing signals. Operational kinds
# (failure_note, tool_result errors, turn) are excluded — they're noise
# not signal.
_INSTINCT_SOURCE_KINDS: frozenset[str] = frozenset({
    "preference",
    "fact",
    "interest",
    "event",
    "project",
})


def _utc_iso(dt: datetime | None = None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _existing_instinct_ids(store: MemoryStore) -> list[int]:
    """Return ids of all rows currently marked as user-instinct."""
    with store._lock:  # noqa: SLF001
        rows = store._conn.execute(  # noqa: SLF001
            "SELECT id FROM memories WHERE kind = 'user_instinct'"
        ).fetchall()
    return [int(r["id"]) for r in rows]


def clear_instincts(store: MemoryStore) -> int:
    """Delete every user_instinct row. Returns count deleted."""
    ids = _existing_instinct_ids(store)
    if not ids:
        return 0
    with store._lock:  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            f"DELETE FROM memories WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
    return len(ids)


def distill(
    store: MemoryStore | None = None,
    *,
    lookback_days: int = 14,
    min_cluster_size: int = 2,
    max_instincts: int = 20,
    jaccard_threshold: float = 0.30,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Scan recent user-pattern-bearing rows, cluster by topic, and
    write the top-N recurring patterns as L4 user-instinct rows.

    Returns a summary dict: {clusters_found, written, deleted_prior,
    candidates_scanned, dry_run}.

    Idempotent: each pass deletes the existing user-instinct batch
    before writing a new one. Re-running is safe and cheap.
    """
    if store is None:
        store = MemoryStore()

    cutoff_iso = _utc_iso(
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    )

    # Pull candidate signal rows
    placeholders = ",".join("?" * len(_INSTINCT_SOURCE_KINDS))
    params: list[Any] = list(_INSTINCT_SOURCE_KINDS) + [cutoff_iso]
    with store._lock:  # noqa: SLF001
        rows = store._conn.execute(  # noqa: SLF001
            f"SELECT id, content, kind, created_utc, access_count, "
            f"strength FROM memories "
            f"WHERE kind IN ({placeholders}) AND created_utc >= ? "
            f"ORDER BY id",
            params,
        ).fetchall()
    candidates = [dict(r) for r in rows]

    # Cluster by token overlap
    clusters = _cluster(candidates, threshold=jaccard_threshold)
    # Filter to qualifying clusters and sort by size descending
    qualifying = sorted(
        (c for c in clusters if len(c) >= min_cluster_size),
        key=len,
        reverse=True,
    )[:max_instincts]

    deleted_prior = 0
    written = 0
    if not dry_run:
        deleted_prior = clear_instincts(store)
        for cluster in qualifying:
            members = [candidates[i] for i in cluster]
            sig = _signature(members) or members[0]["kind"]
            rep = max(members, key=lambda m: float(m.get("strength") or 0))
            content = (
                f"[INSTINCT x {len(members)}] {sig}: "
                f"{(rep.get('content') or '')[:160]}"
            )
            metadata = {
                "source_ids": [m["id"] for m in members],
                "signature_terms": sig.split(", "),
                "cluster_size": len(members),
                "distilled_at_utc": _utc_iso(),
                "lookback_days": lookback_days,
            }
            new_id = store.write(
                content,
                source="instinct",
                kind="user_instinct",
                tier=L4_PATTERN,
                metadata=metadata,
            )
            written += 1
            store._emit(  # noqa: SLF001
                "instinct_distilled",
                instinct_id=new_id,
                cluster_size=len(members),
            )

    return {
        "clusters_found": len(qualifying),
        "written": written,
        "deleted_prior": deleted_prior,
        "candidates_scanned": len(candidates),
        "lookback_days": lookback_days,
        "min_cluster_size": min_cluster_size,
        "max_instincts": max_instincts,
        "dry_run": dry_run,
    }


def list_instincts(store: MemoryStore) -> list[dict[str, Any]]:
    """Return all current user-instinct rows ordered by strength desc."""
    with store._lock:  # noqa: SLF001
        rows = store._conn.execute(  # noqa: SLF001
            "SELECT id, content, strength, created_utc, metadata_json "
            "FROM memories WHERE kind = 'user_instinct' "
            "ORDER BY strength DESC, created_utc DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---- CLI -------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="mnemosyne-instinct",
        description="Distill user-pattern instincts into fast-path L4 rows.",
    )
    p.add_argument("--db", default=None,
                   help="override memory.db path")
    sub = p.add_subparsers(dest="cmd", required=True)

    dp = sub.add_parser("distill",
                         help="one user-pattern distillation pass")
    dp.add_argument("--lookback-days", type=int, default=14)
    dp.add_argument("--min-cluster-size", type=int, default=2)
    dp.add_argument("--max-instincts", type=int, default=20)
    dp.add_argument("--jaccard", type=float, default=0.30)
    dp.add_argument("--dry-run", action="store_true")

    sub.add_parser("list", help="show current user-instinct rows")
    sub.add_parser("clear", help="delete all user-instinct rows")

    args = p.parse_args(argv)
    path = Path(args.db) if args.db else _default_memory_path()
    store = MemoryStore(path=path)

    if args.cmd == "distill":
        result = distill(
            store,
            lookback_days=args.lookback_days,
            min_cluster_size=args.min_cluster_size,
            max_instincts=args.max_instincts,
            jaccard_threshold=args.jaccard,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2))
    elif args.cmd == "list":
        rows = list_instincts(store)
        print(f"user_instinct rows: {len(rows)}")
        for r in rows:
            print(f"  [{r['id']}] s={r['strength']:.2f}  "
                  f"{(r['content'] or '')[:120]}")
    elif args.cmd == "clear":
        deleted = clear_instincts(store)
        print(f"deleted {deleted} user_instinct rows")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
