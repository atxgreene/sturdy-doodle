"""
bench/locomo.py — LOCOMO benchmark runner (v0.9.4).

Integrates with the canonical dataset at snap-research/locomo:
https://github.com/snap-research/locomo — 10 long-form conversations,
~600 turns each, 199 human-annotated QA pairs per sample, categories
1-5 (single-hop, multi-hop, temporal, open-domain, adversarial).

This file is **not part of the Mnemosyne distribution.** It lives in
bench/ so the methodology is reproducible, but it depends on the
optional bench/requirements.txt (openai, mem0ai) for the judge +
head-to-head comparator and is not run by the main test suite.

We deliberately **do NOT redistribute the LOCOMO data.** Users fetch
it themselves so there's no license ambiguity. The README explains
the one-liner download.

Usage (after dropping locomo10.json at bench/data/locomo10.json):

    # Mnemosyne, LM Studio, 1 sample / 20 questions — sanity check
    python bench/locomo.py \\
        --substrate mnemosyne --llm-grounded \\
        --provider lmstudio --model <your-model-id> \\
        --max-samples 1 --max-questions-per-sample 20 --verbose \\
        --out bench/results/mnemo-locomo-smoke.json

    # Full run (all 10 samples, all 1990 questions) — hours on a 7-8B model
    python bench/locomo.py \\
        --substrate mnemosyne --llm-grounded \\
        --provider lmstudio --model <your-model-id> \\
        --verbose \\
        --out bench/results/mnemo-locomo-full.json

    # Mem0 head-to-head (same subsample)
    python bench/locomo.py \\
        --substrate mem0 \\
        --max-samples 1 --max-questions-per-sample 20 \\
        --out bench/results/mem0-locomo-smoke.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

# Ensure the repo root is importable when bench/locomo.py runs as a
# script from the repo. (No package install assumed.)
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Mnemosyne substrate (always available in this repo)
from mnemosyne_memory import MemoryStore  # noqa: E402


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

_LOCOMO_DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "locomo10.json"
_LOCOMO_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


# Mapping from LOCOMO's integer category codes to human-readable
# category names, per the snap-research/locomo paper.
_CATEGORY_NAMES = {
    1: "single_hop",
    2: "multi_hop",
    3: "temporal",
    4: "open_domain",
    5: "adversarial",
}


def load_locomo(
    *,
    path: Path | None = None,
    max_samples: int | None = None,
) -> list[dict[str, Any]]:
    """Load the LOCOMO dataset as a list of sample dicts.

    Each dict follows the canonical snap-research/locomo schema:

        {
          "sample_id": str,
          "conversation": {
              "speaker_a": str, "speaker_b": str,
              "session_1": [ {speaker, dia_id, text}, ... ],
              "session_1_date_time": "...",
              "session_2": [...], ...
          },
          "qa": [
              {"question": str, "answer": str,
               "evidence": [dia_id, ...], "category": 1..5}, ...
          ],
          "observation": {...}, "session_summary": {...},
          "event_summary": {...},
        }

    The dataset ships 10 samples total. `max_samples` caps the list
    for smoke-test runs.
    """
    src = Path(path) if path else _LOCOMO_DEFAULT_PATH
    if not src.exists():
        raise FileNotFoundError(
            f"LOCOMO dataset not found at {src}. "
            f"Download it with:\n\n"
            f"  mkdir -p {src.parent}\n"
            f"  curl -L {_LOCOMO_URL} -o {src}\n"
        )
    with src.open() as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"expected list, got {type(data).__name__}")
    if max_samples is not None:
        data = data[:max_samples]
    return data


def iter_conversation_turns(sample: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield turns from a LOCOMO sample in chronological session order.

    Sessions are keyed `session_<N>`; we sort by the integer N so
    session_2 comes before session_10. Skips the `_date_time` sibling
    keys.
    """
    conv = sample.get("conversation", {})
    session_keys = sorted(
        (k for k in conv
         if k.startswith("session_") and not k.endswith("_date_time")),
        key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0,
    )
    for sk in session_keys:
        turns = conv.get(sk, [])
        if not isinstance(turns, list):
            continue
        session_ts = conv.get(f"{sk}_date_time")
        for turn in turns:
            yield {
                "session": sk,
                "session_date_time": session_ts,
                "speaker": turn.get("speaker", "?"),
                "dia_id": turn.get("dia_id"),
                "text": turn.get("text", ""),
            }


# ---------------------------------------------------------------------------
# Substrate adapters
# ---------------------------------------------------------------------------

class MnemosyneSubstrate:
    """LOCOMO adapter for Mnemosyne.

    Two modes:
      - retrieval-only: write each turn to L2; on probe, run search()
        and concat top-k. No LLM. Measures the substrate in isolation.
      - llm-grounded: same writes, but on probe pass retrieved context
        as a system prompt and ask the configured backend to answer.
        Measures the full agent stack.
    """

    def __init__(self, *, db_path: Path, llm_grounded: bool = False,
                 provider: str | None = None,
                 model: str | None = None) -> None:
        self.memory = MemoryStore(path=db_path)
        self.llm_grounded = llm_grounded
        self.provider = provider
        self.model = model

    def ingest_sample(self, sample: dict[str, Any]) -> int:
        """Write every turn of a LOCOMO sample into the memory store.

        Returns the number of turns ingested.
        """
        count = 0
        for turn in iter_conversation_turns(sample):
            # Encode "[session dia_id] Speaker: text" so retrieval can
            # key off either the speaker or a dialog-id mention.
            content = (
                f"[{turn['session']} {turn.get('dia_id') or ''}] "
                f"{turn['speaker']}: {turn['text']}"
            )
            self.memory.write(
                content=content,
                source="locomo",
                kind="event",
                tier=2,
                metadata={
                    "dia_id": turn.get("dia_id"),
                    "session": turn.get("session"),
                    "session_date_time": turn.get("session_date_time"),
                },
            )
            count += 1
        return count

    def probe(self, question: str) -> str:
        hits = self.memory.search(question, limit=8)
        context = "\n".join(h["content"] for h in hits[:8])
        if not self.llm_grounded:
            return context
        import mnemosyne_models as mm_models  # type: ignore
        if not self.provider or not self.model:
            raise RuntimeError(
                "--llm-grounded requires --provider and --model")
        backend = mm_models.Backend(provider=self.provider,
                                     default_model=self.model)
        messages = [
            {"role": "system",
             "content": (
                 "You are answering a question about a long conversation "
                 "using only the retrieved excerpts below. Be concise "
                 "and direct. If the answer isn't in the excerpts, say "
                 "'I don't know.'\n\n"
                 "## Retrieved excerpts\n\n" + (context or "(none)"))},
            {"role": "user", "content": question},
        ]
        resp = mm_models.chat(messages, backend=backend)
        return (resp.get("text") or "").strip()

    def reset(self) -> None:
        """Clear memory between samples so conversations don't leak."""
        with self.memory._lock:  # noqa: SLF001
            self.memory._conn.execute("DELETE FROM memories")  # noqa: SLF001

    def close(self) -> None:
        try:
            self.memory.close()
        except Exception:
            pass


class Mem0Substrate:
    """Mem0 LOCOMO adapter — requires `pip install -r bench/requirements.txt`."""

    def __init__(self) -> None:
        try:
            from mem0 import Memory  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "mem0ai not installed. Run "
                "`pip install -r bench/requirements.txt` first.") from e
        self.client = Memory()
        self.user_id = "locomo-bench"

    def ingest_sample(self, sample: dict[str, Any]) -> int:
        count = 0
        for turn in iter_conversation_turns(sample):
            text = f"{turn['speaker']}: {turn['text']}"
            self.client.add(text, user_id=self.user_id)
            count += 1
        return count

    def probe(self, question: str) -> str:
        hits = self.client.search(question, user_id=self.user_id, limit=8)
        if isinstance(hits, dict):
            hits = hits.get("results", [])
        return "\n".join(
            (h.get("memory") or h.get("text") or "") for h in hits[:8]
        )

    def reset(self) -> None:
        try:
            # Mem0 exposes `reset()` on the Memory client
            self.client.reset()
        except Exception:
            pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# LLM-as-judge
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = (
    "You are an impartial judge. Given a question, the expected answer, "
    "and a candidate answer, decide whether the candidate captures the "
    "key information from the expected answer. Minor wording differences "
    "are fine. Respond with exactly YES or NO on the first line."
)


def _openai_judge(question: str, expected: str, actual: str,
                   *, model: str = "gpt-4o-mini") -> bool:
    from openai import OpenAI  # type: ignore
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model, temperature=0,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user",
             "content": (
                 f"Question: {question}\nExpected: {expected}\n"
                 f"Candidate: {actual}")},
        ],
    )
    return (resp.choices[0].message.content or "").strip().upper().startswith("YES")


def _substring_judge(question: str, expected: str, actual: str) -> bool:
    """Fallback judge: case-insensitive substring / any-token overlap.

    Used when no LLM judge is available. Pass if the expected answer
    (or any of its >=4-char tokens) appears in the actual response.
    Permissive on purpose — this is a lower bound, not the final score.
    """
    if not actual:
        return False
    lo_actual = actual.lower()
    lo_expected = expected.strip().lower()
    if lo_expected and lo_expected in lo_actual:
        return True
    tokens = [t for t in re.findall(r"[a-zA-Z0-9]{4,}", lo_expected)]
    return any(t in lo_actual for t in tokens)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(
    substrate: Any,
    samples: list[dict[str, Any]],
    *,
    max_questions_per_sample: int | None = None,
    judge: str = "substring",
    judge_model: str = "gpt-4o-mini",
    on_progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Ingest each sample, then probe with its QA and judge responses."""
    per_question: list[dict[str, Any]] = []
    for s_idx, sample in enumerate(samples, start=1):
        substrate.reset()
        n_turns = substrate.ingest_sample(sample)
        qa_list = sample.get("qa", []) or []
        if max_questions_per_sample is not None:
            qa_list = qa_list[:max_questions_per_sample]
        for q_idx, qa in enumerate(qa_list, start=1):
            question = qa.get("question", "")
            expected = str(qa.get("answer", ""))
            category = _CATEGORY_NAMES.get(qa.get("category"),
                                            f"cat_{qa.get('category')}")
            t0 = time.monotonic()
            try:
                actual = substrate.probe(question)
            except Exception as e:
                actual = f"[probe error: {type(e).__name__}: {e}]"
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            if judge == "openai":
                try:
                    passed = _openai_judge(question, expected, actual,
                                            model=judge_model)
                except Exception as e:
                    passed = False
                    actual += f" [judge error: {e}]"
            else:
                passed = _substring_judge(question, expected, actual)
            result = {
                "sample_id": sample.get("sample_id"),
                "question_idx": q_idx,
                "question": question,
                "expected": expected,
                "actual_preview": (actual or "")[:240],
                "category": category,
                "passed": bool(passed),
                "latency_ms": round(elapsed_ms, 2),
            }
            per_question.append(result)
            if on_progress is not None:
                try:
                    on_progress(s_idx, len(samples), result)
                except Exception:
                    pass
        # Per-sample summary for verbose mode
        if on_progress is not None:
            try:
                on_progress(s_idx, len(samples), {
                    "_sample_done": True,
                    "sample_id": sample.get("sample_id"),
                    "turns_ingested": n_turns,
                    "questions_scored": len(qa_list),
                })
            except Exception:
                pass

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
        "samples_run": len(samples),
        "by_category": {
            c: {**v, "score": round(v["passed"] / v["total"], 4)
                              if v["total"] else 0.0}
            for c, v in by_category.items()
        },
        "judge": judge,
        "results": per_question,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bench/locomo.py",
        description="LOCOMO benchmark runner for Mnemosyne "
                    "(and Mem0 via --substrate mem0).",
    )
    p.add_argument("--substrate", choices=("mnemosyne", "mem0"),
                   default="mnemosyne")
    p.add_argument("--provider", default=None,
                   help="LLM provider (Mnemosyne --llm-grounded mode only)")
    p.add_argument("--model", default=None)
    p.add_argument("--llm-grounded", action="store_true",
                   help="answer questions via mnemosyne_models.chat() "
                        "using retrieved context as system prompt")
    p.add_argument("--dataset", default=None,
                   help=f"path to locomo10.json (default: {_LOCOMO_DEFAULT_PATH})")
    p.add_argument("--max-samples", type=int, default=None,
                   help="cap number of LOCOMO samples; the canonical "
                        "release has 10")
    p.add_argument("--max-questions-per-sample", type=int, default=None,
                   help="cap QA count per sample (smoke tests); "
                        "samples have ~199 QA each")
    p.add_argument("--judge", choices=("substring", "openai"),
                   default="substring",
                   help="answer-scoring method. `substring` = fast "
                        "substring/token match (lower bound). `openai` "
                        "= LLM-as-judge via OPENAI_API_KEY (paid; more "
                        "representative of LOCOMO-style grading).")
    p.add_argument("--judge-model", default="gpt-4o-mini")
    p.add_argument("--out", default="bench/results/locomo.json")
    p.add_argument("--db-path", default="/tmp/locomo-mnemo.db",
                   help="(mnemosyne only) scratch memory.db path")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    print(f"[locomo] loading dataset "
          f"(max_samples={args.max_samples})...", flush=True)
    samples = load_locomo(path=(Path(args.dataset) if args.dataset else None),
                           max_samples=args.max_samples)
    print(f"[locomo] loaded {len(samples)} sample(s)", flush=True)

    if args.substrate == "mnemosyne":
        substrate: Any = MnemosyneSubstrate(
            db_path=Path(args.db_path),
            llm_grounded=args.llm_grounded,
            provider=args.provider,
            model=args.model,
        )
    else:
        substrate = Mem0Substrate()

    def _mk_progress() -> Callable[[int, int, dict[str, Any]], None] | None:
        if not args.verbose:
            return None
        def progress(s_idx: int, s_total: int, r: dict[str, Any]) -> None:
            if r.get("_sample_done"):
                print(
                    f"[sample {s_idx}/{s_total}] done · "
                    f"{r['turns_ingested']} turns · "
                    f"{r['questions_scored']} questions",
                    flush=True,
                )
                return
            mark = "\033[1;32m✓\033[0m" if r["passed"] else "\033[1;31m✗\033[0m"
            print(
                f"  {mark} [{r['category']:12s}] "
                f"q{r['question_idx']:3d}: {r['question'][:80]}",
                flush=True,
            )
        return progress

    progress = _mk_progress()

    try:
        report = run(
            substrate, samples,
            max_questions_per_sample=args.max_questions_per_sample,
            judge=args.judge,
            judge_model=args.judge_model,
            on_progress=progress,
        )
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
