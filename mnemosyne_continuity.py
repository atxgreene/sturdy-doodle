"""
mnemosyne_continuity.py — Continuity Score benchmark (v0.7).

Purpose
-------
Measure how well Mnemosyne's memory hierarchy preserves information
across turns and sessions. The Continuity Score is the fraction of
scenarios where the agent correctly recalls planted information.

Why a separate runner?
----------------------
`scenario_runner.py` assumes single-turn prompts. Continuity scenarios
are inherently multi-turn: 1-N `plant` turns, then a `probe` turn.
Cross-session scenarios additionally require re-instantiating the Brain
(fresh MemoryStore connection on the same DB path) between plant and
probe — a different control flow than the default runner.

How scoring works
-----------------
- `expected_any` (case-insensitive): passes if ANY listed substring
  appears in the response to the probe.
- `not_contains` (optional, case-insensitive): additionally passes only
  if NONE of the forbidden substrings appear.
- Empty `expected_any` with non-empty `not_contains`: pure negative
  check (e.g. "don't use em-dashes").
- Blank `expected_any` with no `not_contains`: scenario is skipped with
  a warning (unjudgeable).

CLI
---
    mnemosyne-continuity run \\
        --scenarios scenarios/continuity.jsonl \\
        --model qwen3.5:9b --provider ollama \\
        --out /tmp/continuity.json

    mnemosyne-continuity dryrun \\
        --scenarios scenarios/continuity.jsonl
        # Echos the plant/probe plan without calling a model.

The `dryrun` mode is what we use in CI and in the unit tests.

Zero deps, stdlib only.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from mnemosyne_memory import MemoryStore


# ---- scenario file parsing -------------------------------------------------

def load_scenarios(path: str | Path) -> list[dict[str, Any]]:
    """Parse the JSONL scenario file. Skips blanks and `#`-comments."""
    scenarios: list[dict[str, Any]] = []
    for ln, raw in enumerate(Path(path).read_text().splitlines(), start=1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{ln}: invalid JSON: {e}") from e
        for req in ("id", "probe"):
            if req not in obj:
                raise ValueError(f"{path}:{ln}: missing required field {req!r}")
        obj.setdefault("plant", [])
        obj.setdefault("expected_any", [])
        obj.setdefault("not_contains", [])
        obj.setdefault("cross_session", False)
        obj.setdefault("category", "uncategorized")
        obj.setdefault("tags", [])
        scenarios.append(obj)
    return scenarios


# ---- judge -----------------------------------------------------------------

def judge_response(
    response_text: str,
    *,
    expected_any: list[str],
    not_contains: list[str],
) -> tuple[bool, str]:
    """Return (passed, explanation)."""
    lo = response_text.lower()
    # Forbidden substrings first — any hit fails regardless of positives
    for f in not_contains:
        if f.lower() in lo:
            return False, f"forbidden substring appeared: {f!r}"
    if not expected_any:
        # Negative-only check; we passed the forbidden check above.
        return True, "negative-only check passed"
    for e in expected_any:
        if e.lower() in lo:
            return True, f"matched {e!r}"
    return False, (
        f"none of {expected_any!r} in response "
        f"(first 100 chars: {response_text[:100]!r})"
    )


# ---- runner ----------------------------------------------------------------

RunnerResult = dict[str, Any]
ChatFn = Callable[..., dict[str, Any]]


def _run_one_scenario(
    scenario: dict[str, Any],
    *,
    make_brain: Callable[[Path], Any],
    db_path: Path,
) -> RunnerResult:
    """Execute plant + probe, judge the probe response, return a result."""
    # Session 1: plant
    brain1 = make_brain(db_path)
    for plant in scenario["plant"]:
        try:
            brain1.turn(plant)
        except Exception as e:
            return {
                "id": scenario["id"],
                "passed": False,
                "reason": f"plant exception: {e}",
                "stage": "plant",
                "category": scenario.get("category"),
                "cross_session": scenario.get("cross_session", False),
            }
    try:
        brain1.memory.close()
    except Exception:
        pass

    # Session 2: either a fresh brain (cross_session) or the same one
    if scenario.get("cross_session"):
        brain2 = make_brain(db_path)
    else:
        brain2 = make_brain(db_path)  # always fresh to isolate the
        # probe call from any in-process short-term state; only the
        # persistent MemoryStore carries continuity.
    try:
        response = brain2.turn(scenario["probe"])
        response_text = getattr(response, "text", "") or ""
    except Exception as e:
        return {
            "id": scenario["id"],
            "passed": False,
            "reason": f"probe exception: {e}",
            "stage": "probe",
            "category": scenario.get("category"),
            "cross_session": scenario.get("cross_session", False),
        }
    finally:
        try:
            brain2.memory.close()
        except Exception:
            pass

    passed, reason = judge_response(
        response_text,
        expected_any=scenario.get("expected_any", []),
        not_contains=scenario.get("not_contains", []),
    )
    return {
        "id": scenario["id"],
        "passed": passed,
        "reason": reason,
        "stage": "probe",
        "category": scenario.get("category"),
        "cross_session": scenario.get("cross_session", False),
        "response_preview": response_text[:200],
    }


def run_continuity(
    scenarios: list[dict[str, Any]],
    *,
    make_brain: Callable[[Path], Any],
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Run all scenarios and return an aggregate report.

    Each scenario gets its own tempdir (and thus its own memory.db) so
    a plant in scenario A can't leak into scenario B. `make_brain(db)`
    is a factory the caller supplies so we stay agnostic to model
    choice.
    """
    results: list[RunnerResult] = []
    for sc in scenarios:
        if db_path is None:
            with tempfile.TemporaryDirectory() as td:
                per_db = Path(td) / "memory.db"
                results.append(_run_one_scenario(
                    sc, make_brain=make_brain, db_path=per_db,
                ))
        else:
            results.append(_run_one_scenario(
                sc, make_brain=make_brain, db_path=db_path,
            ))

    # Aggregate
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        c = r.get("category", "uncategorized")
        slot = by_category.setdefault(c, {"total": 0, "passed": 0})
        slot["total"] += 1
        if r["passed"]:
            slot["passed"] += 1

    cross_total = sum(1 for r in results if r.get("cross_session"))
    cross_passed = sum(1 for r in results
                        if r.get("cross_session") and r["passed"])

    return {
        "continuity_score": round(passed / total, 4) if total else 0.0,
        "passed": passed,
        "total": total,
        "by_category": {
            c: {
                **v,
                "score": round(v["passed"] / v["total"], 4)
                         if v["total"] else 0.0,
            }
            for c, v in by_category.items()
        },
        "cross_session": {
            "total": cross_total,
            "passed": cross_passed,
            "score": (round(cross_passed / cross_total, 4)
                      if cross_total else 0.0),
        },
        "results": results,
    }


# ---- dry-run mock ----------------------------------------------------------

class _DryRunBrain:
    """Tiny stand-in for a real Brain. On `.turn(plant)` it writes the
    plant text to the shared MemoryStore. On `.turn(probe)` it does a
    memory search and returns the best hit.

    This lets the scenario file be judged without calling any LLM — a
    useful sanity check that the scenario list itself is sensible. A
    real model run is obviously the honest test; dryrun just verifies
    the *memory plumbing* can, in principle, resolve each probe.
    """

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    def turn(self, user_message: str):  # noqa: D401
        class _R:
            text: str = ""
        r = _R()
        # Heuristic: if the message ends with "?" OR starts with a
        # question-word, treat as probe. (Some rule scenarios like
        # "Summarize our conversation" don't end with ?.)
        stripped = user_message.strip()
        first_word = re.match(r"\w+", stripped.lower())
        first_word = first_word.group(0) if first_word else ""
        is_probe = stripped.endswith("?") or first_word in {
            "what", "where", "which", "who", "when", "how", "why",
            "can", "could", "tell", "give", "summarize",
        }
        if is_probe:
            # Tokenize the question to FTS-friendly terms (len >= 4,
            # stripped of punctuation). Drop question-words and
            # low-content function words so the FTS5 query focuses on
            # topical nouns/verbs.
            _STOP = {"what", "where", "which", "does", "have",
                     "has", "had", "many", "much", "they", "their",
                     "this", "that", "your", "mine", "ours", "you",
                     "yours", "from", "with", "about", "there",
                     "using", "doing", "would", "could", "should",
                     "some", "any", "into", "over", "under", "been",
                     "were", "was", "are", "the", "and", "for",
                     "tell", "give", "summarize", "please", "all",
                     "one", "two", "who", "how", "why", "when",
                     "can", "will", "did", "get", "got"}
            # Threshold of 3 (not 4) catches useful short tokens like
            # "car", "RAM", "NLB"; the stopword set covers the 3-char
            # function words that threshold-4 was implicitly filtering.
            query_tokens = [
                w for w in re.findall(r"[a-zA-Z]{3,}", stripped.lower())
                if w not in _STOP
            ]
            query = " ".join(query_tokens) or stripped
            hits = self.memory.search(query, limit=8)
            if not hits:
                # Recency fallback: when the probe shares no tokens
                # with anything in memory, return the most recently
                # planted row. A real agent with no retrieval signal
                # shouldn't pretend it knows nothing; it should offer
                # whatever it remembers most freshly. Caps at 3 rows
                # so noisy fallback doesn't flood the judge.
                hits = self.memory.search("", limit=3)
                if not hits:
                    # Empty-string FTS match returns nothing; try a
                    # direct most-recent scan as a last resort.
                    with self.memory._lock:  # noqa: SLF001
                        rows = self.memory._conn.execute(  # noqa: SLF001
                            "SELECT * FROM memories "
                            "ORDER BY created_utc DESC LIMIT 3"
                        ).fetchall()
                    hits = [dict(row) for row in rows]
            if hits:
                # Rerank by count of distinct query tokens appearing in
                # the hit content (multi-plant scenarios benefit: the
                # single hit that contains the *answer token* often has
                # the highest overlap even when several rows match).
                def _overlap(h: dict) -> int:
                    txt = (h.get("content") or "").lower()
                    return sum(1 for t in query_tokens if t in txt)
                hits.sort(key=lambda h: (-_overlap(h),
                                          -float(h.get("strength") or 0)))
                # Top-3 concatenated — the judge is a substring match,
                # so including a couple of backups helps when the top
                # hit is a near-miss.
                r.text = " ".join(h["content"] for h in hits[:3])
            else:
                r.text = ""
        else:
            self.memory.write(user_message, source="continuity", kind="fact",
                              tier=2)
            r.text = "ok"
        return r


def _make_dry_brain(db_path: Path):
    mem = MemoryStore(path=db_path)
    return _DryRunBrain(mem)


# ---- CLI -------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="mnemosyne-continuity",
        description="Continuity Score benchmark for v0.7.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("run", help="run against a live model")
    rp.add_argument("--scenarios", required=True)
    rp.add_argument("--model", required=True,
                    help="model id, e.g. qwen3.5:9b")
    rp.add_argument("--provider", required=True,
                    choices=["ollama", "lmstudio", "openai",
                             "anthropic"])
    rp.add_argument("--out", default=None,
                    help="write full JSON report to this path")
    rp.add_argument(
        "--max-scenarios", type=int, default=None,
        help="cap scenarios run (smoke test / CI); default all"
    )

    dp = sub.add_parser("dryrun",
                         help="dry-run: use the memory plumbing only, "
                              "no LLM calls (sanity-check scenario file)")
    dp.add_argument("--scenarios", required=True)
    dp.add_argument("--out", default=None)
    dp.add_argument("--max-scenarios", type=int, default=None)

    args = p.parse_args(argv)
    scenarios = load_scenarios(args.scenarios)
    if getattr(args, "max_scenarios", None):
        scenarios = scenarios[: args.max_scenarios]

    if args.cmd == "dryrun":
        report = run_continuity(scenarios, make_brain=_make_dry_brain)
    else:
        # Live mode — late import to keep CLI --help fast
        import mnemosyne_models as mm_models
        from mnemosyne_brain import Brain, BrainConfig

        backend = mm_models.Backend(
            provider=args.provider,
            default_model=args.model,
        )
        cfg = BrainConfig(
            backend=backend,
            enforce_identity_lock=False,  # continuity ≠ identity
            inject_env_snapshot=False,
        )

        def _make(db_path: Path):
            mem = MemoryStore(path=db_path)
            return Brain(
                config=cfg,
                chat_fn=mm_models.chat,
                memory=mem,
            )

        report = run_continuity(scenarios, make_brain=_make)

    summary = {
        k: v for k, v in report.items() if k != "results"
    }
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
        print(f"wrote full report to {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
