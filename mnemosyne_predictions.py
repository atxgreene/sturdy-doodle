"""
mnemosyne_predictions.py — prediction → outcome → calibration loop.

The cognitive-OS definition (see `docs/VISION.md`) requires the
runtime to emit predictions as first-class events, observe outcomes,
and adjust confidence over time. This module provides the two
halves: `predict(...)` and `observe(...)`, plus a calibration-score
reducer that rolls them up into an avatar trait.

Design
------
- **First-class events.** Predictions are emitted through the normal
  `TelemetrySession.log` path with `event_type="prediction"`. No new
  storage layer, no new database. They land in the same events.jsonl
  the rest of the system reads.
- **Deterministic resolution.** Each prediction has a `prediction_id`
  (UUID) the outcome references. An unresolved prediction is
  observable drift; the calibration reducer counts only resolved
  pairs.
- **No ML, no gradient.** Calibration is the mean absolute error
  between declared `confidence` and observed `actual_correctness`
  (0.0 for wrong, 1.0 for right, intermediate for partial).
- **Horizon-bounded.** Every prediction carries a `horizon_turns` or
  `horizon_seconds`. After the horizon, unresolved predictions
  auto-score as `error=0.5` (no information) so the calibration
  score penalizes making claims you never verify.

Calibration score
-----------------
Over a window of resolved predictions:

    calibration = 1 - mean(|confidence - actual_correctness|)

Range [0, 1]. A well-calibrated agent with confidence=0.8 gets the
answer right 80% of the time; that gives a calibration near 1.0.
Overconfident-but-wrong collapses fast — claiming 0.95 confidence
on a wrong answer contributes 0.95 to mean error, not 1.0. That
shape is intentional: small overreach costs small score; big
overreach costs big score.

Stdlib only.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---- prediction + outcome emit helpers -------------------------------------

def predict(
    telemetry: Any,
    *,
    claim: str,
    confidence: float,
    kind: str = "generic",
    horizon_turns: int | None = None,
    horizon_seconds: float | None = None,
    parent_event_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Emit a `prediction` event. Returns the prediction_id.

    Parameters
    ----------
    telemetry : TelemetrySession | None
        The active session. No-op if None.
    claim : str
        Human-readable claim the agent is committing to ("this tool call
        will succeed", "the user wants X"). Shown in the triage report.
    confidence : float
        Agent's own confidence in the claim. [0.0, 1.0]. Clamped.
    kind : str
        Category for clustering ("tool_success", "goal_progress",
        "plan_resolves", "user_intent", "generic"). Any string works.
    horizon_turns / horizon_seconds
        When this prediction should be resolved by. After the horizon
        an unresolved prediction auto-scores as uninformative.
    """
    if telemetry is None:
        return ""
    conf = max(0.0, min(1.0, float(confidence)))
    pid = f"pred_{uuid.uuid4().hex[:16]}"
    payload = {
        "prediction_id": pid,
        "kind": kind,
        "claim": claim,
        "confidence": conf,
        "emitted_at": _utcnow_iso(),
        "horizon_turns": horizon_turns,
        "horizon_seconds": horizon_seconds,
        **(metadata or {}),
    }
    try:
        telemetry.log("prediction",
                      metadata=payload,
                      parent_event_id=parent_event_id)
    except Exception:
        pass
    return pid


def observe(
    telemetry: Any,
    *,
    prediction_id: str,
    actual: str,
    actual_correctness: float,
    parent_event_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit an `outcome` event linking back to a prediction_id.

    `actual_correctness` is in [0.0, 1.0]:
      1.0 = claim was fully right
      0.0 = claim was fully wrong
      0.5 = partial / inconclusive / no information

    If `actual_correctness` comes from a scalar delta (e.g. absolute
    error of a numeric prediction), callers should map it into [0, 1]
    themselves before calling — this function doesn't normalize.
    """
    if telemetry is None or not prediction_id:
        return
    correctness = max(0.0, min(1.0, float(actual_correctness)))
    payload = {
        "prediction_id": prediction_id,
        "actual": actual,
        "actual_correctness": correctness,
        "observed_at": _utcnow_iso(),
        **(metadata or {}),
    }
    try:
        telemetry.log("outcome",
                      metadata=payload,
                      parent_event_id=parent_event_id)
    except Exception:
        pass


# ---- calibration-score reducer --------------------------------------------

@dataclass
class CalibrationReport:
    """Summary of prediction/outcome pairs over a window."""
    predictions_total: int = 0
    predictions_resolved: int = 0
    predictions_expired: int = 0     # past horizon, no outcome
    predictions_pending: int = 0     # still within horizon, no outcome
    mean_confidence: float = 0.0
    mean_correctness: float = 0.0
    calibration: float | None = None  # 1 - mean_abs_error, None if no data
    overconfident_wrong: int = 0     # confidence >= 0.8 and correctness <= 0.3
    underconfident_right: int = 0    # confidence <= 0.3 and correctness >= 0.7
    by_kind: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "predictions_total": self.predictions_total,
            "predictions_resolved": self.predictions_resolved,
            "predictions_expired": self.predictions_expired,
            "predictions_pending": self.predictions_pending,
            "mean_confidence": round(self.mean_confidence, 4),
            "mean_correctness": round(self.mean_correctness, 4),
            "calibration": (round(self.calibration, 4)
                            if self.calibration is not None else None),
            "overconfident_wrong": self.overconfident_wrong,
            "underconfident_right": self.underconfident_right,
            "by_kind": self.by_kind,
        }


def score_events(
    events: list[dict[str, Any]],
    *,
    now_iso: str | None = None,
) -> CalibrationReport:
    """Reduce a list of event dicts into a calibration report.

    Input events may come from any source (events.jsonl, a
    TelemetrySession buffer, a test fixture). We look for pairs of
    `prediction` + `outcome` with matching prediction_id. Unresolved
    predictions within their horizon are "pending"; past horizon they
    are "expired" and score as 0.5 correctness (uninformative).
    """
    preds: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    for e in events:
        et = e.get("event_type")
        md = e.get("metadata") or {}
        pid = md.get("prediction_id")
        if not pid:
            continue
        if et == "prediction":
            preds[pid] = md
        elif et == "outcome":
            outcomes[pid] = md

    now_dt = (datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
              if now_iso else datetime.now(timezone.utc))

    report = CalibrationReport()
    report.predictions_total = len(preds)
    per_kind: dict[str, list[tuple[float, float]]] = {}
    errors: list[float] = []
    confidences: list[float] = []
    correctnesses: list[float] = []

    for pid, p in preds.items():
        conf = float(p.get("confidence", 0.5))
        confidences.append(conf)
        kind = p.get("kind", "generic")

        out = outcomes.get(pid)
        if out is not None:
            # Resolved
            report.predictions_resolved += 1
            correctness = float(out.get("actual_correctness", 0.5))
        else:
            # Unresolved — is it past its horizon?
            emitted_at = p.get("emitted_at")
            horizon_s = p.get("horizon_seconds")
            horizon_t = p.get("horizon_turns")
            expired = False
            if emitted_at:
                try:
                    dt = datetime.fromisoformat(
                        emitted_at.replace("Z", "+00:00"))
                    age_s = (now_dt - dt).total_seconds()
                    if horizon_s is not None and age_s > horizon_s:
                        expired = True
                    elif horizon_t is None and horizon_s is None \
                            and age_s > 3600:
                        # Default 1-hour horizon if caller gave neither
                        expired = True
                except (ValueError, TypeError):
                    pass
            if expired:
                report.predictions_expired += 1
                correctness = 0.5    # uninformative
            else:
                report.predictions_pending += 1
                continue

        correctnesses.append(correctness)
        err = abs(conf - correctness)
        errors.append(err)

        if conf >= 0.8 and correctness <= 0.3:
            report.overconfident_wrong += 1
        if conf <= 0.3 and correctness >= 0.7:
            report.underconfident_right += 1

        per_kind.setdefault(kind, []).append((conf, correctness))

    if confidences:
        report.mean_confidence = sum(confidences) / len(confidences)
    if correctnesses:
        report.mean_correctness = sum(correctnesses) / len(correctnesses)
    if errors:
        report.calibration = 1.0 - (sum(errors) / len(errors))

    for kind, pairs in per_kind.items():
        confs = [c for c, _ in pairs]
        corrs = [r for _, r in pairs]
        errs = [abs(c - r) for c, r in pairs]
        report.by_kind[kind] = {
            "n": len(pairs),
            "mean_confidence": round(sum(confs) / len(confs), 4),
            "mean_correctness": round(sum(corrs) / len(corrs), 4),
            "calibration": round(1.0 - sum(errs) / len(errs), 4),
        }

    return report


def score_run(
    run_dir: Path,
    *,
    now_iso: str | None = None,
) -> CalibrationReport:
    """Score a single experiments/<run>/events.jsonl file."""
    events_file = run_dir / "events.jsonl"
    if not events_file.exists():
        return CalibrationReport()
    events: list[dict[str, Any]] = []
    with events_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return score_events(events, now_iso=now_iso)


def score_window(
    projects_dir: Path,
    *,
    window_minutes: int = 60,
    now_iso: str | None = None,
) -> CalibrationReport:
    """Score every run whose events.jsonl was modified within the
    window. Used by the avatar to compute the `calibration` trait."""
    exp = projects_dir / "experiments"
    if not exp.is_dir():
        return CalibrationReport()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    events: list[dict[str, Any]] = []
    for run_dir in exp.iterdir():
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        ef = run_dir / "events.jsonl"
        if not ef.exists():
            continue
        try:
            mtime = datetime.fromtimestamp(ef.stat().st_mtime,
                                            tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        with ef.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return score_events(events, now_iso=now_iso)


# For avatar integration — the one function the compute_state path calls
def calibration_trait(projects_dir: Path,
                        *,
                        window_minutes: int = 60) -> float | None:
    """Compute the `calibration` avatar trait.

    Returns the calibration score (0..1) if at least 3 resolved
    predictions exist in the window; None otherwise (honest: no
    signal, no fake number).
    """
    report = score_window(projects_dir, window_minutes=window_minutes)
    if report.predictions_resolved + report.predictions_expired < 3:
        return None
    return report.calibration


_ = math  # kept for future probabilistic-distance extensions
