"""
bench/longmemeval.py — LongMemEval benchmark runner (v0.9.8).

LongMemEval (Wu et al., ICLR 2025 — arXiv 2410.10813,
https://github.com/xiaowu0162/LongMemEval) tests five long-term memory
abilities over timestamped multi-session chat histories: information
extraction, multi-session reasoning, knowledge updates, temporal
reasoning, and abstention. 500 questions; three history sizes:

  * longmemeval_oracle.json  — evidence sessions only (small)
  * longmemeval_s_cleaned.json — ~40 sessions / ~115k tokens per question
  * longmemeval_m_cleaned.json — ~500 sessions per question

This file is **not part of the Mnemosyne distribution** (see
bench/README.md). We do not redistribute the dataset; fetch it from
the official HuggingFace release:

    mkdir -p bench/data
    wget -P bench/data https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json
    wget -P bench/data https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json

Tracks reported per run (all in the output JSON):

  * session-level retrieval recall@k — fraction of gold
    `answer_session_ids` whose sessions appear in the top-k retrieved
    rows (LongMemEval's standard memory-recall metric; judge-free);
  * turn-level retrieval recall@k — fraction of `has_answer` turns
    retrieved (judge-free);
  * answer-in-context / answer accuracy by question_type (substring
    judge lower bound, or OpenAI LLM-as-judge), with abstention
    (`*_abs`) questions scored only in --llm-grounded mode;
  * latency p50/p95, token estimates, ingest throughput, cost.

Usage:

    # Validate the runner end-to-end on a synthetic fixture (no
    # dataset, no network, no LLM — runs in CI-able time):
    python bench/longmemeval.py --selftest

    # Retrieval-only over longmemeval_s (no LLM, no API key):
    python bench/longmemeval.py --dataset bench/data/longmemeval_s_cleaned.json \\
        --verbose --out bench/results/longmemeval-s-retrieval.json

    # Same-protocol baselines:
    python bench/longmemeval.py --dataset bench/data/longmemeval_s_cleaned.json \\
        --retrieval-mode recency --out bench/results/lme-recency.json

    # LLM-grounded via LM Studio / Ollama / any of the 19 providers:
    python bench/longmemeval.py --dataset bench/data/longmemeval_s_cleaned.json \\
        --llm-grounded --provider lmstudio --model <id> --verbose \\
        --out bench/results/longmemeval-s-grounded.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

# Repo root importable as script
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from mnemosyne_memory import MemoryStore  # noqa: E402

# Shared helpers from the LOCOMO runner (same directory)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from locomo import (  # noqa: E402
    _RUNNER_VERSION,
    _abstention_judge,
    _git_commit,
    _latency_block,
    _make_token_counter,
    _openai_judge,
    _pctl,
    _substring_judge,
)

_DATA_DIR = Path(__file__).resolve().parent / "data"
_HF_BASE = ("https://huggingface.co/datasets/xiaowu0162/"
            "longmemeval-cleaned/resolve/main")


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def load_longmemeval(path: Path, *, max_questions: int | None = None,
                     ) -> tuple[list[dict[str, Any]], str]:
    """Load a LongMemEval JSON file; returns (instances, sha256).

    Instance schema (per the official repo):
      question_id, question_type, question, answer, question_date,
      haystack_session_ids, haystack_dates,
      haystack_sessions: [[{role, content, has_answer?}, ...], ...],
      answer_session_ids
    """
    if not path.exists():
        raise FileNotFoundError(
            f"LongMemEval dataset not found at {path}. Download it:\n\n"
            f"  mkdir -p {path.parent}\n"
            f"  wget -P {path.parent} {_HF_BASE}/{path.name}\n")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"expected list, got {type(data).__name__}")
    if max_questions is not None:
        data = data[:max_questions]
    return data, sha


# ---------------------------------------------------------------------------
# Substrate
# ---------------------------------------------------------------------------

class MnemosyneLMESubstrate:
    """LongMemEval adapter for Mnemosyne's MemoryStore.

    One fresh haystack per question: reset, ingest that question's
    sessions turn-by-turn, then probe. Retrieval modes mirror
    bench/locomo.py (fts / recency / random / full).
    """

    def __init__(self, *, db_path: Path, llm_grounded: bool = False,
                 provider: str | None = None, model: str | None = None,
                 retrieval_mode: str = "fts", top_k: int = 8,
                 seed: int = 0) -> None:
        self.memory = MemoryStore(path=db_path)
        self.llm_grounded = llm_grounded
        self.provider = provider
        self.model = model
        self.retrieval_mode = retrieval_mode
        self.top_k = top_k
        self._rng = random.Random(seed)
        self._turns: list[dict[str, Any]] = []
        self._backend = None
        self.last_probe: dict[str, Any] = {}

    def ingest_instance(self, inst: dict[str, Any]) -> int:
        self.reset()
        sessions = inst.get("haystack_sessions") or []
        ids = inst.get("haystack_session_ids") or []
        dates = inst.get("haystack_dates") or []
        count = 0
        for s_idx, session in enumerate(sessions):
            sid = ids[s_idx] if s_idx < len(ids) else f"session_{s_idx}"
            sdate = dates[s_idx] if s_idx < len(dates) else None
            if not isinstance(session, list):
                continue
            for t_idx, turn in enumerate(session):
                content = (f"[{sid} {sdate or ''}] "
                           f"{turn.get('role', '?')}: "
                           f"{turn.get('content', '')}")
                self.memory.write(
                    content=content, source="longmemeval", kind="event",
                    tier=2,
                    metadata={"session_id": sid, "turn_idx": t_idx,
                              "has_answer": bool(turn.get("has_answer"))},
                )
                self._turns.append({"content": content, "session_id": sid,
                                    "turn_idx": t_idx,
                                    "has_answer": bool(turn.get("has_answer"))})
                count += 1
        return count

    def _retrieve(self, question: str) -> tuple[str, list[dict[str, Any]]]:
        k = self.top_k
        if self.retrieval_mode == "fts":
            hits = self.memory.search(question, limit=k)
            rows = []
            for h in hits[:k]:
                try:
                    meta = json.loads(h.get("metadata_json") or "{}")
                except Exception:
                    meta = {}
                rows.append({"content": h["content"],
                             "session_id": meta.get("session_id"),
                             "turn_idx": meta.get("turn_idx"),
                             "has_answer": meta.get("has_answer")})
            return "\n".join(r["content"] for r in rows), rows
        if self.retrieval_mode == "recency":
            rows = self._turns[-k:]
        elif self.retrieval_mode == "random":
            rows = (self._rng.sample(self._turns, k)
                    if len(self._turns) > k else list(self._turns))
        elif self.retrieval_mode == "full":
            rows = self._turns
        else:
            raise ValueError(f"unknown retrieval_mode {self.retrieval_mode!r}")
        return "\n".join(r["content"] for r in rows), rows

    def probe(self, question: str, question_date: str | None = None) -> str:
        query = question
        t0 = time.monotonic()
        context, rows = self._retrieve(query)
        search_ms = (time.monotonic() - t0) * 1000.0
        self.last_probe = {
            "search_ms": round(search_ms, 2),
            "retrieved_rows": rows,
            "context_chars": len(context),
            "llm_ms": None,
        }
        if not self.llm_grounded:
            return context
        import mnemosyne_models as mm_models  # type: ignore
        if not self.provider or not self.model:
            raise RuntimeError("--llm-grounded requires --provider and --model")
        if self._backend is None:
            self._backend = mm_models.Backend(provider=self.provider,
                                              default_model=self.model)
        system = (
            "You are answering a question about the user's past chat "
            "sessions using only the retrieved excerpts below. Each "
            "excerpt is prefixed with its session id and timestamp. "
            f"Today's date is {question_date or 'unknown'}. Be concise. "
            "If the answer isn't in the excerpts, say 'I don't know.'\n\n"
            "## Retrieved excerpts\n\n" + (context or "(none)"))
        t1 = time.monotonic()
        resp = mm_models.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": question}],
            backend=self._backend)
        text = (resp.get("text") or "").strip()
        self.last_probe["llm_ms"] = round((time.monotonic() - t1) * 1000.0, 2)
        return text

    def reset(self) -> None:
        self._turns = []
        with self.memory._lock:  # noqa: SLF001
            self.memory._conn.execute("DELETE FROM memories")  # noqa: SLF001

    def close(self) -> None:
        try:
            self.memory.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(substrate: MnemosyneLMESubstrate,
        instances: list[dict[str, Any]],
        *,
        judge: str = "substring",
        judge_model: str = "gpt-4o-mini",
        llm_grounded: bool = False,
        on_progress: Callable[[int, int, dict[str, Any]], None] | None = None,
        ) -> dict[str, Any]:
    """Per question: ingest haystack, probe, judge, score retrieval.

    Scoring protocol:
      * non-abstention questions: judge(expected, response); headline
        `score` covers these. Abstention questions (`question_id`
        ending `_abs`) are scored via abstention detection only in
        --llm-grounded mode, reported separately.
      * session recall@k / turn recall@k: judge-free; gold
        `answer_session_ids` / `has_answer` turns found among the
        retrieved rows.
    """
    token_count, token_method = _make_token_counter()
    per_question: list[dict[str, Any]] = []
    ingest = {"turns_total": 0, "seconds": 0.0, "tokens_est": 0}
    wall_t0 = time.monotonic()

    for idx, inst in enumerate(instances, start=1):
        t0 = time.monotonic()
        n_turns = substrate.ingest_instance(inst)
        ingest["seconds"] += time.monotonic() - t0
        ingest["turns_total"] += n_turns
        ingest["tokens_est"] += sum(
            token_count(t.get("content", "")) for s in
            (inst.get("haystack_sessions") or []) if isinstance(s, list)
            for t in s)

        qid = str(inst.get("question_id", ""))
        qtype = inst.get("question_type", "unknown")
        abstention = qid.endswith("_abs")
        question = inst.get("question", "")
        expected = "(abstain)" if abstention else str(inst.get("answer", ""))

        t1 = time.monotonic()
        try:
            actual = substrate.probe(question,
                                     question_date=inst.get("question_date"))
        except Exception as e:
            actual = f"[probe error: {type(e).__name__}: {e}]"
        latency_ms = (time.monotonic() - t1) * 1000.0
        probe_info = substrate.last_probe or {}
        rows = probe_info.get("retrieved_rows") or []

        # --- retrieval metrics (judge-free) ---
        gold_sessions = set(inst.get("answer_session_ids") or [])
        got_sessions = {r.get("session_id") for r in rows}
        session_recall = (round(len(gold_sessions & got_sessions)
                                / len(gold_sessions), 4)
                          if gold_sessions else None)
        gold_turns = {(t["session_id"], t["turn_idx"])
                      for t in substrate._turns if t.get("has_answer")}
        got_turns = {(r.get("session_id"), r.get("turn_idx")) for r in rows}
        turn_recall = (round(len(gold_turns & got_turns) / len(gold_turns), 4)
                       if gold_turns else None)

        # --- answer judging ---
        passed: bool | None
        if abstention:
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

        result = {
            "question_id": qid,
            "question_type": qtype,
            "abstention": abstention,
            "question": question,
            "expected": expected,
            "actual_preview": (actual or "")[:240],
            "passed": passed,
            "session_recall": session_recall,
            "turn_recall": turn_recall,
            "latency_ms": round(latency_ms, 2),
            "search_ms": probe_info.get("search_ms"),
            "llm_ms": probe_info.get("llm_ms"),
            "context_tokens_est": (probe_info.get("context_chars") or 0) // 4,
            "haystack_turns": n_turns,
        }
        per_question.append(result)
        if on_progress is not None:
            try:
                on_progress(idx, len(instances), result)
            except Exception:
                pass

    wall_s = time.monotonic() - wall_t0

    scored = [r for r in per_question if not r["abstention"]]
    abst = [r for r in per_question if r["abstention"]]
    abst_scored = [r for r in abst if r["passed"] is not None]
    total, passed_n = len(scored), sum(1 for r in scored if r["passed"])

    by_type: dict[str, dict[str, Any]] = {}
    for r in scored:
        slot = by_type.setdefault(r["question_type"],
                                  {"total": 0, "passed": 0,
                                   "session_recall": [], "turn_recall": []})
        slot["total"] += 1
        if r["passed"]:
            slot["passed"] += 1
        if r["session_recall"] is not None:
            slot["session_recall"].append(r["session_recall"])
        if r["turn_recall"] is not None:
            slot["turn_recall"].append(r["turn_recall"])

    sess_vals = [r["session_recall"] for r in scored
                 if r["session_recall"] is not None]
    turn_vals = [r["turn_recall"] for r in scored
                 if r["turn_recall"] is not None]
    search_lat = [r["search_ms"] for r in per_question
                  if r.get("search_ms") is not None]
    llm_lat = [r["llm_ms"] for r in per_question
               if r.get("llm_ms") is not None]
    ctx_tokens = [r["context_tokens_est"] for r in per_question]

    return {
        "score": round(passed_n / total, 4) if total else 0.0,
        "passed": passed_n,
        "total": total,
        "abstention": {
            "total": len(abst),
            "scored": len(abst_scored),
            "passed": sum(1 for r in abst_scored if r["passed"]),
            "score": (round(sum(1 for r in abst_scored if r["passed"])
                            / len(abst_scored), 4) if abst_scored else None),
            "note": ("abstention-judged (llm-grounded)" if llm_grounded
                     else "not scored: retrieval-only mode has no model "
                          "to abstain; excluded from headline score"),
        },
        "by_question_type": {
            t: {"total": v["total"], "passed": v["passed"],
                "score": round(v["passed"] / v["total"], 4),
                "session_recall_mean": (round(statistics.fmean(
                    v["session_recall"]), 4) if v["session_recall"] else None),
                "turn_recall_mean": (round(statistics.fmean(
                    v["turn_recall"]), 4) if v["turn_recall"] else None)}
            for t, v in sorted(by_type.items())
        },
        "session_recall": {
            "mean": round(statistics.fmean(sess_vals), 4) if sess_vals else None,
            "all_found_rate": (round(sum(1 for v in sess_vals if v == 1.0)
                                     / len(sess_vals), 4) if sess_vals else None),
            "n": len(sess_vals),
        },
        "turn_recall": {
            "mean": round(statistics.fmean(turn_vals), 4) if turn_vals else None,
            "n": len(turn_vals),
        },
        "latency": {
            "search": _latency_block(search_lat),
            "llm": _latency_block(llm_lat),
            "wall_clock_s": round(wall_s, 1),
        },
        "tokens": {
            "method": {"ingested": token_method, "context": "chars/4"},
            "ingested_total_est": ingest["tokens_est"],
            "context_per_probe_mean_est": (round(statistics.fmean(ctx_tokens))
                                           if ctx_tokens else 0),
            "context_per_probe_p95_est": (round(_pctl(sorted(ctx_tokens), 95))
                                          if ctx_tokens else 0),
        },
        "ingest": {
            "questions": len(instances),
            "turns_total": ingest["turns_total"],
            "seconds": round(ingest["seconds"], 2),
            "writes_per_sec": (round(ingest["turns_total"] / ingest["seconds"])
                               if ingest["seconds"] else None),
        },
        "cost_usd": 0.0 if judge != "openai" and not llm_grounded else None,
        "judge": judge,
        "results": per_question,
    }


# ---------------------------------------------------------------------------
# Selftest fixture
# ---------------------------------------------------------------------------

def _synthetic_dataset() -> list[dict[str, Any]]:
    """Tiny schema-faithful fixture so the runner is verifiable without
    redistributing LongMemEval. Three questions: answerable single-
    session, answerable multi-session, abstention."""
    filler = [
        [{"role": "user", "content": f"Tell me a fact about topic {i}."},
         {"role": "assistant", "content": f"Topic {i} is interesting."}]
        for i in range(6)
    ]

    def inst(qid, qtype, question, answer, evidence_sessions, extra_sessions):
        sessions, sids, dates = [], [], []
        for j, s in enumerate(extra_sessions):
            sessions.append(s)
            sids.append(f"filler_{qid}_{j}")
            dates.append("2025/01/0%d (Wed) 10:00" % (j + 1))
        for j, (sid, s) in enumerate(evidence_sessions):
            sessions.append(s)
            sids.append(sid)
            dates.append("2025/01/1%d (Fri) 09:00" % j)
        return {
            "question_id": qid,
            "question_type": qtype,
            "question": question,
            "answer": answer,
            "question_date": "2025/02/01 (Sat) 12:00",
            "haystack_session_ids": sids,
            "haystack_dates": dates,
            "haystack_sessions": sessions,
            "answer_session_ids": [sid for sid, _ in evidence_sessions],
        }

    return [
        inst("synth_1", "single-session-user",
             "What rare instrument does the user play?", "the theremin",
             [("ev_1", [
                 {"role": "user", "content":
                  "I started practicing the theremin again last night.",
                  "has_answer": True},
                 {"role": "assistant", "content":
                  "The theremin is a wonderful instrument!"}])],
             filler[:3]),
        inst("synth_2", "multi-session",
             "Which city is the user's marathon in?", "Reykjavik",
             [("ev_2a", [
                 {"role": "user", "content":
                  "I signed up for a marathon next spring.",
                  "has_answer": True},
                 {"role": "assistant", "content": "Congratulations!"}]),
              ("ev_2b", [
                  {"role": "user", "content":
                   "Booked my flights to Reykjavik for the race.",
                   "has_answer": True},
                  {"role": "assistant", "content": "Iceland in spring!"}])],
             filler[3:5]),
        inst("synth_3_abs", "single-session-user",
             "What is the name of the user's pet iguana?", "(abstain)",
             [("ev_3", [
                 {"role": "user", "content":
                  "I'm thinking about getting a pet someday."},
                 {"role": "assistant", "content": "Pets are great."}])],
             filler[5:]),
    ]


def _selftest() -> int:
    print("[longmemeval] selftest: synthetic 3-question fixture")
    sub = MnemosyneLMESubstrate(db_path=Path("/tmp/lme-selftest.db"))
    try:
        report = run(sub, _synthetic_dataset())
    finally:
        sub.close()
    ok = True
    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond
    check("answerable questions scored", report["total"] == 2)
    check("abstention split out", report["abstention"]["total"] == 1
          and report["abstention"]["scored"] == 0)
    check("evidence retrieved (session recall = 1.0)",
          report["session_recall"]["mean"] == 1.0)
    check("turn recall computed", report["turn_recall"]["mean"] is not None
          and report["turn_recall"]["mean"] > 0)
    check("answers found in context", report["score"] == 1.0)
    check("latency recorded", report["latency"]["search"]["n"] == 3)
    check("ingest counted", report["ingest"]["turns_total"] > 0)
    print(f"[longmemeval] selftest {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bench/longmemeval.py",
        description="LongMemEval benchmark runner for Mnemosyne.")
    p.add_argument("--dataset",
                   default=str(_DATA_DIR / "longmemeval_s_cleaned.json"),
                   help="path to longmemeval_{oracle,s_cleaned,m_cleaned}.json")
    p.add_argument("--max-questions", type=int, default=None)
    p.add_argument("--retrieval-mode",
                   choices=("fts", "recency", "random", "full"), default="fts")
    p.add_argument("--top-k", type=int, default=8)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--llm-grounded", action="store_true")
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--judge", choices=("substring", "openai"),
                   default="substring")
    p.add_argument("--judge-model", default="gpt-4o-mini")
    p.add_argument("--out", default="bench/results/longmemeval.json")
    p.add_argument("--db-path", default="/tmp/longmemeval-mnemo.db")
    p.add_argument("--selftest", action="store_true",
                   help="run the runner against a built-in synthetic "
                        "fixture; no dataset / network / LLM needed")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return _selftest()

    instances, sha = load_longmemeval(Path(args.dataset),
                                      max_questions=args.max_questions)
    print(f"[longmemeval] loaded {len(instances)} question(s) "
          f"sha256={sha[:16]}…", flush=True)

    db = Path(args.db_path)
    if db.exists():
        db.unlink()
    substrate = MnemosyneLMESubstrate(
        db_path=db, llm_grounded=args.llm_grounded,
        provider=args.provider, model=args.model,
        retrieval_mode=args.retrieval_mode, top_k=args.top_k,
        seed=args.seed)

    def progress(idx: int, total: int, r: dict[str, Any]) -> None:
        if not args.verbose:
            return
        if r["passed"] is None:
            mark = "·"
        else:
            mark = "✓" if r["passed"] else "✗"
        print(f"  {mark} [{idx:3d}/{total}] {r['question_type']:28s} "
              f"sess_recall={r['session_recall']} "
              f"{r['question'][:60]}", flush=True)

    try:
        report = run(substrate, instances, judge=args.judge,
                     judge_model=args.judge_model,
                     llm_grounded=args.llm_grounded,
                     on_progress=progress)
    finally:
        substrate.close()

    report["_metadata"] = {
        "benchmark": f"LongMemEval ({Path(args.dataset).name})",
        "runner": f"bench/longmemeval.py v{_RUNNER_VERSION}",
        "retrieval_mode": args.retrieval_mode,
        "top_k": args.top_k,
        "llm_grounded": args.llm_grounded,
        "provider": args.provider,
        "model": args.model,
        "judge": args.judge,
        "judge_model": args.judge_model if args.judge == "openai" else None,
        "dataset_sha256": sha,
        "git_commit": _git_commit(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "date_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "argv": (argv if argv is not None else sys.argv[1:]),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "results"},
                     indent=2))
    print(f"[longmemeval] full report: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
