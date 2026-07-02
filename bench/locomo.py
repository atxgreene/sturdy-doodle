"""
bench/locomo.py — LOCOMO benchmark runner (v0.9.8).

Integrates with the canonical dataset at snap-research/locomo:
https://github.com/snap-research/locomo — 10 long-form conversations,
~600 turns each, ~199 human-annotated QA pairs per sample, categories
1-5 (multi-hop, temporal, open-domain, single-hop, adversarial).

This file is **not part of the Mnemosyne distribution.** It lives in
bench/ so the methodology is reproducible, but it depends on the
optional bench/requirements.txt (openai, mem0ai) for the judge +
head-to-head comparator and is not run by the main test suite.

We deliberately **do NOT redistribute the LOCOMO data.** Users fetch
it themselves so there's no license ambiguity. The README explains
the one-liner download.

Tracks reported per run (all in the output JSON):

  * answer-in-context / answer accuracy by category and overall, with
    adversarial (category 5) excluded from the headline score in
    retrieval-only mode (there is no model to abstain) and judged via
    abstention-phrase detection in --llm-grounded mode;
  * evidence recall@k — fraction of gold `evidence` dia_ids present in
    the top-k retrieved rows (categories 1-4; judge-free, deterministic);
  * latency — p50 / p95 / mean for search and (if enabled) LLM calls;
  * tokens — estimated context tokens per probe + totals (chars/4
    heuristic; exact if tiktoken is importable);
  * cost — 0 USD for retrieval-only / local-model runs; token counts
    are recorded so any paid-API cost is computable per provider.

Usage (after dropping locomo10.json at bench/data/locomo10.json):

    # Retrieval-only, full 10 samples / 1,986 questions. No LLM, no
    # API key, ~2-4 minutes. This is the headline substrate number.
    python bench/locomo.py --substrate mnemosyne --verbose \\
        --out bench/results/mnemo-locomo-retrieval.json

    # Same-protocol baselines for the comparison table:
    python bench/locomo.py --substrate mnemosyne --retrieval-mode recency \\
        --out bench/results/baseline-recency.json
    python bench/locomo.py --substrate mnemosyne --retrieval-mode random \\
        --out bench/results/baseline-random.json
    python bench/locomo.py --substrate mnemosyne --retrieval-mode full \\
        --out bench/results/ceiling-full-context.json

    # LLM-grounded via LM Studio (hours on a 7-8B local model)
    python bench/locomo.py --substrate mnemosyne --llm-grounded \\
        --provider lmstudio --model <your-model-id> --verbose \\
        --out bench/results/mnemo-locomo-grounded.json

    # Mem0 head-to-head (same subsample; needs OPENAI_API_KEY)
    python bench/locomo.py --substrate mem0 \\
        --max-samples 1 --max-questions-per-sample 20 \\
        --out bench/results/mem0-locomo-smoke.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import re
import statistics
import subprocess
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


_RUNNER_VERSION = "0.9.8"

# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

_LOCOMO_DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "locomo10.json"
_LOCOMO_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


# Mapping from LOCOMO's integer category codes to category names.
# Verified against the data itself (category 2 questions are "When
# did ..." → temporal; category 4 is the 841-question single-evidence
# bulk → single-hop) and consistent with mem0's published evaluation
# code. NOTE: runner versions < 0.9.8 had 1/2 and 3/4 swapped — per-
# category numbers from those runs are mislabeled (totals unaffected).
_CATEGORY_NAMES = {
    1: "multi_hop",
    2: "temporal",
    3: "open_domain",
    4: "single_hop",
    5: "adversarial",
}


def load_locomo(
    *,
    path: Path | None = None,
    max_samples: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Load the LOCOMO dataset; returns (samples, sha256-of-file).

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
    for smoke-test runs. Adversarial (category 5) rows carry
    `adversarial_answer` instead of `answer` — the correct behavior
    is abstention.
    """
    src = Path(path) if path else _LOCOMO_DEFAULT_PATH
    if not src.exists():
        raise FileNotFoundError(
            f"LOCOMO dataset not found at {src}. "
            f"Download it with:\n\n"
            f"  mkdir -p {src.parent}\n"
            f"  curl -L {_LOCOMO_URL} -o {src}\n"
        )
    raw = src.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"expected list, got {type(data).__name__}")
    if max_samples is not None:
        data = data[:max_samples]
    return data, sha


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
# Token estimation
# ---------------------------------------------------------------------------

def _make_token_counter() -> tuple[Callable[[str], int], str]:
    """Exact tokens via tiktoken when available, else chars/4."""
    try:
        import tiktoken  # type: ignore
        enc = tiktoken.get_encoding("cl100k_base")
        return (lambda s: len(enc.encode(s))), "tiktoken/cl100k_base"
    except Exception:
        return (lambda s: max(1, len(s) // 4)), "chars/4 estimate"


# ---------------------------------------------------------------------------
# Substrate adapters
# ---------------------------------------------------------------------------

class MnemosyneSubstrate:
    """LOCOMO adapter for Mnemosyne.

    Modes:
      - retrieval-only (default): write each turn to L2; on probe, run
        search() and concat top-k. No LLM. Measures the substrate.
      - llm-grounded: same writes, but on probe pass retrieved context
        as a system prompt and ask the configured backend to answer.
        Measures the full agent stack.

    `retrieval_mode` selects what fills the context window on probe —
    used to produce same-protocol baselines:
      - fts:     MemoryStore.search() top-k (the real substrate path)
      - recency: last k turns of the conversation (no search at all)
      - random:  k uniformly random turns (seeded)
      - full:    the entire conversation (answer-in-context ceiling)
    """

    def __init__(self, *, db_path: Path, llm_grounded: bool = False,
                 provider: str | None = None,
                 model: str | None = None,
                 retrieval_mode: str = "fts",
                 top_k: int = 8,
                 seed: int = 0) -> None:
        self.memory = MemoryStore(path=db_path)
        self.llm_grounded = llm_grounded
        self.provider = provider
        self.model = model
        self.retrieval_mode = retrieval_mode
        self.top_k = top_k
        self._rng = random.Random(seed)
        self._turns: list[dict[str, Any]] = []  # retained for baselines
        self._backend = None
        self.last_probe: dict[str, Any] = {}

    def ingest_sample(self, sample: dict[str, Any]) -> int:
        """Write every turn of a LOCOMO sample into the memory store.

        Returns the number of turns ingested.
        """
        count = 0
        self._turns = []
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
            self._turns.append({"content": content,
                                "dia_id": turn.get("dia_id")})
            count += 1
        return count

    def _retrieve(self, question: str) -> tuple[str, list[str]]:
        """Return (context, retrieved dia_ids) per retrieval_mode."""
        k = self.top_k
        if self.retrieval_mode == "fts":
            hits = self.memory.search(question, limit=k)
            dia_ids = []
            for h in hits[:k]:
                try:
                    meta = json.loads(h.get("metadata_json") or "{}")
                except Exception:
                    meta = {}
                if meta.get("dia_id"):
                    dia_ids.append(meta["dia_id"])
            return "\n".join(h["content"] for h in hits[:k]), dia_ids
        if self.retrieval_mode == "recency":
            rows = self._turns[-k:]
        elif self.retrieval_mode == "random":
            rows = (self._rng.sample(self._turns, k)
                    if len(self._turns) > k else list(self._turns))
        elif self.retrieval_mode == "full":
            rows = self._turns
        else:
            raise ValueError(f"unknown retrieval_mode {self.retrieval_mode!r}")
        return ("\n".join(r["content"] for r in rows),
                [r["dia_id"] for r in rows if r.get("dia_id")])

    def probe(self, question: str) -> str:
        t0 = time.monotonic()
        context, dia_ids = self._retrieve(question)
        search_ms = (time.monotonic() - t0) * 1000.0
        self.last_probe = {
            "search_ms": round(search_ms, 2),
            "retrieved_dia_ids": dia_ids,
            "context_chars": len(context),
            "llm_ms": None,
            "llm_prompt_chars": None,
            "llm_response_chars": None,
        }
        if not self.llm_grounded:
            return context
        import mnemosyne_models as mm_models  # type: ignore
        if not self.provider or not self.model:
            raise RuntimeError(
                "--llm-grounded requires --provider and --model")
        if self._backend is None:
            self._backend = mm_models.Backend(provider=self.provider,
                                              default_model=self.model)
        system = (
            "You are answering a question about a long conversation "
            "using only the retrieved excerpts below. Be concise "
            "and direct. If the answer isn't in the excerpts, say "
            "'I don't know.'\n\n"
            "## Retrieved excerpts\n\n" + (context or "(none)"))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ]
        t1 = time.monotonic()
        resp = mm_models.chat(messages, backend=self._backend)
        text = (resp.get("text") or "").strip()
        self.last_probe.update(
            llm_ms=round((time.monotonic() - t1) * 1000.0, 2),
            llm_prompt_chars=len(system) + len(question),
            llm_response_chars=len(text),
        )
        return text

    def reset(self) -> None:
        """Clear memory between samples so conversations don't leak."""
        self._turns = []
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
        self.last_probe: dict[str, Any] = {}

    def ingest_sample(self, sample: dict[str, Any]) -> int:
        count = 0
        for turn in iter_conversation_turns(sample):
            text = f"{turn['speaker']}: {turn['text']}"
            self.client.add(text, user_id=self.user_id)
            count += 1
        return count

    def probe(self, question: str) -> str:
        t0 = time.monotonic()
        hits = self.client.search(question, user_id=self.user_id, limit=8)
        if isinstance(hits, dict):
            hits = hits.get("results", [])
        context = "\n".join(
            (h.get("memory") or h.get("text") or "") for h in hits[:8]
        )
        self.last_probe = {
            "search_ms": round((time.monotonic() - t0) * 1000.0, 2),
            "retrieved_dia_ids": [],
            "context_chars": len(context),
            "llm_ms": None,
            "llm_prompt_chars": None,
            "llm_response_chars": None,
        }
        return context

    def reset(self) -> None:
        try:
            # Mem0 exposes `reset()` on the Memory client
            self.client.reset()
        except Exception:
            pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Judges
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


_ABSTAIN_RE = re.compile(
    r"(don'?t know|do not know|no (?:such )?(?:information|mention)|"
    r"not (?:mentioned|specified|stated|discussed|provided|in the)|"
    r"never (?:mentioned|discussed)|cannot (?:find|determine|answer)|"
    r"unable to (?:find|determine|answer)|no answer)", re.IGNORECASE)


def _abstention_judge(actual: str) -> bool:
    """Adversarial (category 5) questions are unanswerable: the correct
    behavior is abstention. Pass iff the response declines to answer."""
    return bool(_ABSTAIN_RE.search(actual or ""))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _pctl(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(statistics.quantiles(values, n=100)[min(98, int(q) - 1)]) \
        if len(values) > 1 else float(values[0])


def _latency_block(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean_ms": round(statistics.fmean(values), 2),
        "p50_ms": round(statistics.median(values), 2),
        "p95_ms": round(_pctl(sorted(values), 95), 2),
        "max_ms": round(max(values), 2),
    }


def run(
    substrate: Any,
    samples: list[dict[str, Any]],
    *,
    max_questions_per_sample: int | None = None,
    judge: str = "substring",
    judge_model: str = "gpt-4o-mini",
    llm_grounded: bool = False,
    on_progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Ingest each sample, then probe with its QA and judge responses.

    Scoring protocol:
      * categories 1-4: judge(expected answer, response). Headline
        `score` is computed over these 1,540 questions — matching how
        published LOCOMO evals (e.g. mem0's) report theirs.
      * category 5 (adversarial, 446 questions): scored only when an
        LLM produces the answer (`--llm-grounded`), via abstention
        detection; reported as a separate `adversarial` block, plus
        `score_with_adversarial` for the combined number.
      * evidence recall@k: judge-free; gold `evidence` dia_ids found
        in the retrieved rows (categories 1-4, fts mode rows only).
    """
    token_count, token_method = _make_token_counter()
    per_question: list[dict[str, Any]] = []
    ingest_stats = {"samples": 0, "turns_total": 0, "seconds": 0.0,
                    "ingested_tokens_est": 0}
    wall_t0 = time.monotonic()

    for s_idx, sample in enumerate(samples, start=1):
        substrate.reset()
        ing_t0 = time.monotonic()
        n_turns = substrate.ingest_sample(sample)
        ingest_stats["seconds"] += time.monotonic() - ing_t0
        ingest_stats["samples"] += 1
        ingest_stats["turns_total"] += n_turns
        ingest_stats["ingested_tokens_est"] += sum(
            token_count(t["text"]) for t in iter_conversation_turns(sample))

        qa_list = sample.get("qa", []) or []
        if max_questions_per_sample is not None:
            qa_list = qa_list[:max_questions_per_sample]
        for q_idx, qa in enumerate(qa_list, start=1):
            question = qa.get("question", "")
            category_code = qa.get("category")
            category = _CATEGORY_NAMES.get(category_code,
                                           f"cat_{category_code}")
            adversarial = category_code == 5
            expected = ("(abstain)" if adversarial
                        else str(qa.get("answer", "")))
            t0 = time.monotonic()
            try:
                actual = substrate.probe(question)
            except Exception as e:
                actual = f"[probe error: {type(e).__name__}: {e}]"
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            probe_info = getattr(substrate, "last_probe", {}) or {}

            passed: bool | None
            if adversarial:
                # Only meaningful when an LLM produced `actual`.
                passed = _abstention_judge(actual) if llm_grounded else None
            elif judge == "openai":
                try:
                    passed = _openai_judge(question, expected, actual,
                                           model=judge_model)
                except Exception as e:
                    passed = False
                    actual += f" [judge error: {e}]"
            else:
                passed = _substring_judge(question, expected, actual)

            # Evidence recall (judge-free; only when the substrate
            # reports which dia_ids it retrieved).
            evidence = [e for e in (qa.get("evidence") or []) if e]
            retrieved = probe_info.get("retrieved_dia_ids") or []
            ev_recall = None
            if evidence and retrieved and not adversarial:
                found = sum(1 for e in evidence if e in retrieved)
                ev_recall = round(found / len(evidence), 4)

            result = {
                "sample_id": sample.get("sample_id"),
                "question_idx": q_idx,
                "question": question,
                "expected": expected,
                "actual_preview": (actual or "")[:240],
                "category": category,
                "passed": passed,
                "latency_ms": round(elapsed_ms, 2),
                "search_ms": probe_info.get("search_ms"),
                "llm_ms": probe_info.get("llm_ms"),
                "context_tokens_est": (probe_info.get("context_chars") or 0) // 4,
                "evidence_recall": ev_recall,
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

    wall_s = time.monotonic() - wall_t0

    scored = [r for r in per_question if r["category"] != "adversarial"]
    adversarial_rows = [r for r in per_question
                        if r["category"] == "adversarial"]
    total = len(scored)
    passed = sum(1 for r in scored if r["passed"])
    by_category: dict[str, dict[str, int]] = {}
    for r in scored:
        c = r.get("category") or "uncategorized"
        slot = by_category.setdefault(c, {"total": 0, "passed": 0})
        slot["total"] += 1
        if r["passed"]:
            slot["passed"] += 1

    adv_scored = [r for r in adversarial_rows if r["passed"] is not None]
    adv_passed = sum(1 for r in adv_scored if r["passed"])
    all_scored = total + len(adv_scored)
    all_passed = passed + adv_passed

    ev_values = [r["evidence_recall"] for r in scored
                 if r["evidence_recall"] is not None]
    ev_by_cat: dict[str, list[float]] = {}
    for r in scored:
        if r["evidence_recall"] is not None:
            ev_by_cat.setdefault(r["category"], []).append(
                r["evidence_recall"])

    search_lat = [r["search_ms"] for r in per_question
                  if r.get("search_ms") is not None]
    llm_lat = [r["llm_ms"] for r in per_question
               if r.get("llm_ms") is not None]
    ctx_tokens = [r["context_tokens_est"] for r in per_question]

    ing = ingest_stats
    return {
        "score": round(passed / total, 4) if total else 0.0,
        "passed": passed,
        "total": total,
        "adversarial": {
            "total": len(adversarial_rows),
            "scored": len(adv_scored),
            "passed": adv_passed,
            "score": (round(adv_passed / len(adv_scored), 4)
                      if adv_scored else None),
            "note": ("abstention-judged (llm-grounded)" if llm_grounded
                     else "not scored: retrieval-only mode has no model "
                          "to abstain; excluded from headline score"),
        },
        "score_with_adversarial": (round(all_passed / all_scored, 4)
                                   if all_scored else 0.0),
        "samples_run": ing["samples"],
        "by_category": {
            c: {**v, "score": round(v["passed"] / v["total"], 4)
                if v["total"] else 0.0}
            for c, v in sorted(by_category.items())
        },
        "evidence_recall": {
            "mean": round(statistics.fmean(ev_values), 4) if ev_values else None,
            "all_found_rate": (round(sum(1 for v in ev_values if v == 1.0)
                                     / len(ev_values), 4)
                               if ev_values else None),
            "n": len(ev_values),
            "by_category": {c: round(statistics.fmean(v), 4)
                            for c, v in sorted(ev_by_cat.items())},
        },
        "latency": {
            "search": _latency_block(search_lat),
            "llm": _latency_block(llm_lat),
            "wall_clock_s": round(wall_s, 1),
        },
        "tokens": {
            "method": {"ingested": token_method, "context": "chars/4"},
            "ingested_total_est": ing["ingested_tokens_est"],
            "context_per_probe_mean_est": (round(statistics.fmean(ctx_tokens))
                                           if ctx_tokens else 0),
            "context_per_probe_p95_est": (round(_pctl(sorted(ctx_tokens), 95))
                                          if ctx_tokens else 0),
            "context_total_est": sum(ctx_tokens),
        },
        "ingest": {
            "samples": ing["samples"],
            "turns_total": ing["turns_total"],
            "seconds": round(ing["seconds"], 2),
            "writes_per_sec": (round(ing["turns_total"] / ing["seconds"])
                               if ing["seconds"] else None),
        },
        "cost_usd": 0.0 if judge != "openai" and not llm_grounded else None,
        "judge": judge,
        "results": per_question,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=_REPO,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


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
    p.add_argument("--retrieval-mode",
                   choices=("fts", "recency", "random", "full"),
                   default="fts",
                   help="what fills the probe context (mnemosyne only): "
                        "fts = MemoryStore.search() top-k (default); "
                        "recency = last k turns; random = k seeded random "
                        "turns; full = whole conversation (ceiling)")
    p.add_argument("--top-k", type=int, default=8,
                   help="retrieved rows per probe (default 8)")
    p.add_argument("--seed", type=int, default=0,
                   help="seed for --retrieval-mode random")
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
    samples, dataset_sha = load_locomo(
        path=(Path(args.dataset) if args.dataset else None),
        max_samples=args.max_samples)
    print(f"[locomo] loaded {len(samples)} sample(s) "
          f"sha256={dataset_sha[:16]}…", flush=True)

    if args.substrate == "mnemosyne":
        db = Path(args.db_path)
        if db.exists():
            db.unlink()
        substrate: Any = MnemosyneSubstrate(
            db_path=db,
            llm_grounded=args.llm_grounded,
            provider=args.provider,
            model=args.model,
            retrieval_mode=args.retrieval_mode,
            top_k=args.top_k,
            seed=args.seed,
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
            if r["passed"] is None:
                mark = "\033[2m·\033[0m"
            elif r["passed"]:
                mark = "\033[1;32m✓\033[0m"
            else:
                mark = "\033[1;31m✗\033[0m"
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
            llm_grounded=args.llm_grounded,
            on_progress=progress,
        )
    finally:
        substrate.close()

    report["_metadata"] = {
        "benchmark": "LOCOMO (snap-research/locomo, locomo10.json)",
        "runner": f"bench/locomo.py v{_RUNNER_VERSION}",
        "substrate": args.substrate,
        "retrieval_mode": (args.retrieval_mode
                           if args.substrate == "mnemosyne" else "mem0"),
        "top_k": args.top_k,
        "llm_grounded": args.llm_grounded,
        "provider": args.provider,
        "model": args.model,
        "judge": args.judge,
        "judge_model": args.judge_model if args.judge == "openai" else None,
        "dataset_sha256": dataset_sha,
        "git_commit": _git_commit(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "date_utc": time.strftime("%Y-%m-%d %H:%M:%S",  time.gmtime()),
        "argv": (argv if argv is not None else sys.argv[1:]),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    summary = {k: v for k, v in report.items() if k != "results"}
    print(json.dumps(summary, indent=2))
    print(f"[locomo] full report: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
