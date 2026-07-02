"""
bench/efficiency_frontier.py — the "break the norm" benchmark (v0.9.8).

The agent-memory leaderboard (Mem0, Zep, Honcho, …) races a single number:
LLM-judged answer accuracy on LOCOMO / LongMemEval. That race hides the
costs that actually decide whether memory is *usable in a live agent loop*:
how many tokens you spend per answer, how much that costs, how long the
retrieval takes, and whether any of it runs locally.

This benchmark reframes the question. It sweeps retrieval depth (top-k) and
reports the **efficiency frontier**:

  * answer-in-context accuracy vs. context tokens spent per probe,
  * **answer utility per 1k context tokens** — the norm-breaking metric,
  * tokens needed to reach an accuracy target,
  * a cost model — $ per 1,000 questions at a configurable answer-LLM input
    price — for the retrieved context vs. the full-conversation baseline,
  * search latency p50/p95 at each depth.

Everything runs locally against Mnemosyne's SQLite FTS5 substrate: no LLM,
no API key, no network, $0. It reuses the LOCOMO runner's retrieval path so
the numbers are the same substrate the LOCOMO track measures.

The full-conversation baseline (every turn stuffed into context) is the
"norm" this breaks: published LOCOMO conversations average ~26k tokens
(Mem0, arXiv 2504.19413). We report our token load as a fraction of that.

Usage:
    curl -L https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json \\
        -o bench/data/locomo10.json
    python3 bench/efficiency_frontier.py \\
        --out bench/results/efficiency-frontier.json --verbose
    # cost model at a different answer-LLM price (default illustrative):
    python3 bench/efficiency_frontier.py --input-price-per-1m 0.15
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from locomo import (  # noqa: E402
    _RUNNER_VERSION,
    _git_commit,
    load_locomo,
    run as locomo_run,
    MnemosyneSubstrate,
)

# Published reference: mean tokens per LOCOMO conversation when the whole
# history is stuffed into context. Source: Mem0 paper (arXiv 2504.19413),
# "~600 dialogues and 26,000 tokens on average". Used only for the
# fraction-of-full-context framing, never as a Mnemosyne result.
_FULL_CONTEXT_TOKENS_REF = 26_000


def _score_at_k(dataset_path: Path | None, k: int, db_path: Path,
                ) -> dict[str, Any]:
    """Run the LOCOMO retrieval track at a single top-k; return key metrics."""
    samples, _sha = load_locomo(path=dataset_path)
    if db_path.exists():
        db_path.unlink()
    sub = MnemosyneSubstrate(db_path=db_path, llm_grounded=False,
                             retrieval_mode="fts", top_k=k)
    try:
        report = locomo_run(sub, samples, judge="substring",
                            llm_grounded=False)
    finally:
        sub.close()
    tokens = report["tokens"]["context_per_probe_mean_est"]
    return {
        "k": k,
        "answer_in_context": report["score"],
        "evidence_recall": report["evidence_recall"]["mean"],
        "context_tokens_per_probe": tokens,
        "search_p50_ms": report["latency"]["search"]["p50_ms"],
        "search_p95_ms": report["latency"]["search"]["p95_ms"],
        "questions": report["total"],
    }


def _tokens_to_reach(frontier: list[dict[str, Any]],
                     target: float) -> dict[str, Any] | None:
    """Smallest-k point that reaches `target` answer-in-context, if any."""
    for row in sorted(frontier, key=lambda r: r["k"]):
        if row["answer_in_context"] >= target:
            return {"target": target, "k": row["k"],
                    "context_tokens_per_probe": row["context_tokens_per_probe"]}
    return None


def build_frontier(dataset_path: Path | None, ks: list[int], db_path: Path,
                   *, input_price_per_1m: float, verbose: bool = False,
                   ) -> dict[str, Any]:
    frontier: list[dict[str, Any]] = []
    for k in ks:
        t0 = time.monotonic()
        row = _score_at_k(dataset_path, k, db_path)
        # Answer utility per 1k context tokens — the headline efficiency
        # metric: how much correct-answer coverage each 1k tokens buys.
        tok = max(1, row["context_tokens_per_probe"])
        row["utility_per_1k_tokens"] = round(
            row["answer_in_context"] / (tok / 1000.0), 4)
        row["pct_of_full_context_tokens"] = round(
            100.0 * tok / _FULL_CONTEXT_TOKENS_REF, 3)
        # Cost model: $ per 1,000 questions to feed this retrieved context to
        # an answer LLM at the given input price (retrieval itself is $0).
        row["cost_per_1k_questions_usd"] = round(
            (tok / 1_000_000.0) * input_price_per_1m * 1000.0, 6)
        frontier.append(row)
        if verbose:
            print(f"  k={k:2d}  acc={row['answer_in_context']:.4f}  "
                  f"tok={tok:5d}  util/1k={row['utility_per_1k_tokens']:.4f}  "
                  f"p50={row['search_p50_ms']}ms  "
                  f"({time.monotonic()-t0:.1f}s)", flush=True)

    # Full-context baseline (retrieval_mode=full) — the "norm".
    if verbose:
        print("  full-context ceiling …", flush=True)
    samples, _ = load_locomo(path=dataset_path)
    if db_path.exists():
        db_path.unlink()
    sub = MnemosyneSubstrate(db_path=db_path, llm_grounded=False,
                             retrieval_mode="full")
    try:
        full = locomo_run(sub, samples, judge="substring", llm_grounded=False)
    finally:
        sub.close()
    full_tok = full["tokens"]["context_per_probe_mean_est"]
    full_row = {
        "answer_in_context": full["score"],
        "context_tokens_per_probe": full_tok,
        "cost_per_1k_questions_usd": round(
            (full_tok / 1_000_000.0) * input_price_per_1m * 1000.0, 6),
    }

    # Norm-breaking summary: best retrieved point vs the full-context norm.
    best = max(frontier, key=lambda r: r["utility_per_1k_tokens"])
    knee = max(frontier, key=lambda r: r["answer_in_context"])  # deepest k
    token_savings = (1 - knee["context_tokens_per_probe"] / full_tok
                     if full_tok else 0.0)
    return {
        "frontier": frontier,
        "full_context_baseline": full_row,
        "tokens_to_reach": [
            t for t in (_tokens_to_reach(frontier, x) for x in (0.5, 0.6, 0.7))
            if t is not None
        ],
        "headline": {
            "most_efficient_point": {
                "k": best["k"],
                "answer_in_context": best["answer_in_context"],
                "context_tokens_per_probe": best["context_tokens_per_probe"],
                "utility_per_1k_tokens": best["utility_per_1k_tokens"],
            },
            "deepest_point": {
                "k": knee["k"],
                "answer_in_context": knee["answer_in_context"],
                "context_tokens_per_probe": knee["context_tokens_per_probe"],
                "pct_of_ceiling_accuracy": (
                    round(100.0 * knee["answer_in_context"]
                          / full_row["answer_in_context"], 1)
                    if full_row["answer_in_context"] else None),
                "token_savings_vs_full_pct": round(100.0 * token_savings, 2),
            },
            "cost_ratio_full_over_deepest": (
                round(full_row["cost_per_1k_questions_usd"]
                      / knee["cost_per_1k_questions_usd"], 1)
                if knee["cost_per_1k_questions_usd"] else None),
        },
    }


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bench/efficiency_frontier.py",
        description="Token/cost/latency efficiency frontier for the Mnemosyne "
                    "retrieval substrate on LOCOMO.")
    p.add_argument("--dataset", default=None,
                   help="path to locomo10.json (default bench/data/)")
    p.add_argument("--ks", default="2,4,8,16,32",
                   help="comma-separated top-k values to sweep")
    p.add_argument("--input-price-per-1m", type=float, default=0.15,
                   help="answer-LLM input $ per 1M tokens for the cost model "
                        "(default 0.15, illustrative; set to your provider's "
                        "price). Retrieval itself is always $0.")
    p.add_argument("--db-path", default="/tmp/efficiency-frontier.db")
    p.add_argument("--out", default="bench/results/efficiency-frontier.json")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    dataset = Path(args.dataset) if args.dataset else None
    print(f"[frontier] sweeping k={ks} "
          f"(cost model @ ${args.input_price_per_1m}/1M input tokens)",
          flush=True)
    result = build_frontier(dataset, ks, Path(args.db_path),
                            input_price_per_1m=args.input_price_per_1m,
                            verbose=args.verbose)
    result["_metadata"] = {
        "benchmark": "LOCOMO efficiency frontier (retrieval substrate)",
        "runner": f"bench/efficiency_frontier.py v{_RUNNER_VERSION}",
        "full_context_tokens_ref": _FULL_CONTEXT_TOKENS_REF,
        "full_context_tokens_ref_source": "Mem0 arXiv:2504.19413 (~26k tok/conv)",
        "input_price_per_1m_usd": args.input_price_per_1m,
        "retrieval_cost_usd": 0.0,
        "git_commit": _git_commit(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "date_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))

    h = result["headline"]
    print("\n=== efficiency frontier ===")
    print(f"{'k':>3} {'acc':>7} {'tokens':>7} {'util/1k':>8} "
          f"{'%full-tok':>9} {'$/1k-q':>9} {'p50ms':>6}")
    for r in result["frontier"]:
        print(f"{r['k']:>3} {r['answer_in_context']:>7.4f} "
              f"{r['context_tokens_per_probe']:>7} "
              f"{r['utility_per_1k_tokens']:>8.4f} "
              f"{r['pct_of_full_context_tokens']:>8.3f}% "
              f"{r['cost_per_1k_questions_usd']:>9.5f} "
              f"{r['search_p50_ms']:>6}")
    fb = result["full_context_baseline"]
    print(f"full {fb['answer_in_context']:>7.4f} "
          f"{fb['context_tokens_per_probe']:>7} "
          f"{'—':>8} {'100.000':>8}% {fb['cost_per_1k_questions_usd']:>9.5f}")
    d = h["deepest_point"]
    print(f"\nnorm-break: k={d['k']} reaches {d['pct_of_ceiling_accuracy']}% of "
          f"full-context accuracy on {d['token_savings_vs_full_pct']}% fewer "
          f"tokens; full context costs {h['cost_ratio_full_over_deepest']}× "
          f"more per 1k questions.")
    print(f"[frontier] full report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
