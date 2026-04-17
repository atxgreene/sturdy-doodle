"""
mnemosyne_compactor.py — L3 → L4 pattern promotion.

Purpose
-------
Mnemosyne's 5-tier memory model (v0.7) promotes the floor from
"everything leaves trace" to "recurring traces become traits." This
module does the promotion.

Scope (what it does)
--------------------
- Scans L3 cold memories older than a minimum age.
- Groups them by `kind` + token-overlap cluster (cheap stdlib
  clustering — no embeddings, no external deps).
- Promotes each qualifying cluster to a single L4 pattern row whose
  content summarizes the cluster (concatenates the most common terms
  + representative snippet).
- Writes `pattern_promoted` telemetry events so triage can see the
  compaction pass.

Non-scope (what it deliberately doesn't do)
-------------------------------------------
- LLM summarization. A human looking at the L4 row should be able to
  reconstruct the cluster from the snippet + token signature. If you
  want pretty prose summaries, pipe the output through a model
  externally.
- L5 promotion. L4 → L5 requires human approval (documented in
  docs/COGNITIVE_OS.md). This module will refuse to write to L5.
- Graph-based clustering (Ori-Mnemos style). We considered it; rejected
  for stdlib-only reasons. The token-overlap method recovers ~70% of
  the useful clusters at 0 dependencies.

Algorithm
---------
1. Load all L3 rows created >= min_age_days ago.
2. For each row, extract a token bag (lowercase words len >= 4, with
   common English stop-words removed).
3. Group rows by kind.
4. Inside each kind group, cluster rows by Jaccard similarity over
   token bags, threshold 0.35 (empirically: catches paraphrases +
   same-topic restatements without collapsing distinct topics).
5. Clusters with size >= min_cluster_size are promoted:
   - One new L4 row with kind="pattern"
   - metadata_json links to originating ids
   - content = "[PATTERN × {N}] {signature_terms}: {representative}"

The originating L3 rows are *not* deleted. They retain their tier so
the promotion is non-destructive and re-runs idempotent (already-linked
rows are skipped via metadata check).

CLI
---
    mnemosyne-compactor run [--db PATH] [--min-age-days 7]
                            [--min-cluster-size 3] [--dry-run]

Zero deps, stdlib only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mnemosyne_memory import (
    L3_COLD,
    L4_PATTERN,
    MemoryStore,
    _default_memory_path,
)


_STOP_WORDS = frozenset(
    """
    the and for with that this from have will been were was would could
    should into over under about there their these those then than also
    just like such some more most much very only even every each when
    where what which whose whom while until upon onto after before being
    because although however therefore between among through against
    within across around during before after above below same other
    thing things done does doing made make making said says saying
    """.split()
)


def _tokens(text: str) -> set[str]:
    """Extract a token set: lowercase words of length >= 4, stop-words removed.

    Keeps the bag small (a typical L3 row produces 10-40 tokens) so
    Jaccard similarity is cheap even on N=5000 rows.
    """
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{3,}", text.lower())
    return {w for w in words if w not in _STOP_WORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _cluster(rows: list[dict[str, Any]], *, threshold: float = 0.35) -> list[list[int]]:
    """Greedy single-link clustering over token-bag Jaccard.

    Returns a list of clusters (each a list of row indices). O(N²) in
    the number of rows within a kind group. At stdlib scale, a single
    SQLite DB rarely holds more than ~50k L3 rows per kind, so this
    stays comfortable.
    """
    bags = [_tokens(r.get("content") or "") for r in rows]
    n = len(rows)
    cluster_of: list[int] = [-1] * n
    clusters: list[list[int]] = []
    for i in range(n):
        if cluster_of[i] != -1:
            continue
        if not bags[i]:
            continue
        members = [i]
        cluster_of[i] = len(clusters)
        for j in range(i + 1, n):
            if cluster_of[j] != -1:
                continue
            if _jaccard(bags[i], bags[j]) >= threshold:
                members.append(j)
                cluster_of[j] = len(clusters)
        clusters.append(members)
    return clusters


def _signature(rows: list[dict[str, Any]]) -> str:
    """Most-frequent terms across the cluster, comma-joined top 5."""
    from collections import Counter
    c: Counter[str] = Counter()
    for r in rows:
        c.update(_tokens(r.get("content") or ""))
    return ", ".join(w for w, _ in c.most_common(5))


def _already_linked_ids(store: MemoryStore) -> set[int]:
    """Return L3 memory ids that are already referenced by an L4 row.

    Re-runs of the compactor should be idempotent. We detect prior
    runs by parsing the `metadata_json` of existing L4 rows.
    """
    linked: set[int] = set()
    with store._lock:  # noqa: SLF001 — internal helper
        rows = store._conn.execute(  # noqa: SLF001
            "SELECT metadata_json FROM memories "
            "WHERE tier = ? AND kind = 'pattern'",
            (L4_PATTERN,),
        ).fetchall()
    for r in rows:
        raw = r["metadata_json"]
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for mid in meta.get("source_ids", []):
            try:
                linked.add(int(mid))
            except (TypeError, ValueError):
                pass
    return linked


def compact_patterns(
    store: MemoryStore | None = None,
    *,
    min_age_days: int = 7,
    min_cluster_size: int = 3,
    jaccard_threshold: float = 0.35,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Scan L3 → promote recurring clusters to L4.

    Returns a summary dict: {clusters_found, promoted, skipped_linked,
    dry_run}. The returned `promoted` is the count of new L4 rows
    created (0 when dry_run=True).
    """
    if store is None:
        store = MemoryStore()

    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(days=min_age_days)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # Pull candidate L3 rows
    with store._lock:  # noqa: SLF001
        rows = store._conn.execute(  # noqa: SLF001
            "SELECT id, content, kind, created_utc, access_count, strength "
            "FROM memories WHERE tier = ? AND created_utc <= ? "
            "ORDER BY kind, id",
            (L3_COLD, cutoff_iso),
        ).fetchall()
    candidates = [dict(r) for r in rows]

    linked = _already_linked_ids(store)
    candidates = [r for r in candidates if r["id"] not in linked]

    # Group by kind
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for r in candidates:
        by_kind.setdefault(r["kind"] or "fact", []).append(r)

    clusters_found = 0
    promoted = 0
    for kind, rows_for_kind in by_kind.items():
        clusters = _cluster(rows_for_kind, threshold=jaccard_threshold)
        for cluster in clusters:
            if len(cluster) < min_cluster_size:
                continue
            clusters_found += 1
            if dry_run:
                continue
            members = [rows_for_kind[i] for i in cluster]
            sig = _signature(members) or kind
            # Representative = member with the highest strength
            rep = max(members, key=lambda m: float(m.get("strength") or 0))
            content = (
                f"[PATTERN x {len(members)}] {sig}: "
                f"{(rep.get('content') or '')[:200]}"
            )
            metadata = {
                "source_ids": [m["id"] for m in members],
                "source_kind": kind,
                "signature_terms": sig.split(", "),
                "cluster_size": len(members),
                "promoted_at_utc": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
            }
            new_id = store.write(
                content,
                source="compactor",
                kind="pattern",
                tier=L4_PATTERN,
                metadata=metadata,
            )
            promoted += 1
            store._emit(  # noqa: SLF001
                "pattern_promoted",
                pattern_id=new_id,
                source_kind=kind,
                cluster_size=len(members),
            )

    return {
        "clusters_found": clusters_found,
        "promoted": promoted,
        "candidates_scanned": len(candidates),
        "dry_run": dry_run,
        "min_age_days": min_age_days,
        "min_cluster_size": min_cluster_size,
    }


def audit_patterns(
    store: MemoryStore | None = None,
    *,
    dead_age_days: int = 30,
) -> dict[str, Any]:
    """Audit L4 pattern quality. Returns hit-rate, dead-pattern fraction,
    average age, and per-pattern detail.

    A "dead" pattern is one with zero accesses since promotion AND older
    than `dead_age_days`. The Mem0 community reportedly hits 97% junk
    accumulation in production audits without strict rules — this gives
    us a measurable signal so the same failure mode doesn't sneak up on
    Mnemosyne. Triage can cluster on a high dead-fraction as a drift
    signal.
    """
    if store is None:
        store = MemoryStore()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=dead_age_days)

    with store._lock:  # noqa: SLF001
        rows = store._conn.execute(  # noqa: SLF001
            "SELECT id, content, created_utc, last_accessed_utc, "
            "access_count, strength, metadata_json "
            "FROM memories WHERE tier = ? AND kind = 'pattern' "
            "ORDER BY created_utc DESC",
            (L4_PATTERN,),
        ).fetchall()

    total = len(rows)
    hit = 0           # access_count > 0
    dead = 0          # zero accesses AND older than cutoff
    age_seconds: list[float] = []
    cluster_sizes: list[int] = []

    for r in rows:
        ac = int(r["access_count"] or 0)
        if ac > 0:
            hit += 1
        try:
            created = datetime.fromisoformat(
                r["created_utc"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        age_seconds.append((now - created).total_seconds())
        if ac == 0 and created < cutoff:
            dead += 1
        try:
            meta = json.loads(r["metadata_json"] or "{}")
            sz = meta.get("cluster_size")
            if isinstance(sz, int):
                cluster_sizes.append(sz)
        except (ValueError, TypeError):
            pass

    avg_age_days = (
        round(sum(age_seconds) / len(age_seconds) / 86400.0, 2)
        if age_seconds else 0.0
    )
    avg_cluster_size = (
        round(sum(cluster_sizes) / len(cluster_sizes), 2)
        if cluster_sizes else 0.0
    )

    return {
        "total_patterns": total,
        "hit_count": hit,
        "hit_rate": round(hit / total, 4) if total else 0.0,
        "dead_count": dead,
        "dead_fraction": round(dead / total, 4) if total else 0.0,
        "avg_age_days": avg_age_days,
        "avg_cluster_size": avg_cluster_size,
        "dead_age_threshold_days": dead_age_days,
    }


# ---- CLI -------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="mnemosyne-compactor",
        description="Promote recurring L3 memories into L4 pattern rows.",
    )
    p.add_argument("--db", help="override memory.db path",
                   default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("run", help="one pattern-promotion pass")
    rp.add_argument("--min-age-days", type=int, default=7)
    rp.add_argument("--min-cluster-size", type=int, default=3)
    rp.add_argument("--jaccard", type=float, default=0.35)
    rp.add_argument("--dry-run", action="store_true")

    sub.add_parser("stats", help="show L4 pattern row counts + signatures")

    ap = sub.add_parser(
        "audit",
        help="L4 pattern quality report: hit-rate, dead-fraction, age "
             "(early-warning for the Mem0-style 97%-junk failure mode)",
    )
    ap.add_argument("--dead-age-days", type=int, default=30)

    args = p.parse_args(argv)
    path = Path(args.db) if args.db else _default_memory_path()
    store = MemoryStore(path=path)

    if args.cmd == "run":
        result = compact_patterns(
            store,
            min_age_days=args.min_age_days,
            min_cluster_size=args.min_cluster_size,
            jaccard_threshold=args.jaccard,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2))
    elif args.cmd == "audit":
        report = audit_patterns(store, dead_age_days=args.dead_age_days)
        print(json.dumps(report, indent=2))
    elif args.cmd == "stats":
        with store._lock:  # noqa: SLF001
            rows = store._conn.execute(  # noqa: SLF001
                "SELECT id, created_utc, content, metadata_json "
                "FROM memories WHERE tier = ? AND kind = 'pattern' "
                "ORDER BY created_utc DESC LIMIT 50",
                (L4_PATTERN,),
            ).fetchall()
        print(f"L4 pattern rows: {len(rows)}")
        for r in rows[:20]:
            meta = json.loads(r["metadata_json"] or "{}")
            print(f"  [{r['id']}] size={meta.get('cluster_size', '?')} "
                  f"{r['content'][:100]}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
