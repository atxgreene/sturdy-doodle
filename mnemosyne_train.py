"""
mnemosyne_train.py — telemetry → LoRA training bridge.

Purpose
-------
Closes the last loop: captured turns → fine-tuning dataset → LoRA
adapter → deploy to LM Studio or Ollama → A/B-eval against the base.

Five subcommands:

    export     events.jsonl + memory.db → Hermes-compatible ShareGPT JSONL
    compress   port of Hermes's trajectory_compressor algorithm
    train      shell out to Unsloth (optional [train] extras_require)
    deploy     install a GGUF adapter into LM Studio or Ollama
    eval       run a scenario set through base + adapted, print Pareto delta

Design
------
- Core is stdlib-only. Training itself shells out to `_train_unsloth.py`
  so heavy deps (unsloth/torch/transformers) are only loaded when the
  user runs `mnemosyne-train train`.
- Output JSONL is **exactly** the format Hermes's batch_runner writes
  (verified from source). Our extra fields live under
  `metadata.mnemo_*` — downstream trainers ignore unknown keys.
- LM Studio and Ollama both consume GGUF; `deploy` handles the
  platform-specific install path for both.

See docs/TRAINING.md for methodology, minimum dataset sizes, and
honest caveats about what LoRA can and cannot do.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


# Approximate token estimator. For compressor sizing only. Users who
# want accurate tokens can wire tiktoken/transformers externally.
def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _utcnow_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _default_projects_dir() -> Path:
    try:
        from mnemosyne_config import default_projects_dir
        return default_projects_dir()
    except ImportError:
        raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
        return Path(raw).expanduser() if raw else (
            Path.home() / "projects" / "mnemosyne"
        )


# ---- export ----------------------------------------------------------------

@dataclass
class ExportSummary:
    trajectories_written: int = 0
    runs_scanned: int = 0
    runs_skipped: int = 0
    turns_total: int = 0
    fallback_to_memory_db: bool = False
    out_path: str = ""
    warnings: list[str] = field(default_factory=list)


def _load_events(events_file: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not events_file.exists():
        return out
    with events_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _trajectory_from_training_turn(
    turn_evt: dict[str, Any],
    tool_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the Hermes `conversations` list from one training_turn event."""
    md = turn_evt.get("metadata") or {}
    convs: list[dict[str, Any]] = []
    sys_prompt = md.get("system_prompt") or ""
    if sys_prompt:
        convs.append({"from": "system", "value": sys_prompt})
    convs.append({"from": "human", "value": md.get("user_message") or ""})

    tool_calls_raw = md.get("tool_calls") or []
    gpt_turn: dict[str, Any] = {
        "from": "gpt",
        "value": md.get("assistant_text") or "",
    }
    if tool_calls_raw:
        # Map Mnemosyne's shape {name, args, result} → OpenAI tool_calls
        gpt_turn["tool_calls"] = [
            {"name": tc.get("name"), "arguments": tc.get("args") or {}}
            for tc in tool_calls_raw
        ]
    convs.append(gpt_turn)

    # Emit one "tool" turn per tool call with its result, matching Hermes
    for tc in tool_calls_raw:
        result = tc.get("result")
        content = result if isinstance(result, str) else json.dumps(result, default=str)
        convs.append({"from": "tool", "value": content})

    # Additional tool_events from events.jsonl with this turn as parent
    # (only used if the training_turn didn't already include them — rare).
    if not tool_calls_raw and tool_events:
        for te in tool_events:
            convs.append({
                "from": "tool",
                "value": json.dumps(te.get("result"), default=str),
            })

    return convs


def _tool_stats_from_events(tool_events: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, dict[str, int]] = {}
    errs: dict[str, int] = {}
    for te in tool_events:
        name = te.get("tool") or "(unnamed)"
        slot = stats.setdefault(name, {"calls": 0, "ok": 0, "error": 0})
        slot["calls"] += 1
        if te.get("status") == "error":
            slot["error"] += 1
            etype = (te.get("error") or {}).get("type") or "Unknown"
            errs[etype] = errs.get(etype, 0) + 1
        else:
            slot["ok"] += 1
    return {"tool_stats": stats, "tool_error_counts": errs}


def _build_trajectory(
    *,
    prompt_index: int,
    conversations: list[dict[str, Any]],
    run_id: str,
    turn_number: int,
    model: str,
    provider: str,
    path_kind: str,
    tool_events: list[dict[str, Any]],
    completed: bool,
    extra_tags: list[str],
) -> dict[str, Any]:
    ts = _tool_stats_from_events(tool_events)
    api_calls = sum(v["calls"] for v in ts["tool_stats"].values()) + 1
    toolsets = sorted(ts["tool_stats"].keys())
    return {
        "prompt_index": prompt_index,
        "conversations": conversations,
        "metadata": {
            "mnemo_run_id": run_id,
            "mnemo_turn_number": turn_number,
            "mnemo_model": model,
            "mnemo_provider": provider,
            "mnemo_path": path_kind,
            "mnemo_tags": extra_tags,
        },
        "completed": completed,
        "partial": not completed,
        "api_calls": api_calls,
        "toolsets_used": toolsets,
        "tool_stats": ts["tool_stats"],
        "tool_error_counts": ts["tool_error_counts"],
    }


def _export_from_events(
    run_dir: Path,
    *,
    drop_identity_slips: bool,
    completed_only: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Walk one run's events.jsonl → list of trajectory dicts.

    Returns (trajectories, had_training_turn_events).
    """
    events = _load_events(run_dir / "events.jsonl")
    if not events:
        return [], False

    # Group children by parent_event_id
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        pid = e.get("parent_event_id")
        if pid:
            by_parent.setdefault(pid, []).append(e)

    run_has_slip = any(e.get("event_type") == "identity_slip_detected" for e in events)
    run_tags_raw = (events[0].get("metadata") or {}).get("tags") if events else None
    run_tags = list(run_tags_raw) if isinstance(run_tags_raw, list) else []

    if drop_identity_slips and run_has_slip:
        return [], False

    has_training_turns = any(e.get("event_type") == "training_turn" for e in events)
    trajectories: list[dict[str, Any]] = []
    turn_number = 0

    for e in events:
        if e.get("event_type") != "turn_start":
            continue
        turn_number += 1
        turn_evt_id = e.get("event_id")
        children = by_parent.get(turn_evt_id or "", []) if turn_evt_id else []
        training_turn = next(
            (c for c in children if c.get("event_type") == "training_turn"), None
        )
        turn_end = next(
            (c for c in children if c.get("event_type") == "turn_end"), None
        )
        tool_events = [c for c in children if c.get("event_type") == "tool_call"]

        completed = (turn_end or {}).get("status") == "ok"
        if completed_only and not completed:
            continue
        if training_turn is None:
            # Fallback handled by caller via memory.db path
            continue

        md = training_turn.get("metadata") or {}
        convs = _trajectory_from_training_turn(training_turn, tool_events)
        trajectories.append(_build_trajectory(
            prompt_index=len(trajectories),
            conversations=convs,
            run_id=run_dir.name,
            turn_number=turn_number,
            model=md.get("model") or "",
            provider=md.get("provider") or "",
            path_kind=md.get("path") or "single",
            tool_events=tool_events,
            completed=completed,
            extra_tags=run_tags,
        ))

    return trajectories, has_training_turns


def _export_memory_fallback(projects_dir: Path) -> list[dict[str, Any]]:
    """Reconstruct trajectories from memory.db Q:/A: rows when no
    training_turn events exist. Lossy — responses truncated at 500 chars."""
    db = projects_dir / "memory.db"
    if not db.exists():
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT content, created_utc FROM memories "
            "WHERE kind = 'turn' ORDER BY created_utc ASC"
        ).fetchall()
        conn.close()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        content = row["content"] or ""
        if not content.startswith("Q: "):
            continue
        try:
            prompt, answer = content[3:].split("\nA: ", 1)
        except ValueError:
            continue
        convs = [
            {"from": "human", "value": prompt.strip()},
            {"from": "gpt",   "value": answer.strip()},
        ]
        out.append({
            "prompt_index": i,
            "conversations": convs,
            "metadata": {
                "mnemo_source": "memory_db_fallback",
                "mnemo_truncated_at_chars": 500,
                "mnemo_created_utc": row["created_utc"],
            },
            "completed": True,
            "partial": False,
            "api_calls": 1,
            "toolsets_used": [],
            "tool_stats": {},
            "tool_error_counts": {},
        })
    return out


def export(
    *,
    projects_dir: Path | None = None,
    out: Path | None = None,
    window_days: int | None = None,
    completed_only: bool = True,
    drop_identity_slips: bool = False,
    allow_memory_fallback: bool = True,
) -> ExportSummary:
    """Walk experiments/, emit Hermes-compatible ShareGPT JSONL."""
    pd = projects_dir or _default_projects_dir()
    experiments = pd / "experiments"
    summary = ExportSummary()

    if out is None:
        training_dir = pd / "training"
        training_dir.mkdir(parents=True, exist_ok=True)
        out = training_dir / f"{_utcnow_slug()}.jsonl"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
    summary.out_path = str(out)

    run_dirs: list[Path] = []
    if experiments.is_dir():
        from datetime import timedelta
        cutoff = None
        if window_days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        for r in sorted(experiments.iterdir()):
            if not r.is_dir():
                continue
            if cutoff is not None:
                mtime = datetime.fromtimestamp(r.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
            run_dirs.append(r)

    any_training_turns = False
    all_trajs: list[dict[str, Any]] = []
    for r in run_dirs:
        trajs, had_tt = _export_from_events(
            r,
            drop_identity_slips=drop_identity_slips,
            completed_only=completed_only,
        )
        if had_tt:
            any_training_turns = True
        if not trajs and not had_tt:
            summary.runs_skipped += 1
        else:
            summary.runs_scanned += 1
        all_trajs.extend(trajs)

    if not all_trajs and allow_memory_fallback:
        fallback = _export_memory_fallback(pd)
        if fallback:
            summary.fallback_to_memory_db = True
            summary.warnings.append(
                "No training_turn events found. Used memory.db Q:/A: "
                "rows as fallback. Responses are truncated at 500 chars. "
                "Enable BrainConfig(capture_for_training=True) for full "
                "verbatim capture on future runs."
            )
            all_trajs = fallback

    # Re-index prompt_index consecutively across the full dataset
    with out.open("w", encoding="utf-8") as f:
        for i, t in enumerate(all_trajs):
            t["prompt_index"] = i
            f.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")

    summary.trajectories_written = len(all_trajs)
    summary.turns_total = len(all_trajs)
    _ = any_training_turns  # kept for future reporting
    return summary


# ---- compress ---------------------------------------------------------------
#
# Stdlib port of Hermes's `trajectory_compressor.py` algorithm. Same I/O
# schema so the outputs are interchangeable. Middle turns get collapsed
# into one summary message when total tokens exceed target_max_tokens.
# We preserve the head turns (system) and the last N turns.
#
# The stub summarizer concatenates the first ~100 chars of each middle
# turn. Users who want a proper summary can pipe the intermediate JSONL
# through an LLM of their choice via --summarizer-cmd.

def _stub_summarize(turns: list[dict[str, Any]]) -> str:
    snippets: list[str] = []
    for t in turns:
        v = (t.get("value") or "")[:100]
        role = t.get("from") or "?"
        snippets.append(f"[{role}] {v}")
    joined = " | ".join(snippets)
    return f"[CONTEXT SUMMARY of {len(turns)} turns]: {joined}"[:2000]


def compress_one(
    trajectory: dict[str, Any],
    *,
    target_max_tokens: int = 15250,
    protect_last_n_turns: int = 4,
    summary_target_tokens: int = 750,
    summarizer: Callable[[list[dict[str, Any]]], str] | None = None,
) -> dict[str, Any]:
    """Compress one Hermes-shaped trajectory in place (returns new dict)."""
    summarizer = summarizer or _stub_summarize
    turns = trajectory.get("conversations") or []
    if not turns:
        return trajectory

    tok = [_approx_tokens(str(t.get("value") or "")) for t in turns]
    total = sum(tok)
    if total <= target_max_tokens:
        return trajectory   # no compression needed

    # Head: leading system turns (compress everything after the last system turn)
    compress_start = 0
    for i, t in enumerate(turns):
        if t.get("from") != "system":
            compress_start = i
            break
    # Tail: last N protected
    compress_end = max(compress_start, len(turns) - max(0, protect_last_n_turns))

    if compress_end <= compress_start:
        return trajectory   # nothing to compress between head and tail

    tokens_to_save = total - target_max_tokens
    target_to_compress = tokens_to_save + summary_target_tokens

    accumulated = 0
    compress_until = compress_start
    for i in range(compress_start, compress_end):
        accumulated += tok[i]
        compress_until = i + 1
        if accumulated >= target_to_compress:
            break
    if accumulated < target_to_compress and compress_until < compress_end:
        compress_until = compress_end

    middle = turns[compress_start:compress_until]
    summary_text = summarizer(middle)
    compressed_turns: list[dict[str, Any]] = []
    compressed_turns.extend(turns[:compress_start])
    compressed_turns.append({"from": "human", "value": summary_text})
    compressed_turns.extend(turns[compress_until:])

    new_traj = dict(trajectory)
    new_traj["conversations"] = compressed_turns
    new_traj["compression_metrics"] = {
        "original_tokens": total,
        "compressed_tokens": sum(_approx_tokens(str(t.get("value") or ""))
                                    for t in compressed_turns),
        "was_compressed": True,
        "turns_before": len(turns),
        "turns_after": len(compressed_turns),
    }
    return new_traj


def compress_file(
    in_path: Path,
    out_path: Path,
    *,
    target_max_tokens: int = 15250,
    protect_last_n_turns: int = 4,
) -> dict[str, int]:
    """Stream-compress a JSONL file of trajectories."""
    n_read = n_compressed = 0
    with in_path.open(encoding="utf-8") as f, out_path.open("w", encoding="utf-8") as g:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                g.write(line + "\n" if line else "\n")
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_read += 1
            new_obj = compress_one(
                obj,
                target_max_tokens=target_max_tokens,
                protect_last_n_turns=protect_last_n_turns,
            )
            if new_obj.get("compression_metrics", {}).get("was_compressed"):
                n_compressed += 1
            g.write(json.dumps(new_obj, ensure_ascii=False, default=str) + "\n")
    return {"read": n_read, "compressed": n_compressed}


# ---- train ------------------------------------------------------------------
#
# Thin dispatcher: finds `_train_unsloth.py` next to this module and runs
# it with the chosen args. If `unsloth` isn't importable, prints the
# install command and exits 2. Training itself is out-of-process so the
# heavy deps never touch the `mnemosyne_train` import path.

_UNSLOTH_INSTALL_HINT = (
    "Unsloth is not installed. Install the optional [train] extra:\n"
    "    pip install -e '.[train]'\n"
    "Or install Unsloth directly:\n"
    "    pip install unsloth datasets transformers trl peft accelerate\n"
)


def _unsloth_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("unsloth") is not None
    except Exception:
        return False


def train(
    *,
    data: Path,
    base_model: str,
    out_dir: Path,
    backend: str = "unsloth",
    chat_template: str = "chatml",
    max_steps: int = 500,
    learning_rate: float = 2e-4,
    rank: int = 16,
    quant: str = "q4_k_m",
    dry_run: bool = False,
) -> int:
    if backend != "unsloth":
        print(f"train: backend {backend!r} not yet implemented "
              "(only 'unsloth' in v1)", file=sys.stderr)
        return 2
    if not _unsloth_available() and not dry_run:
        sys.stderr.write(_UNSLOTH_INSTALL_HINT)
        return 2

    wrapper = Path(__file__).resolve().parent / "_train_unsloth.py"
    if not wrapper.exists():
        print(f"train: subprocess wrapper missing at {wrapper}", file=sys.stderr)
        return 2

    argv = [
        sys.executable, str(wrapper),
        "--data", str(data),
        "--base-model", base_model,
        "--out-dir", str(out_dir),
        "--chat-template", chat_template,
        "--max-steps", str(max_steps),
        "--lr", str(learning_rate),
        "--rank", str(rank),
        "--quant", quant,
    ]
    if dry_run:
        argv.append("--dry-run")
    print(f"train: launching {' '.join(argv)}", file=sys.stderr)
    return subprocess.call(argv)


# ---- deploy -----------------------------------------------------------------

def _lmstudio_models_dir() -> Path:
    env = os.environ.get("LMSTUDIO_MODELS_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    sysname = platform.system()
    if sysname == "Windows":
        up = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return Path(up) / ".lmstudio" / "models"
    return Path.home() / ".lmstudio" / "models"


def _find_gguf(adapter_dir: Path) -> Path | None:
    candidates = sorted(adapter_dir.glob("*.gguf")) + sorted(adapter_dir.glob("**/*.gguf"))
    return candidates[0] if candidates else None


def deploy(
    adapter_dir: Path,
    *,
    to: str = "lmstudio",
    name: str = "mnemo-lora",
    base_model: str = "qwen3.5:9b",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Install the trained adapter into LM Studio or Ollama."""
    adapter_dir = Path(adapter_dir).expanduser()

    if to == "lmstudio":
        dest_dir = _lmstudio_models_dir() / "mnemosyne" / name
        gguf = _find_gguf(adapter_dir)
        dest_file = dest_dir / (gguf.name if gguf else f"{name}.gguf")
        if dry_run:
            return {"mode": "lmstudio", "dry_run": True,
                     "would_copy_from": str(gguf) if gguf else None,
                     "would_copy_to": str(dest_file)}
        if gguf is None:
            raise FileNotFoundError(
                f"no .gguf found under {adapter_dir}; run `train` first "
                f"or pass the adapter directory containing the gguf file."
            )
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(gguf, dest_file)
        return {"mode": "lmstudio", "copied": True, "path": str(dest_file),
                 "lmstudio_model_id": name,
                 "use_with": f"Backend(provider='lmstudio', default_model={name!r})"}

    if to == "ollama":
        gguf = _find_gguf(adapter_dir)
        modelfile_path = adapter_dir / "Modelfile"
        modelfile = (
            f"FROM {base_model}\n"
            + (f"ADAPTER {gguf}\n" if gguf else "")
            + 'TEMPLATE """{{ .Prompt }}"""\n'
        )
        if dry_run:
            return {"mode": "ollama", "dry_run": True,
                     "modelfile_path": str(modelfile_path),
                     "modelfile": modelfile, "name": name}
        modelfile_path.write_text(modelfile, encoding="utf-8")
        if not shutil.which("ollama"):
            return {"mode": "ollama", "ollama_found": False,
                     "modelfile_written": str(modelfile_path),
                     "next_step": "install Ollama, then run: "
                                    f"ollama create {name} -f {modelfile_path}"}
        rc = subprocess.call(["ollama", "create", name, "-f", str(modelfile_path)])
        return {"mode": "ollama", "ollama_exit": rc, "name": name,
                 "use_with": f"Backend(provider='ollama', default_model={name!r})"}

    raise ValueError(f"deploy: unknown target {to!r}")


# ---- eval -------------------------------------------------------------------
#
# A/B compare base model vs. LoRA-adapted model on a set of scenarios.
# Uses scenario_runner.run_scenarios(...) — verified signature:
# `harness(prompt, session) -> {"text": str, "tool_calls": [...]}`.

def _dominates(a: dict[str, float], b: dict[str, float],
                directions: dict[str, str]) -> bool:
    """Return True if run a Pareto-dominates run b across `directions`.

    directions[metric] is "max" or "min". `a` dominates `b` iff it is
    at least as good on every axis and strictly better on at least one.
    """
    at_least_as_good = True
    strictly_better = False
    for metric, direction in directions.items():
        av = a.get(metric)
        bv = b.get(metric)
        if av is None or bv is None:
            continue
        if direction == "max":
            if av < bv:
                at_least_as_good = False
                break
            if av > bv:
                strictly_better = True
        else:   # "min"
            if av > bv:
                at_least_as_good = False
                break
            if av < bv:
                strictly_better = True
    return at_least_as_good and strictly_better


def _build_model_harness(
    backend_spec: dict[str, Any],
    chat_fn: Callable[..., dict[str, Any]] | None = None,
) -> Callable[[str, Any], dict[str, Any]]:
    """Return a harness(prompt, session) → {text, tool_calls}."""
    import mnemosyne_models as models

    if chat_fn is None:
        chat_fn = models.chat

    backend = models.Backend(
        provider=backend_spec.get("provider", "ollama"),
        default_model=backend_spec["model"],
        url=backend_spec.get("url"),
    )

    def harness(prompt: str, session: Any) -> dict[str, Any]:
        resp = chat_fn(
            [{"role": "user", "content": prompt}],
            backend=backend, telemetry=session,
        )
        return {
            "text": (resp.get("text") if isinstance(resp, dict) else "") or "",
            "tool_calls": [tc.get("name") for tc in (resp.get("tool_calls") or [])],
        }
    return harness


def eval_ab(
    *,
    base: dict[str, Any],
    adapted: dict[str, Any],
    scenarios_paths: Iterable[Path],
    projects_dir: Path | None = None,
    base_chat_fn: Callable[..., dict[str, Any]] | None = None,
    adapted_chat_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run each scenario set through both harnesses, return delta report."""
    import harness_telemetry as ht
    import scenario_runner as sr

    scenarios: list[dict[str, Any]] = []
    for p in scenarios_paths:
        scenarios.extend(sr.load_scenarios(Path(p)))

    results: dict[str, dict[str, Any]] = {}
    for label, spec, chat_fn in (
        ("base", base, base_chat_fn),
        ("adapted", adapted, adapted_chat_fn),
    ):
        run_id = ht.create_run(
            model=spec["model"],
            tags=["train-eval", label],
            projects_dir=projects_dir,
            notes=f"mnemosyne-train eval: {label}",
        )
        with ht.TelemetrySession(run_id, projects_dir=projects_dir) as sess:
            harness = _build_model_harness(spec, chat_fn)
            r = sr.run_scenarios(scenarios=scenarios, harness=harness, session=sess)
        ht.finalize_run(run_id, metrics=r["metrics"], projects_dir=projects_dir)
        results[label] = {"run_id": run_id, **r}

    # Build the per-scenario delta
    def index_by_id(rs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {s.get("id"): s for s in rs if s.get("id")}
    base_by_id = index_by_id(results["base"].get("per_scenario") or [])
    adap_by_id = index_by_id(results["adapted"].get("per_scenario") or [])
    per_scen: list[dict[str, Any]] = []
    for sid in sorted(set(base_by_id) | set(adap_by_id)):
        b = base_by_id.get(sid, {})
        a = adap_by_id.get(sid, {})
        per_scen.append({
            "id": sid,
            "base_passed": b.get("passed"),
            "adapted_passed": a.get("passed"),
            "delta": (1 if a.get("passed") and not b.get("passed") else
                      -1 if b.get("passed") and not a.get("passed") else 0),
        })

    directions = {"accuracy": "max", "latency_ms_avg": "min"}
    base_metrics = results["base"]["metrics"]
    adap_metrics = results["adapted"]["metrics"]
    pareto = {
        "base_dominates_adapted": _dominates(base_metrics, adap_metrics, directions),
        "adapted_dominates_base": _dominates(adap_metrics, base_metrics, directions),
    }

    return {
        "base":   {"run_id": results["base"]["run_id"],   "metrics": base_metrics},
        "adapted": {"run_id": results["adapted"]["run_id"], "metrics": adap_metrics},
        "delta": {
            "accuracy":        adap_metrics.get("accuracy", 0)
                               - base_metrics.get("accuracy", 0),
            "latency_ms_avg":  adap_metrics.get("latency_ms_avg", 0)
                               - base_metrics.get("latency_ms_avg", 0),
            "passed":          adap_metrics.get("passed", 0)
                               - base_metrics.get("passed", 0),
        },
        "pareto": pareto,
        "per_scenario": per_scen,
    }


# ---- CLI --------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="mnemosyne-train",
        description="telemetry → LoRA → LM Studio/Ollama. Five subcommands: "
                    "export, compress, train, deploy, eval. See docs/TRAINING.md.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ep = sub.add_parser("export",
                          help="events.jsonl + memory.db → Hermes-compatible ShareGPT JSONL")
    ep.add_argument("--projects-dir")
    ep.add_argument("--out")
    ep.add_argument("--window-days", type=int, default=None)
    ep.add_argument("--include-failed", action="store_true",
                     help="include runs that ended with status=error")
    ep.add_argument("--drop-identity-slips", action="store_true")
    ep.add_argument("--no-memory-fallback", action="store_true")
    ep.add_argument("--json", action="store_true")

    cp = sub.add_parser("compress", help="compress trajectories JSONL")
    cp.add_argument("input")
    cp.add_argument("--out", required=True)
    cp.add_argument("--target-max-tokens", type=int, default=15250)
    cp.add_argument("--protect-last-n-turns", type=int, default=4)

    tp = sub.add_parser("train", help="run LoRA training via Unsloth")
    tp.add_argument("--data", required=True)
    tp.add_argument("--base-model", required=True)
    tp.add_argument("--out-dir", required=True)
    tp.add_argument("--backend", default="unsloth")
    tp.add_argument("--chat-template", default="chatml")
    tp.add_argument("--max-steps", type=int, default=500)
    tp.add_argument("--lr", type=float, default=2e-4)
    tp.add_argument("--rank", type=int, default=16)
    tp.add_argument("--quant", default="q4_k_m")
    tp.add_argument("--dry-run", action="store_true")

    dp = sub.add_parser("deploy", help="install an adapter into LM Studio or Ollama")
    dp.add_argument("adapter_dir")
    dp.add_argument("--to", choices=["lmstudio", "ollama"], default="lmstudio")
    dp.add_argument("--name", default="mnemo-lora")
    dp.add_argument("--base-model", default="qwen3.5:9b")
    dp.add_argument("--dry-run", action="store_true")
    dp.add_argument("--json", action="store_true")

    vp = sub.add_parser("eval", help="A/B compare base vs. LoRA-adapted")
    vp.add_argument("--base-model", required=True)
    vp.add_argument("--base-provider", default="ollama")
    vp.add_argument("--adapted-model", required=True)
    vp.add_argument("--adapted-provider", default="lmstudio")
    vp.add_argument("--scenarios", nargs="+", required=True)
    vp.add_argument("--projects-dir")
    vp.add_argument("--out")
    vp.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    pd = Path(args.projects_dir).expanduser() \
        if getattr(args, "projects_dir", None) else None

    if args.cmd == "export":
        summary = export(
            projects_dir=pd,
            out=Path(args.out).expanduser() if args.out else None,
            window_days=args.window_days,
            completed_only=not args.include_failed,
            drop_identity_slips=args.drop_identity_slips,
            allow_memory_fallback=not args.no_memory_fallback,
        )
        if args.json:
            json.dump(summary.__dict__, sys.stdout, indent=2, default=str)
            print()
            return 0
        print(f"export: wrote {summary.trajectories_written} trajectories "
              f"from {summary.runs_scanned} runs → {summary.out_path}")
        for w in summary.warnings:
            print(f"  warning: {w}", file=sys.stderr)
        return 0

    if args.cmd == "compress":
        r = compress_file(Path(args.input).expanduser(),
                           Path(args.out).expanduser(),
                           target_max_tokens=args.target_max_tokens,
                           protect_last_n_turns=args.protect_last_n_turns)
        print(f"compress: read={r['read']} compressed={r['compressed']} "
              f"→ {args.out}")
        return 0

    if args.cmd == "train":
        return train(
            data=Path(args.data).expanduser(),
            base_model=args.base_model,
            out_dir=Path(args.out_dir).expanduser(),
            backend=args.backend,
            chat_template=args.chat_template,
            max_steps=args.max_steps,
            learning_rate=args.lr,
            rank=args.rank,
            quant=args.quant,
            dry_run=args.dry_run,
        )

    if args.cmd == "deploy":
        r = deploy(Path(args.adapter_dir).expanduser(), to=args.to, name=args.name,
                    base_model=args.base_model, dry_run=args.dry_run)
        if args.json:
            json.dump(r, sys.stdout, indent=2, default=str)
            print()
            return 0
        for k, v in r.items():
            print(f"  {k}: {v}")
        return 0

    if args.cmd == "eval":
        base = {"provider": args.base_provider, "model": args.base_model}
        adap = {"provider": args.adapted_provider, "model": args.adapted_model}
        report = eval_ab(
            base=base, adapted=adap,
            scenarios_paths=[Path(s).expanduser() for s in args.scenarios],
            projects_dir=pd,
        )
        if args.out:
            Path(args.out).expanduser().write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )
        if args.json:
            json.dump(report, sys.stdout, indent=2, default=str)
            print()
            return 0
        print(f"base     [{report['base']['run_id']}]    "
              f"acc={report['base']['metrics'].get('accuracy', 0):.3f} "
              f"lat={report['base']['metrics'].get('latency_ms_avg', 0):.1f}ms")
        print(f"adapted  [{report['adapted']['run_id']}] "
              f"acc={report['adapted']['metrics'].get('accuracy', 0):.3f} "
              f"lat={report['adapted']['metrics'].get('latency_ms_avg', 0):.1f}ms")
        print(f"delta: acc={report['delta']['accuracy']:+.3f}  "
              f"lat={report['delta']['latency_ms_avg']:+.1f}ms  "
              f"pass={report['delta']['passed']:+d}")
        pareto = report["pareto"]
        if pareto["adapted_dominates_base"]:
            print("pareto: adapted DOMINATES base ✓")
        elif pareto["base_dominates_adapted"]:
            print("pareto: base dominates adapted (regression)")
        else:
            print("pareto: tradeoff (neither dominates)")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_main())
