"""
mnemosyne_avatar.py — derive an evolving avatar state from agent telemetry.

Purpose
-------
The dashboard shows a visual representation of the agent that changes
over time as the agent accumulates memories, completes goals, fires
inner-dialogue passes, dreams, and resists identity slips. This module
computes that state deterministically from observable agent data — no
magic, no separate ML model, no opaque "personality engine." Every
visual property maps directly to an integer or float you can grep out
of `avatar.json`.

Design (AGI-scaling-friendly)
-----------------------------
1. **Versioned schema.** Every avatar state carries `schema_version`.
   Additive keys only — new traits join, none get renamed or removed.
   Old `avatar.json` files load forever.
2. **Derived, not stored.** State is recomputed from telemetry on
   demand. The cached `avatar.json` is just a snapshot for the UI to
   read; truth lives in events.jsonl + memory.db.
3. **Interpretable.** Every trait answers "what observable fact made
   the avatar look this way?" — e.g. `glow_intensity = clip(0..1,
   memory_count / 1000)`.
4. **Future-facing.** The schema reserves slots for traits we haven't
   computed yet (e.g. `wisdom`, `restlessness`) so the UI animation
   layer can already wire them up.

Computed traits (v1 schema)
---------------------------
    schema_version       : 1
    epoch                : monotonically increasing tick counter
    age_days             : days since first memory write
    memory_count         : total rows in memories table
    l1_count, l2_count, l3_count : per-tier counts
    skills_count         : registered skills (incl. learned)
    learned_skills       : skills written via record_learned_skill
    goals_open           : open goal count
    goals_resolved       : resolved goal count (lifetime)
    dreams_count         : dream consolidation passes
    inner_dialogues      : inner-dialogue activations
    identity_slip_count  : total slip events (lower = healthier)
    identity_strength    : 1 - normalized_slip_rate (0..1)
    activity_score       : recent telemetry events / time window
    diversity_score      : distinct event_types / total events
    health               : composite [0..1]; weighted of strength + activity
    mood_phase           : "rest" | "focus" | "explore" | "consolidate"
    palette              : hex colors derived from health + activity
    aura_radius          : derived from memory_count
    rings                : derived from inner_dialogues
    pulses_per_minute    : derived from recent activity
    last_event_iso       : ISO timestamp of the most recent event

The UI reads `avatar.json` and animates accordingly. SVG rendering
lives in the frontend (mnemosyne_ui/); this module never touches
display code.

Stdlib only.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


# ---- color helpers ----------------------------------------------------------

def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def _hex(r: float, g: float, b: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        int(round(_lerp(0, 255, r))),
        int(round(_lerp(0, 255, g))),
        int(round(_lerp(0, 255, b))),
    )


def _palette_for(health: float, activity: float) -> dict[str, str]:
    """Derive a four-color palette from health (0..1) + activity (0..1).

    Health drives saturation/brightness (low = greyed out, high = vivid).
    Activity drives hue (low = cool blue/purple, high = warm orange/red
    accent for energy).
    """
    # Core teal/cyan that shifts toward magenta when activity rises
    h = max(0.0, min(1.0, activity))
    base_r = _lerp(0.20, 0.95, h)
    base_g = _lerp(0.85, 0.30, h)
    base_b = _lerp(0.95, 0.55, h)

    # Health = saturation: low health → grey
    desat = 1.0 - max(0.0, min(1.0, health)) * 0.9
    base_r = _lerp(base_r, 0.45, desat)
    base_g = _lerp(base_g, 0.45, desat)
    base_b = _lerp(base_b, 0.50, desat)

    accent_r = _lerp(0.85, 0.95, h)
    accent_g = _lerp(0.40, 0.75, 1.0 - h)
    accent_b = _lerp(0.95, 0.30, h)

    return {
        "core": _hex(base_r, base_g, base_b),
        "accent": _hex(accent_r, accent_g, accent_b),
        "bg": _hex(0.04, 0.05, 0.10),
        "rim": _hex(_lerp(base_r, 1.0, 0.15),
                     _lerp(base_g, 1.0, 0.15),
                     _lerp(base_b, 1.0, 0.20)),
    }


def _mood_phase(activity: float, dreams: int, inner: int) -> str:
    # Dreams dominate first — an idle agent can still be consolidating.
    if dreams > 0 and dreams > inner * 2:
        return "consolidate"
    if activity < 0.05:
        return "rest"
    if inner > 0 and inner >= dreams:
        return "focus"
    return "explore"


# ---- defaults / config --------------------------------------------------

def _default_projects_dir() -> Path:
    try:
        from mnemosyne_config import default_projects_dir
        return default_projects_dir()
    except ImportError:  # pragma: no cover
        import os
        raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
        return (Path(raw).expanduser().resolve() if raw
                else (Path.home() / "projects" / "mnemosyne").resolve())


# ---- data sources -------------------------------------------------------

def _read_memory_stats(memory_db: Path) -> dict[str, Any]:
    if not memory_db.exists():
        return {"total": 0, "L1": 0, "L2": 0, "L3": 0,
                "first_created_utc": None, "learned_skills": 0}
    conn = sqlite3.connect(str(memory_db))
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0]
        by_tier = {row[0]: row[1] for row in conn.execute(
            "SELECT tier, COUNT(*) FROM memories GROUP BY tier"
        )}
        first = conn.execute(
            "SELECT MIN(created_utc) FROM memories"
        ).fetchone()[0]
    except sqlite3.Error:
        return {"total": 0, "L1": 0, "L2": 0, "L3": 0,
                "first_created_utc": None, "learned_skills": 0}
    finally:
        conn.close()
    return {
        "total": total,
        "L1": by_tier.get(1, 0),
        "L2": by_tier.get(2, 0),
        "L3": by_tier.get(3, 0),
        "first_created_utc": first,
    }


def _scan_recent_events(experiments_dir: Path,
                          window_minutes: int = 60) -> dict[str, Any]:
    """Walk all runs' events.jsonl in the window, aggregate counts."""
    out = {
        "events_total": 0,
        "event_types": {},
        "identity_slips": 0,
        "dreams": 0,
        "inner_dialogues": 0,
        "goals_resolved": 0,
        "tool_calls_ok": 0,
        "tool_calls_err": 0,
        "last_event_iso": None,
        "turn_timestamps": [],      # for restlessness
        "evaluator_verdicts": [],   # for self_assessment
        "learned_skill_events": 0,  # for novelty
    }
    if not experiments_dir.is_dir():
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    last_iso: str | None = None
    for run_dir in experiments_dir.iterdir():
        # Skip the `latest` symlink and any other symlinks — we'll
        # already visit the real directory, double-counting otherwise.
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        events_file = run_dir / "events.jsonl"
        if not events_file.exists():
            continue
        try:
            mtime = datetime.fromtimestamp(events_file.stat().st_mtime,
                                              tz=timezone.utc)
        except OSError:
            continue
        # Cheap filter: skip files entirely before the window
        if mtime < cutoff and mtime < cutoff - timedelta(hours=24):
            continue
        with events_file.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out["events_total"] += 1
                et = e.get("event_type", "unknown")
                out["event_types"][et] = out["event_types"].get(et, 0) + 1
                ts = e.get("timestamp_utc")
                if ts and (last_iso is None or ts > last_iso):
                    last_iso = ts
                if et == "identity_slip_detected":
                    out["identity_slips"] += 1
                elif et == "dream_end":
                    out["dreams"] += 1
                elif et == "inner_dialogue_done":
                    out["inner_dialogues"] += 1
                    md = e.get("metadata") or {}
                    verdict = md.get("evaluator_verdict")
                    if verdict in ("accept", "revise"):
                        out["evaluator_verdicts"].append(verdict)
                elif et == "tool_call":
                    if e.get("status") == "error":
                        out["tool_calls_err"] += 1
                    else:
                        out["tool_calls_ok"] += 1
                elif et == "turn_end" and e.get("status") == "ok":
                    if ts:
                        out["turn_timestamps"].append(ts)
                elif et == "skill_learned":
                    out["learned_skill_events"] += 1
    out["last_event_iso"] = last_iso
    return out


def _read_goals(projects_dir: Path) -> dict[str, int]:
    goals_file = projects_dir / "goals.jsonl"
    if not goals_file.exists():
        return {"open": 0, "resolved": 0, "abandoned": 0}
    counts = {"open": 0, "resolved": 0, "abandoned": 0}
    with goals_file.open(encoding="utf-8") as f:
        for line in f:
            try:
                g = json.loads(line)
            except json.JSONDecodeError:
                continue
            status = g.get("status", "open")
            if status in counts:
                counts[status] += 1
    return counts


def _count_skills(projects_dir: Path) -> dict[str, int]:
    counts = {"total": 0, "learned": 0}
    for sub, key in (("skills", "total"), ("skills/learned", "learned")):
        d = projects_dir / sub
        if d.is_dir():
            n = sum(1 for _ in d.glob("*.md"))
            counts[key] += n
    counts["total"] += counts["learned"]
    # Add the 11 builtins so the dashboard reflects what's actually loaded
    try:
        from mnemosyne_skills_builtin import builtin_skill_names
        counts["total"] += len(builtin_skill_names())
    except ImportError:
        pass
    return counts


# ---- derived traits -----------------------------------------------------

def _compute_health(slip_count: int, events_total: int,
                     activity_score: float) -> tuple[float, float]:
    """Return (identity_strength, health) in [0..1]."""
    # Slip rate per 1000 events. 0 slips → strength=1; 100 slips/1000 → strength=0
    rate = (slip_count / events_total) if events_total else 0
    identity_strength = max(0.0, 1.0 - rate * 10)
    # Health composite
    health = 0.6 * identity_strength + 0.4 * min(1.0, activity_score * 1.5)
    return identity_strength, health


def _activity_score(events_total: int, window_minutes: int) -> float:
    """Saturating events-per-minute → [0..1]. 60 events/min ≈ 1.0."""
    if window_minutes <= 0:
        return 0.0
    return min(1.0, events_total / max(1, window_minutes) / 60.0)


def _diversity_score(event_types: dict[str, int]) -> float:
    n = len(event_types)
    if n <= 1:
        return 0.0
    # Saturate at 12 distinct types
    return min(1.0, n / 12.0)


# ---- AGI-scaling traits (computed when observable, null otherwise) ----------
#
# Each of these has a specific, defensible derivation. When the signal
# isn't available the slot stays `None` instead of being faked. The UI
# renders `None` as "not yet measured" instead of a number.

def _compute_novelty(
    skills_count_new: int,
    skills_count_total: int,
    window_days: float,
) -> float | None:
    """New skills learned per active-week, clipped [0..1]. Null when we
    have no history (age < 1 day) or no skills at all."""
    if window_days < 1.0 or skills_count_total == 0:
        return None
    per_week = (skills_count_new / window_days) * 7.0
    return round(min(1.0, per_week / 3.0), 4)   # 3 new/week → 1.0


def _compute_restlessness(
    gaps_s: list[float],
) -> float | None:
    """Coefficient of variation of inter-turn gaps, clipped [0..1].
    Null when we have fewer than 3 turns to compare."""
    if len(gaps_s) < 3:
        return None
    mean = sum(gaps_s) / len(gaps_s)
    if mean <= 0:
        return None
    var = sum((g - mean) ** 2 for g in gaps_s) / len(gaps_s)
    stdev = var ** 0.5
    cv = stdev / mean
    return round(min(1.0, cv / 2.0), 4)   # CV=2.0 → 1.0


def _compute_wisdom(
    memory_count: int,
    age_days: float,
    identity_strength: float,
) -> float | None:
    """Log-scale memory depth × age, gated on identity_strength.
    Intuition: an agent that has persisted long, accumulated memory,
    and not lost itself has more 'wisdom' than a new or confused one.
    Null for agents with no memory. Fully honest: not a deep signal,
    just a composite. The UI labels it as 'composite' in the trait
    grid so it isn't mistaken for an ML score.
    """
    import math as _m
    if memory_count == 0 or age_days < 0.5:
        return None
    depth = _m.log10(1 + memory_count) / 4.0   # 10k memories → 1.0
    age_factor = min(1.0, age_days / 90.0)     # 90 days → full weight
    raw = depth * age_factor * identity_strength
    return round(min(1.0, raw), 4)


def _compute_self_assessment(
    events: dict,
) -> float | None:
    """Mean evaluator verdict score over the window. Null when the
    Evaluator persona hasn't fired. Scale: 0 = all revise, 1 = all
    accept. Comes from inner_dialogue_done events whose metadata
    carries evaluator_verdict.

    Honest about the indirection: this reflects what the AGENT thought
    of its own output (via the Evaluator persona), not an independent
    judgment. Still informative — it tracks whether self-evaluation
    trends toward accept over time.
    """
    verdicts = events.get("evaluator_verdicts") or []
    if not verdicts:
        return None
    hits = sum(1 for v in verdicts if v == "accept")
    return round(hits / len(verdicts), 4)


# ---- main entry point ---------------------------------------------------

# Tiny cache so the dashboard's 4s poll doesn't re-scan all of
# events.jsonl + memory.db when nothing has changed. Keyed on a
# fingerprint of file mtimes (memory.db, goals.jsonl, every
# events.jsonl). Bypassed when projects_dir or window changes.
_STATE_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_STATE_CACHE_MAX_AGE_S = 30.0


def _state_fingerprint(pd: Path) -> tuple:
    """Stable token that changes when any agent state file changes.

    Cheap: stat() on a small set of files. We do NOT walk into
    events.jsonl line counts — mtime resolution is enough.
    """
    parts: list[tuple[str, float]] = []
    for name in ("memory.db", "goals.jsonl"):
        f = pd / name
        if f.exists():
            parts.append((name, f.stat().st_mtime))
    exp = pd / "experiments"
    if exp.is_dir():
        for run_dir in exp.iterdir():
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            ef = run_dir / "events.jsonl"
            if ef.exists():
                parts.append((run_dir.name, ef.stat().st_mtime))
    return tuple(parts)


def compute_state(
    *,
    projects_dir: Path | None = None,
    window_minutes: int = 60,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Compute the current avatar state. Pure read; no side effects.

    With `use_cache=True` (default), repeat calls within 30 seconds
    that see no file-mtime changes return the cached value. The
    dashboard polls every 4s; this drops state computation to a
    handful of stat() syscalls when nothing has changed.

    Pass `use_cache=False` for benchmarks or when you suspect a stale
    cache (e.g. external writes to memory.db).
    """
    pd_actual = projects_dir or _default_projects_dir()
    if use_cache:
        fp = _state_fingerprint(pd_actual)
        cache_key = (str(pd_actual), window_minutes, fp)
        cached = _STATE_CACHE.get(cache_key)
        if cached is not None:
            cached_at, state = cached
            from time import monotonic as _mono
            if (_mono() - cached_at) < _STATE_CACHE_MAX_AGE_S:
                return state
    state = _compute_state_fresh(projects_dir=pd_actual,
                                    window_minutes=window_minutes)
    if use_cache:
        from time import monotonic as _mono
        _STATE_CACHE[cache_key] = (_mono(), state)
        # Bound the cache so a long-running daemon doesn't accumulate
        # entries from many distinct (projects_dir, window) callers.
        if len(_STATE_CACHE) > 64:
            # Drop the oldest half — simplest LRU
            for k in sorted(_STATE_CACHE,
                              key=lambda k: _STATE_CACHE[k][0])[:32]:
                _STATE_CACHE.pop(k, None)
    return state


def _compute_state_fresh(
    *,
    projects_dir: Path | None = None,
    window_minutes: int = 60,
) -> dict[str, Any]:
    """Internal: always recomputes; no cache. The cached `compute_state`
    delegates here on a cache miss."""
    pd = projects_dir or _default_projects_dir()
    mem = _read_memory_stats(pd / "memory.db")
    evt = _scan_recent_events(pd / "experiments", window_minutes)
    goals = _read_goals(pd)
    skills = _count_skills(pd)

    activity = _activity_score(evt["events_total"], window_minutes)
    diversity = _diversity_score(evt["event_types"])
    identity_strength, health = _compute_health(
        evt["identity_slips"], max(1, evt["events_total"]), activity
    )
    palette = _palette_for(health, activity)
    mood = _mood_phase(activity, evt["dreams"], evt["inner_dialogues"])

    age_days = 0.0
    if mem["first_created_utc"]:
        try:
            first = datetime.fromisoformat(
                mem["first_created_utc"].replace("Z", "+00:00")
            )
            age_days = max(0.0,
                (datetime.now(timezone.utc) - first).total_seconds() / 86400.0)
        except (ValueError, AttributeError):
            pass

    aura_radius = 80 + 80 * (1 - math.exp(-mem["total"] / 500.0))
    pulses_per_minute = 6 + int(54 * activity)
    rings = min(8, evt["inner_dialogues"])

    # AGI-scaling traits — computed where observable, null otherwise
    gaps: list[float] = []
    ts_sorted = sorted(evt.get("turn_timestamps") or [])
    for a, b in zip(ts_sorted, ts_sorted[1:]):
        try:
            t1 = datetime.fromisoformat(a.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(b.replace("Z", "+00:00"))
            gaps.append((t2 - t1).total_seconds())
        except (ValueError, AttributeError):
            continue

    novelty = _compute_novelty(
        skills_count_new=evt.get("learned_skill_events", 0),
        skills_count_total=skills["total"],
        window_days=max(0.0, age_days),
    )
    restlessness = _compute_restlessness(gaps)
    wisdom_v = _compute_wisdom(mem["total"], age_days, identity_strength)
    self_assessment = _compute_self_assessment(evt)

    # Calibration — new in v0.6. Uses mnemosyne_predictions to score
    # prediction/outcome pairs over the window. None when fewer than
    # 3 resolved predictions exist (honest: no signal, no fake score).
    try:
        from mnemosyne_predictions import calibration_trait as _calib
        calibration = _calib(pd, window_minutes=window_minutes)
    except Exception:
        calibration = None

    return {
        "schema_version": SCHEMA_VERSION,
        "computed_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"),
        "age_days": round(age_days, 3),
        "memory_count": mem["total"],
        "l1_count": mem["L1"],
        "l2_count": mem["L2"],
        "l3_count": mem["L3"],
        "skills_count": skills["total"],
        "learned_skills": skills["learned"],
        "goals_open": goals["open"],
        "goals_resolved": goals["resolved"],
        "dreams_count": evt["dreams"],
        "inner_dialogues": evt["inner_dialogues"],
        "identity_slip_count": evt["identity_slips"],
        "identity_strength": round(identity_strength, 4),
        "tool_calls_ok": evt["tool_calls_ok"],
        "tool_calls_err": evt["tool_calls_err"],
        "activity_score": round(activity, 4),
        "diversity_score": round(diversity, 4),
        "health": round(health, 4),
        "mood_phase": mood,
        "palette": palette,
        "aura_radius": round(aura_radius, 2),
        "rings": rings,
        "pulses_per_minute": pulses_per_minute,
        "last_event_iso": evt["last_event_iso"],
        # AGI-scaling traits — computed where observable, null otherwise.
        # See the `_compute_*` helpers for the exact derivations.
        "wisdom":          wisdom_v,          # log(mem) × age × identity
        "restlessness":    restlessness,      # CV of inter-turn gaps
        "novelty":         novelty,           # new skills per week
        "self_assessment": self_assessment,   # evaluator accept ratio
        # Self-calibration — property 4 of the cognitive-OS checklist.
        # 1 − mean(|confidence − actual_correctness|) over resolved
        # prediction/outcome pairs. Null with fewer than 3 resolved.
        "calibration":     calibration,
    }


def write_snapshot(state: dict[str, Any], projects_dir: Path | None = None) -> Path:
    """Persist `state` to $PROJECTS_DIR/avatar.json (atomic write).

    Returns the file path. The UI reads this file to render the avatar.
    """
    pd = projects_dir or _default_projects_dir()
    pd.mkdir(parents=True, exist_ok=True)
    target = pd / "avatar.json"
    tmp = pd / "avatar.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(target)
    return target


# ---- 8-bit pixel owl (v0.9.3) -----------------------------------------------
#
# A recognizable Mnemosyne mascot rendered entirely from SVG rects —
# every cell is one "pixel" in the classic 8-bit sprite sense.
#
# Grid is 16 wide × 16 tall. Each character in _OWL_SPRITE marks the
# material drawn in that cell:
#     '.'  transparent (background shows through)
#     'F'  primary feather       -> palette.core
#     'D'  dark feather outline  -> palette.rim
#     'T'  ear tuft (slightly darker than F)
#     'B'  belly / chest lighter -> blend of core + bg
#     'E'  eye white / iris      -> palette.bg or light-tint
#     'P'  pupil                 -> palette.accent
#     'K'  beak                  -> palette.rim
#     'C'  chest core glow (L0 instinct gut-feel indicator)
#
# The design is deliberately mirror-symmetric left/right so rendering
# is cheap and the animation hooks (blink, breathe, tuft sway) stay
# simple to reason about.

_OWL_SPRITE = [
    #0123456789012345
    "..TT........TT..",  # 0  ear tufts — paired, left/right symmetric
    ".TTT........TTT.",  # 1  tuft broadens
    ".TFF........FFT.",  # 2  tufts fade into head feathers
    "..DFFFFFFFFFFD..",  # 3  head top outline
    ".DFFFFFFFFFFFFD.",  # 4
    ".FFEEEFFFFEEEFF.",  # 5  eye iris — upper
    ".FEPPEFFFFEPPEF.",  # 6  pupils, row 1
    ".FEPPEFFFFEPPEF.",  # 7  pupils, row 2
    ".FFEEEFFFFEEEFF.",  # 8  eye iris — lower
    "..FFFFFKKFFFFF..",  # 9  head base + beak (beak centered below eyes)
    "..FFFFKKKKFFFF..",  # 10 beak widens
    ".FFBBBCCCCBBBFF.",  # 11 chest + L0 instinct glow (C cells)
    ".FFBBBBCCBBBBFF.",  # 12 chest continues, glow tapers
    ".FFBBBBBBBBBBFF.",  # 13 full belly
    "..FFBBBBBBBBFF..",  # 14 belly taper
    "...DDFFFFFFDD...",  # 15 feet (dark base)
]

# Sanity-check the sprite is 16×16 before use (fails at import time
# with a clear error if someone edits it wrong).
for _i, _row in enumerate(_OWL_SPRITE):
    if len(_row) != 16:
        raise AssertionError(
            f"_OWL_SPRITE row {_i} is {len(_row)} chars, expected 16: {_row!r}"
        )
if len(_OWL_SPRITE) != 16:
    raise AssertionError(
        f"_OWL_SPRITE must be 16 rows, got {len(_OWL_SPRITE)}"
    )


def _render_pixel_owl(
    *,
    cx: float,
    cy: float,
    core_r: float,
    palette: dict[str, str],
    mood: str,
    health: float,
    pulse_s: float,
    calibration: float | None,
    restlessness: float | None,
) -> list[str]:
    """Render a 16×16 pixel owl as a list of SVG <rect> fragments.

    Trait encoding:
        mood_phase → eye state (full pupils / narrow slits / closed)
        health     → feather saturation (low health = partial fade)
        pulse_s    → breathing animation period (whole owl gently
                     translates on an <animateTransform>)
        calibration → pupil micro-drift amplitude (well-calibrated
                     agents have steadier eyes)
        restlessness → ear-tuft twitch frequency
    """
    # Sprite cell size chosen so the owl fills ~2× core_r. With a
    # 16-cell sprite, cell = (2 * core_r) / 16.
    cell = max(3.0, (2.0 * core_r) / 16.0)
    # Top-left of the sprite grid
    x0 = cx - cell * 8.0
    y0 = cy - cell * 8.0

    feather  = palette["core"]
    rim      = palette["rim"]
    accent   = palette["accent"]
    bg       = palette["bg"]

    # Feather saturation dims when health is low; below 0.3 the owl
    # visibly fades.
    feather_opacity = 0.65 + 0.35 * max(0.0, min(1.0, health))
    dark_opacity    = 0.55 + 0.40 * max(0.0, min(1.0, health))

    # Mood → eye rendering. Three states:
    #   focus: full round pupils (alert)
    #   active: normal pupils (curious)
    #   rest: narrow slits (half-closed)
    if mood == "rest":
        pupil_style = "rest"
    elif mood == "focus":
        pupil_style = "focus"
    else:
        pupil_style = "active"

    # Calibration drives a tiny SMIL pupil-drift animation. Low
    # calibration → more drift (eyes dart); well-calibrated → steady.
    drift_px = (
        0.0 if calibration is None
        else max(0.0, min(cell * 0.25, (1.0 - calibration) * cell * 0.25))
    )

    # Restlessness → tuft sway amplitude (0..1 → 0..4 deg rotation).
    # None = "no signal yet" (fresh install with no turns), render static.
    _restless = 0.0 if restlessness is None else float(restlessness)
    tuft_sway_deg = max(0.0, min(4.0, _restless * 4.0))

    parts: list[str] = []
    # Group owl so we can breathe-animate the whole thing
    parts.append('<g id="mnemo-owl">')

    # Helper: emit a rect for a single grid cell
    def rect(r: int, c: int, fill: str, opacity: float = 1.0) -> str:
        # +0.2 / -0.4 inset erases hairline seams between rects on
        # browsers that don't align subpixel edges perfectly.
        return (
            f'<rect x="{x0 + c * cell - 0.2:.2f}" '
            f'y="{y0 + r * cell - 0.2:.2f}" '
            f'width="{cell + 0.4:.2f}" '
            f'height="{cell + 0.4:.2f}" '
            f'fill="{fill}" fill-opacity="{opacity:.2f}" '
            f'shape-rendering="crispEdges"/>'
        )

    # Pixel-blend belly colour from core + bg (70/30)
    def _mix(a: str, b: str, t: float) -> str:
        try:
            ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
            br, bg_, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
            rr = int(ar * (1 - t) + br * t)
            rg = int(ag * (1 - t) + bg_ * t)
            rb = int(ab * (1 - t) + bb * t)
            return f'#{rr:02x}{rg:02x}{rb:02x}'
        except (ValueError, IndexError):
            return a  # if palette isn't #rrggbb form, fall back

    belly_colour = _mix(feather, bg, 0.55) if feather.startswith('#') else feather
    chest_glow = accent  # L0 instinct "gut" marker

    # Pass 1: render non-eye, non-pupil cells
    for r, row in enumerate(_OWL_SPRITE):
        for c, ch in enumerate(row):
            if ch == '.':
                continue
            if ch in ('E', 'P'):
                continue  # handled by eye pass
            if ch == 'F':
                parts.append(rect(r, c, feather, feather_opacity))
            elif ch == 'D':
                parts.append(rect(r, c, rim, dark_opacity))
            elif ch == 'T':
                # Ear tufts get a subtle sway via group transform later
                continue  # handled by tuft pass
            elif ch == 'B':
                parts.append(rect(r, c, belly_colour, feather_opacity))
            elif ch == 'K':
                parts.append(rect(r, c, rim, 0.95))
            elif ch == 'C':
                # Chest-centre L0 glow — a little brighter than belly
                parts.append(rect(r, c, chest_glow, 0.55))

    # Eye pass — special-cased to support mood states. The four E
    # cells per eye form a 2×2 block; pupils are the two P cells.
    # In rest mood we draw a slit (single row of darker fill across
    # the middle of the eye block) instead of open pupils.
    for r, row in enumerate(_OWL_SPRITE):
        for c, ch in enumerate(row):
            if ch == 'E':
                if pupil_style == "rest":
                    # Fill eye cells with feather colour so they "close"
                    parts.append(rect(r, c, feather, feather_opacity))
                else:
                    # Eye-white — lighter tint, readable on dark bg
                    eye_bg = _mix(bg, feather, 0.2) if bg.startswith('#') else bg
                    parts.append(rect(r, c, eye_bg, 0.95))
            elif ch == 'P':
                if pupil_style == "rest":
                    parts.append(rect(r, c, rim, dark_opacity))  # slit
                elif pupil_style == "focus":
                    # Full solid accent pupil
                    parts.append(rect(r, c, accent, 1.0))
                else:
                    # Normal active: pupil with slight warmer tint
                    parts.append(rect(r, c, accent, 0.92))

    # Tuft pass — with SMIL rotation around each tuft group's
    # centre. Restlessness drives the sway amplitude.
    if tuft_sway_deg > 0.01:
        for side, (cols, angle_sign) in [("left",  ([0, 1, 2], +1)),
                                           ("right", ([13, 14, 15], -1))]:
            # Determine bounding box of this side's T cells
            t_cells = [(r, c) for r, row in enumerate(_OWL_SPRITE)
                        for c, ch in enumerate(row)
                        if ch == 'T' and c in cols]
            if not t_cells:
                continue
            parts.append('<g>')
            # Pivot at the bottom of the tuft column (blend into head)
            pivot_c = sum(c for _, c in t_cells) / len(t_cells)
            pivot_r = max(r for r, _ in t_cells) + 1
            pivot_x = x0 + pivot_c * cell + cell / 2
            pivot_y = y0 + pivot_r * cell
            for r, c in t_cells:
                parts.append(rect(r, c, rim, dark_opacity * 0.9))
            parts.append(
                f'<animateTransform attributeName="transform" '
                f'type="rotate" '
                f'values="0 {pivot_x:.2f} {pivot_y:.2f};'
                f'{angle_sign * tuft_sway_deg:.1f} {pivot_x:.2f} {pivot_y:.2f};'
                f'0 {pivot_x:.2f} {pivot_y:.2f}" '
                f'dur="{max(1.2, 3.0 - _restless * 2.0):.2f}s" '
                f'repeatCount="indefinite"/>'
            )
            parts.append('</g>')
    else:
        # No sway — render tufts statically
        for r, row in enumerate(_OWL_SPRITE):
            for c, ch in enumerate(row):
                if ch == 'T':
                    parts.append(rect(r, c, rim, dark_opacity * 0.9))

    # Pupil drift: small side-to-side motion on the pupil rects when
    # calibration is low. Encoded as a transparent overlay that shifts
    # just enough to look like gaze wander without looking glitchy.
    if drift_px > 0.1:
        parts.append(
            f'<animateTransform xlink:href="#mnemo-owl" '
            f'attributeName="transform" '
            f'type="translate" additive="sum" '
            f'values="0 0; {drift_px:.2f} 0; 0 0; -{drift_px:.2f} 0; 0 0" '
            f'dur="3.5s" repeatCount="indefinite"/>'
        )

    # Breathing animation — whole owl group gently translates up/down
    # in sync with the aura pulse.
    parts.append(
        f'<animateTransform attributeName="transform" '
        f'type="translate" additive="sum" '
        f'values="0 0; 0 -{cell * 0.12:.2f}; 0 0" '
        f'dur="{pulse_s:.2f}s" repeatCount="indefinite"/>'
    )

    parts.append('</g>')
    return parts


def render_svg(state: dict[str, Any], *, size: int = 500) -> str:
    """Server-side SVG render — mirrors the JS renderer in
    `mnemosyne_ui/static/avatar.js`. Useful for docs screenshots and
    `mnemosyne-avatar render-svg` without spinning up a browser.

    As of v0.9.3, the centre of the avatar is an 8-bit pixel owl
    (see ``_render_pixel_owl``). Everything else — aura, habitat
    memory-tier bands, wisdom ring, self-assessment rays, orbiters,
    scars, memory roots — is unchanged.

    Output is a self-contained <svg>…</svg> string with embedded
    SMIL animation (renders animated in any modern browser; static in
    images-only viewers).
    """
    import math as _math
    cx = size / 2
    cy = size / 2 + 10
    palette = state["palette"]
    bg = palette["bg"]
    core = palette["core"]
    rim = palette["rim"]
    accent = palette["accent"]
    aura_r = float(state.get("aura_radius", 80))
    pulses_per_min = max(1, int(state.get("pulses_per_minute", 6)))
    pulse_s = max(0.6, min(6.0, 60.0 / pulses_per_min))
    rings = int(state.get("rings", 0))
    health = float(state.get("health", 0.5))
    activity = float(state.get("activity_score", 0.0))
    skills_count = int(state.get("skills_count", 0))
    slip_count = int(state.get("identity_slip_count", 0))
    mood = state.get("mood_phase", "rest")
    # v0.9.3: eye-open sizing moved into _render_pixel_owl (per-cell
    # sprite mood states). The old `eye_open` height was unused after
    # the pixel-owl replacement.

    parts: list[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" '
                  f'viewBox="0 0 {size} {size}" '
                  f'role="img" aria-label="Mnemosyne avatar — '
                  f'mood {mood}, health {int(health * 100)}%">')
    parts.append('<defs>')
    parts.append(f'<radialGradient id="auraGrad" cx="50%" cy="50%" r="60%">'
                  f'<stop offset="0%" stop-color="{core}" stop-opacity="0.55"/>'
                  f'<stop offset="55%" stop-color="{core}" stop-opacity="0.18"/>'
                  f'<stop offset="100%" stop-color="{core}" stop-opacity="0"/>'
                  f'</radialGradient>')
    parts.append(f'<radialGradient id="coreGrad" cx="50%" cy="45%" r="60%">'
                  f'<stop offset="0%" stop-color="{rim}"/>'
                  f'<stop offset="55%" stop-color="{core}"/>'
                  f'<stop offset="100%" stop-color="{bg}"/>'
                  f'</radialGradient>')
    parts.append('</defs>')

    # Background panel rectangle for standalone views
    parts.append(f'<rect x="0" y="0" width="{size}" height="{size}" '
                  f'fill="{bg}"/>')

    # Habitat: three memory-tier wave bands
    total_mem = (int(state.get("l1_count", 0)) + int(state.get("l2_count", 0))
                   + int(state.get("l3_count", 0)))
    if total_mem > 0:
        hab = 100.0
        l1h = min(hab * 0.50, (state["l1_count"] / total_mem) * hab * 0.9)
        l2h = min(hab * 0.70, (state["l2_count"] / total_mem) * hab * 0.9)
        l3h = min(hab * 0.90, (state["l3_count"] / total_mem) * hab * 0.9)

        def wave(h: float, amp: float) -> str:
            return (f"M0,{size} L0,{size - h:.1f} "
                    f"Q{size*0.3:.1f},{size - h - amp:.1f} "
                    f"{size*0.5:.1f},{size - h - amp/2:.1f} "
                    f"T{size},{size - h:.1f} L{size},{size} Z")

        parts.append(f'<path d="{wave(l3h, 12)}" fill="{rim}" '
                      f'fill-opacity="0.10"/>')
        parts.append(f'<path d="{wave(l2h, 8)}" fill="{core}" '
                      f'fill-opacity="0.12"/>')
        parts.append(f'<path d="{wave(l1h, 6)}" fill="{accent}" '
                      f'fill-opacity="0.15"/>')

    # Aura
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="{aura_r * 1.55:.1f}" '
                  f'fill="url(#auraGrad)" opacity="0.85">'
                  f'<animate attributeName="opacity" '
                  f'values="0.35;0.95;0.35" dur="{pulse_s:.2f}s" '
                  f'repeatCount="indefinite"/>'
                  f'</circle>')

    # Wisdom ring (outer, subtle, null-safe)
    wisdom = state.get("wisdom")
    if wisdom is not None and wisdom > 0:
        wr = aura_r * 1.95
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{wr:.1f}" '
                      f'fill="none" stroke="{accent}" '
                      f'stroke-opacity="{0.10 + 0.30 * wisdom:.3f}" '
                      f'stroke-width="0.8" stroke-dasharray="4 6"/>')

    # Self-assessment rays
    self_assess = state.get("self_assessment")
    if self_assess is not None:
        ray_count = max(0, min(12, int(round(self_assess * 12))))
        for i in range(ray_count):
            a = (i / 12.0) * _math.pi * 2 + _math.pi / 12.0
            r1 = aura_r * 0.35
            r2 = aura_r * 0.52
            x1 = cx + r1 * _math.cos(a); y1 = cy + r1 * _math.sin(a)
            x2 = cx + r2 * _math.cos(a); y2 = cy + r2 * _math.sin(a)
            parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" '
                          f'x2="{x2:.1f}" y2="{y2:.1f}" '
                          f'stroke="{rim}" stroke-opacity="0.55" '
                          f'stroke-width="1.1" stroke-linecap="round"/>')

    # Inner-dialogue rings
    for i in range(rings):
        r = aura_r * (0.65 + i * 0.12)
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" '
                      f'fill="none" stroke="{accent}" '
                      f'stroke-opacity="{0.18 + 0.06 * (rings - i):.2f}" '
                      f'stroke-width="1.2"/>')

    # Core orb halo (behind the pixel owl; subtle)
    core_r = 56 + health * 12
    parts.append(f'<circle cx="{cx}" cy="{cy + 2}" r="{core_r * 0.9:.1f}" '
                  f'fill="url(#coreGrad)" fill-opacity="0.45" '
                  f'stroke="{rim}" stroke-opacity="0.35" '
                  f'stroke-width="1.2"/>')

    # v0.9.3 — 8-bit pixel owl. Replaces the abstract orb/eye with a
    # recognizable Mnemosyne mascot while still encoding every trait
    # the old renderer did (mood → eye state, health → feather density,
    # calibration → pupil steadiness, breathing pulse → SMIL animation).
    parts.extend(_render_pixel_owl(
        cx=cx, cy=cy, core_r=core_r,
        palette=palette,
        mood=mood,
        health=health,
        pulse_s=pulse_s,
        calibration=state.get("calibration"),
        restlessness=state.get("restlessness", 0.0),
    ))

    # Orbiters
    orbiter_count = max(0, min(12, skills_count))
    orbit_r = aura_r + 28
    orbit_speed = max(4.0, min(20.0, 20 - activity * 14))
    parts.append('<g>')
    for i in range(orbiter_count):
        a = (i / max(1, orbiter_count)) * _math.pi * 2
        ox = cx + orbit_r * _math.cos(a)
        oy = cy + orbit_r * _math.sin(a)
        parts.append(f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="3" '
                      f'fill="{accent}" opacity="0.78"/>')
    parts.append(f'<animateTransform attributeName="transform" '
                  f'type="rotate" from="0 {cx} {cy}" to="360 {cx} {cy}" '
                  f'dur="{orbit_speed:.1f}s" repeatCount="indefinite"/>'
                  f'</g>')

    # Scars (identity slips)
    scar_count = max(0, min(12, slip_count))
    for i in range(scar_count):
        a = (i / max(1, scar_count)) * _math.pi * 2
        r1 = core_r + 4
        r2 = r1 + 4
        x1 = cx + r1 * _math.cos(a); y1 = cy + r1 * _math.sin(a)
        x2 = cx + r2 * _math.cos(a + 0.16); y2 = cy + r2 * _math.sin(a + 0.16)
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" '
                      f'x2="{x2:.1f}" y2="{y2:.1f}" '
                      f'stroke="#f25c6f" stroke-opacity="0.45" '
                      f'stroke-width="1.6" stroke-linecap="round"/>')

    # Memory roots
    tiers = [
        (state.get("l1_count", 0), accent, cx - 20),
        (state.get("l2_count", 0), core,   cx),
        (state.get("l3_count", 0), rim,    cx + 20),
    ]
    root_base = cy + core_r - 4
    for count, colour, x in tiers:
        length = max(20, min(110, 20 + _math.log10(1 + count) * 30))
        parts.append(f'<line x1="{x}" y1="{root_base:.1f}" '
                      f'x2="{x}" y2="{root_base + length:.1f}" '
                      f'stroke="{colour}" stroke-opacity="0.55" '
                      f'stroke-width="2" stroke-linecap="round"/>')

    parts.append('</svg>')
    return "".join(parts)


def read_snapshot(projects_dir: Path | None = None) -> dict[str, Any] | None:
    pd = projects_dir or _default_projects_dir()
    target = pd / "avatar.json"
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---- bidirectional feedback ------------------------------------------------
#
# Today the avatar *visualizes* agent state. `apply_feedback` closes
# the loop: avatar state flows BACK into the brain's runtime config,
# so an observably-unhealthy agent behaves more conservatively and an
# observably-wise one gets more room. Rules are deterministic and
# small — easy to audit, easy to override.
#
# The brain calls this at the start of each turn (cheap: dict reads
# + integer comparisons). Every adjustment logs an `avatar_feedback`
# telemetry event so the observability substrate sees feedback as a
# first-class action, not opaque magic.

@dataclass
class FeedbackAdjustment:
    """One rule firing. Describes what changed and why."""
    rule: str
    field: str
    old: Any
    new: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "field": self.field,
                "old": self.old, "new": self.new, "reason": self.reason}


def _rule_low_health_reduces_retrieval(state, config):
    """health < 0.4 → reduce memory_retrieval_limit (agent struggling,
    don't overwhelm it)."""
    health = state.get("health", 1.0)
    if health >= 0.4:
        return None
    min_limit = 2
    current = getattr(config, "memory_retrieval_limit", 6)
    if current <= min_limit:
        return None
    new = max(min_limit, int(current * 0.6))
    config.memory_retrieval_limit = new
    return FeedbackAdjustment(
        rule="low_health_reduces_retrieval",
        field="memory_retrieval_limit",
        old=current, new=new,
        reason=f"health={health:.2f} < 0.4 — cap retrieval to avoid overload",
    )


def _rule_high_wisdom_expands_ceiling(state, config):
    """wisdom ≥ 0.5 → raise retrieval window; agent demonstrably
    handles context well."""
    wisdom = state.get("wisdom")
    if wisdom is None or wisdom < 0.5:
        return None
    current = getattr(config, "memory_retrieval_limit", 6)
    max_limit = 16
    if current >= max_limit:
        return None
    boost = int(2 + wisdom * 6)     # wisdom=0.5 → +5, 1.0 → +8
    new = min(max_limit, current + boost)
    if new == current:
        return None
    config.memory_retrieval_limit = new
    return FeedbackAdjustment(
        rule="high_wisdom_expands_ceiling",
        field="memory_retrieval_limit",
        old=current, new=new,
        reason=f"wisdom={wisdom:.2f} ≥ 0.5 — widen retrieval window",
    )


def _rule_high_restlessness_disables_inner_dialogue(state, config):
    """restlessness > 0.7 → pause inner-dialogue (user is thrashing,
    no point adding 3x latency per turn). Users override with
    `tags=['hard']` which force-triggers regardless."""
    restless = state.get("restlessness")
    if restless is None or restless < 0.7:
        return None
    if not getattr(config, "inner_dialogue_enabled", False):
        return None
    config.inner_dialogue_enabled = False
    return FeedbackAdjustment(
        rule="high_restlessness_disables_inner_dialogue",
        field="inner_dialogue_enabled",
        old=True, new=False,
        reason=f"restlessness={restless:.2f} — pause reflective mode",
    )


def _rule_consolidate_pauses_new_reasoning(state, config):
    """mood=consolidate → hold off on inner-dialogue so dreams catch
    up. Single-pass routing still runs."""
    if state.get("mood_phase") != "consolidate":
        return None
    if not getattr(config, "inner_dialogue_enabled", False):
        return None
    config.inner_dialogue_enabled = False
    return FeedbackAdjustment(
        rule="consolidate_pauses_new_reasoning",
        field="inner_dialogue_enabled",
        old=True, new=False,
        reason="mood=consolidate — let dreams consolidate memory first",
    )


def _rule_identity_weakness_locks_harder(state, config):
    """identity_strength < 0.85 → flip audit_only off so the
    rewrite filter actively protects instead of just measuring."""
    strength = state.get("identity_strength", 1.0)
    if strength >= 0.85:
        return None
    if not getattr(config, "enforce_identity_audit_only", False):
        return None
    config.enforce_identity_audit_only = False
    return FeedbackAdjustment(
        rule="identity_weakness_locks_harder",
        field="enforce_identity_audit_only",
        old=True, new=False,
        reason=f"identity_strength={strength:.2f} < 0.85 — enforce rewrites",
    )


FEEDBACK_RULES = [
    _rule_low_health_reduces_retrieval,
    _rule_high_wisdom_expands_ceiling,
    _rule_high_restlessness_disables_inner_dialogue,
    _rule_consolidate_pauses_new_reasoning,
    _rule_identity_weakness_locks_harder,
]


def apply_feedback(state, config, *, rules=None):
    """Apply every feedback rule to `config` in place. Returns the
    list of `FeedbackAdjustment` objects that fired — empty list
    means no rule short-circuited (agent healthy enough that nothing
    needs to change).

    The brain calls this at the start of each turn; rules are pure
    except for the in-place mutation of `config`.
    """
    applied: list[FeedbackAdjustment] = []
    for rule in (rules if rules is not None else FEEDBACK_RULES):
        try:
            adj = rule(state, config)
        except Exception:
            continue
        if adj is not None:
            applied.append(adj)
    return applied


# ---- CLI ----------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        prog="mnemosyne-avatar",
        description="Compute the current avatar state from telemetry. "
                    "Three subcommands: state (JSON), render-svg, watch.",
    )
    p.add_argument("--projects-dir")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("state", help="print state JSON (default)")
    sp.add_argument("--window-minutes", type=int, default=60)
    sp.add_argument("--no-write", action="store_true")

    rp = sub.add_parser("render-svg",
                          help="render the avatar to a self-contained SVG file")
    rp.add_argument("--out", required=True)
    rp.add_argument("--size", type=int, default=500)
    rp.add_argument("--window-minutes", type=int, default=60)

    args = p.parse_args(argv)
    pd = Path(args.projects_dir).expanduser() if args.projects_dir else None
    cmd = args.cmd or "state"

    if cmd == "state":
        state = compute_state(projects_dir=pd,
                                window_minutes=args.window_minutes)
        if not args.no_write:
            path = write_snapshot(state, pd)
            sys.stderr.write(f"avatar: wrote {path}\n")
        json.dump(state, sys.stdout, indent=2, default=str)
        print()
        return 0

    if cmd == "render-svg":
        state = compute_state(projects_dir=pd,
                                window_minutes=args.window_minutes)
        svg = render_svg(state, size=args.size)
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(svg, encoding="utf-8")
        sys.stderr.write(f"avatar: wrote {out} ({len(svg)} bytes)\n")
        return 0

    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main())
