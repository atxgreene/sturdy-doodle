"""
bench/locomo.py — LOCOMO benchmark runner skeleton (v0.8).

LOCOMO (Long-form Conversation Memory) is the standard public benchmark
for agent memory systems. Ten ~600-turn dialogues, four question types
(single-hop, multi-hop, temporal, open-domain), LLM-as-judge scoring.

This file is **not part of the Mnemosyne distribution.** It lives in
bench/ so the methodology is reproducible, but it depends on the
optional bench/requirements.txt (datasets, openai, mem0ai, etc.) and
is not run by the main test suite.

Status: skeleton only. The plumbing (load → ingest → probe → judge →
report) is wired up, but the LLM-as-judge call and the actual model
backend are intentionally left as `TODO` markers because the choice of
model + temperature is the dominant source of variance in published
results, and we don't want to ship a single hardcoded answer.

Usage (after installing bench/requirements.txt into bench/.venv):

    python bench/locomo.py \\
        --provider ollama --model qwen2.5:7b \\
        --max-conversations 2 \\
        --out bench/results/mnemosyne-locomo.json

To run the same scenarios through Mem0 for a head-to-head:

    python bench/locomo.py --substrate mem0 \\
        --out bench/results/mem0-locomo.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Ensure the repo root is importable when bench/locomo.py runs as a
# script from the repo. (No package install assumed.)
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Mnemosyne substrate (always available in this repo)
from mnemosyne_memory import MemoryStore  # noqa: E402


# ---------------------------------------------------------------------------
# Substrate adapters
# ---------------------------------------------------------------------------

class MnemosyneSubstrate:
    """Thin LOCOMO adapter for Mnemosyne's MemoryStore + (optional) Brain.

    Two modes:
      - retrieval-only: write each conversation turn to L2; on probe,
        run search() + concat top-k. No LLM. This measures the substrate
        in isolation (analogous to our Continuity Score dryrun).
      - llm-grounded:   same writes, but on probe call mnemosyne_models
        with retrieved context. This measures the full agent stack.
    """

    def __init__(self, *, db_path: Path, llm_grounded: bool = False,
                 provider: str | None = None,
                 model: str | None = None) -> None:
        self.memory = MemoryStore(path=db_path)
        self.llm_grounded = llm_grounded
        self.provider = provider
        self.model = model

    def ingest(self, turn: dict[str, Any]) -> None:
        # LOCOMO turns have {speaker, text, timestamp}. Store as L2.
        text = f"{turn.get('speaker', '?')}: {turn.get('text', '')}"
        self.memory.write(text, source="locomo", kind="event", tier=2)

    def probe(self, question: str) -> str:
        hits = self.memory.search(question, limit=8)
        context = "\n".join(h["content"] for h in hits[:5])
        if not self.llm_grounded:
            return context
        # TODO: pass context as system prompt + question as user message
        # to mnemosyne_models.chat() with the configured backend.
        # Left intentionally unwired so the runner is portable.
        raise NotImplementedError(
            "llm_grounded mode requires a configured backend. "
            "Wire mnemosyne_models.chat() here once you've picked "
            "the model + provider you want to publish numbers for."
        )

    def close(self) -> None:
        self.memory.close()


class Mem0Substrate:
    """Mem0 LOCOMO adapter — only importable when mem0ai is installed."""

    def __init__(self, *, openai_api_key: str | None = None) -> None:
        try:
            from mem0 import Memory  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "mem0ai not installed. Run "
                "`pip install -r bench/requirements.txt` first."
            ) from e
        # Default config — caller can override via env vars per Mem0 docs
        self.client = Memory()
        self.user_id = "locomo-bench"

    def ingest(self, turn: dict[str, Any]) -> None:
        text = f"{turn.get('speaker', '?')}: {turn.get('text', '')}"
        self.client.add(text, user_id=self.user_id)

    def probe(self, question: str) -> str:
        hits = self.client.search(question, user_id=self.user_id, limit=8)
        # Mem0 returns dict-shaped results
        if isinstance(hits, dict):
            hits = hits.get("results", [])
        return "\n".join(
            (h.get("memory") or h.get("text") or "") for h in hits[:5]
        )

    def close(self) -> None:
        pass  # Mem0 client manages its own backend lifecycle


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

def load_locomo(*, max_conversations: int | None = None) -> list[dict[str, Any]]:
    """Load the LOCOMO dataset from Hugging Face. Returns a list of
    conversation dicts: {id, turns, questions}.

    Requires `datasets` (pip install -r bench/requirements.txt).
    """
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "datasets not installed. Run "
            "`pip install -r bench/requirements.txt` first."
        ) from e
    # The published LOCOMO mirror; verify the path matches when you run.
    ds = load_dataset("snap-stanford/locomo", split="test")
    rows = list(ds)
    if max_conversations is not None:
        rows = rows[:max_conversations]
    return rows


# ---------------------------------------------------------------------------
# LLM-as-judge
# ---------------------------------------------------------------------------

def llm_judge(question: str, expected: str, actual: str) -> bool:
    """Score a single answer with an LLM judge. Returns True if the
    actual answer captures the expected information.

    Default judge: gpt-4o-mini at temperature 0. Override by patching
    this function for reproducibility-budgeted runs (e.g. pin to a
    specific snapshot).
    """
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai not installed. Run "
            "`pip install -r bench/requirements.txt` first."
        ) from e
    client = OpenAI()
    prompt = (
        "You are an impartial judge. Given a question, an expected "
        "answer, and an actual answer, decide whether the actual answer "
        "captures the expected information. Reply with exactly YES or NO.\n\n"
        f"Question: {question}\nExpected: {expected}\nActual: {actual}\n\n"
        "Decision:"
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.choices[0].message.content or "").strip().upper().startswith("YES")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(
    substrate: Any,
    conversations: list[dict[str, Any]],
    *,
    judge_fn: Any = llm_judge,
) -> dict[str, Any]:
    """Ingest each conversation, then probe with its questions and judge."""
    per_question: list[dict[str, Any]] = []
    for conv in conversations:
        for turn in conv.get("turns", []):
            substrate.ingest(turn)
        for q in conv.get("questions", []):
            t0 = time.monotonic()
            actual = substrate.probe(q.get("question", ""))
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            try:
                ok = judge_fn(
                    q.get("question", ""),
                    q.get("answer", ""),
                    actual,
                )
            except Exception as e:
                ok = False
                actual = f"[judge error: {e}]"
            per_question.append({
                "conversation_id": conv.get("id"),
                "question": q.get("question"),
                "expected": q.get("answer"),
                "actual_preview": (actual or "")[:240],
                "category": q.get("category"),
                "passed": bool(ok),
                "latency_ms": round(elapsed_ms, 2),
            })

    total = len(per_question)
    passed = sum(1 for r in per_question if r["passed"])
    by_category: dict[str, dict[str, int]] = {}
    for r in per_question:
        c = r.get("category") or "uncategorized"
        slot = by_category.setdefault(c, {"total": 0, "passed": 0})
        slot["total"] += 1
        if r["passed"]:
            slot["passed"] += 1
    return {
        "score": round(passed / total, 4) if total else 0.0,
        "passed": passed,
        "total": total,
        "by_category": {
            c: {**v, "score": round(v["passed"] / v["total"], 4)
                              if v["total"] else 0.0}
            for c, v in by_category.items()
        },
        "results": per_question,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bench/locomo.py",
        description="LOCOMO benchmark runner for Mnemosyne (and Mem0 "
                    "via --substrate mem0).",
    )
    p.add_argument("--substrate", choices=("mnemosyne", "mem0"),
                   default="mnemosyne")
    p.add_argument("--provider", default=None,
                   help="LLM provider (mnemosyne llm_grounded mode only)")
    p.add_argument("--model", default=None)
    p.add_argument("--llm-grounded", action="store_true",
                   help="run probes through mnemosyne_models.chat() "
                        "instead of returning raw retrieval context")
    p.add_argument("--max-conversations", type=int, default=None,
                   help="cap dataset size for smoke tests")
    p.add_argument("--out", default="bench/results/locomo.json")
    p.add_argument("--db-path", default="/tmp/locomo-mnemo.db",
                   help="(mnemosyne only) memory.db scratch path")
    args = p.parse_args(argv)

    print(f"[locomo] loading dataset (max_conversations="
          f"{args.max_conversations})...")
    conversations = load_locomo(max_conversations=args.max_conversations)
    print(f"[locomo] loaded {len(conversations)} conversation(s)")

    if args.substrate == "mnemosyne":
        substrate: Any = MnemosyneSubstrate(
            db_path=Path(args.db_path),
            llm_grounded=args.llm_grounded,
            provider=args.provider,
            model=args.model,
        )
    else:
        substrate = Mem0Substrate()

    try:
        report = run(substrate, conversations)
    finally:
        substrate.close()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    summary = {k: v for k, v in report.items() if k != "results"}
    print(json.dumps(summary, indent=2))
    print(f"[locomo] full report: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
