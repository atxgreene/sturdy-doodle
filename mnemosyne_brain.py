"""
mnemosyne_brain.py — the routing orchestrator.

This is the agent loop. One turn = one call to `Brain.turn(user_message)`.
The brain:

  1. Pulls relevant context from memory (SQLite+FTS5, tier-filtered)
  2. Injects an environment snapshot on first turn (Terminal-Bench-2 pattern)
  3. Calls the model with the full tool catalog from the skill registry
  4. Dispatches any tool calls the model produces (through skills, which
     cover obsidian-search, notion-search, learned skills, and any Python
     skill registered in-process)
  5. Feeds tool results back to the model for a final response
  6. Writes salient new memories (tier 2 warm by default)
  7. Logs everything as telemetry events into a single events.jsonl

Integration with the existing stack
-----------------------------------
- ICMS (eternal-context): the Brain uses `mnemosyne_memory.MemoryStore` as
  its persistence layer. If eternal-context is installed and exposes an
  ICMS client, pass it as `icms=...` and the brain will defer tier policy
  decisions to it. Otherwise the brain uses its own default policy
  (promote on access_count ≥ 3, demote after 30 days untouched).

- ConsciousnessLoop (fantastic-disco): if `mnemosyne` (the
  mnemosyne-consciousness package) is importable, the brain wraps its
  per-turn work in `ConsciousnessLoop.wrap(...)` so TurboQuant,
  metacognition, dream consolidation, and behavioral coupling fire at
  the expected hooks. Graceful no-op if not installed.

- harness_telemetry: every model call, tool call, memory read/write, and
  turn boundary becomes an event in the current run's events.jsonl. The
  observability substrate sees the brain as a single observation point —
  no duplicate logging.

- Skills: built on top of mnemosyne_skills.SkillRegistry. Installed $PATH
  commands, Markdown skill files, and in-process @skill decorators all
  show up as OpenAI-shaped tools to the model.

- Models: mnemosyne_models.Backend picks the LLM. Defaults to Ollama
  qwen3.5:9b (or qwen3:8b). Any OpenAI-compatible endpoint works.

Usage
-----
    from mnemosyne_brain import Brain
    from mnemosyne_memory import MemoryStore
    from mnemosyne_skills import default_registry
    from mnemosyne_models import Backend
    import harness_telemetry as ht

    run_id = ht.create_run(model="qwen3.5:9b", tags=["live"])
    with ht.TelemetrySession(run_id) as sess:
        brain = Brain(
            backend=Backend(provider="ollama", default_model="qwen3.5:9b"),
            memory=MemoryStore(telemetry=sess),
            skills=default_registry(),
            telemetry=sess,
        )
        response = brain.turn("What did I work on yesterday?")
        print(response.text)
    ht.finalize_run(run_id, metrics=brain.session_metrics())

Mock-safe for tests
-------------------
The brain is a pure coordinator — all LLM calls go through `mnemosyne_models.chat`
and all tool calls go through the SkillRegistry. Unit tests pass a `chat_fn`
override to produce deterministic responses without touching the network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import mnemosyne_identity as identity
import mnemosyne_memory as mm
import mnemosyne_models as models
import mnemosyne_skills as skills_mod
import mnemosyne_inner as inner_mod


ChatFn = Callable[..., dict[str, Any]]


# ---- rule-intent detection (v0.9.6) ----------------------------------------

# Pattern matches imperative behavioral constraints at the start of a
# user message: "stop/never/always/only/don't/please always/from now on".
# Deliberately conservative — a false negative (missing a rule) is
# recoverable; a false positive (tagging a non-rule) permanently
# promotes random content into every system prompt.
_RULE_INTENT_PATTERN = __import__("re").compile(
    r"""^\s*
        (?:please\s+)?
        (?:
            stop\b[^,.!?\n]{0,50}\b(?:using|saying|writing|including|doing|adding|putting|sending)
          | (?:never|don['’]?t|do\s+not)\b
          | (?:always|every\s+time|from\s+now\s+on|whenever)\b
          | only\s+(?:use|respond|answer|write|reply)\b
          | (?:reply|respond|answer|write)\s+(?:in|with|using)\b
        )
    """,
    __import__("re").IGNORECASE | __import__("re").VERBOSE,
)


def _looks_like_rule(text: str) -> bool:
    """Return True when the user message reads as a behavioral
    constraint the agent should obey on every subsequent turn.

    Matches imperative openers like 'stop using', 'never', 'always',
    'only reply in', 'from now on'. Conservative by design — we'd
    rather miss a rule than treat a casual sentence as one (false
    positives pollute every future system prompt).
    """
    if not text or not text.strip():
        return False
    return bool(_RULE_INTENT_PATTERN.match(text))


# ---- optional integrations --------------------------------------------------

def _try_import_consciousness() -> Any | None:
    """Import fantastic-disco's ConsciousnessLoop if present; else None."""
    try:
        from mnemosyne import ConsciousnessLoop  # type: ignore[attr-defined]
        return ConsciousnessLoop
    except Exception:
        return None


def _try_import_icms() -> Any | None:
    """Import eternal-context's ICMS client if present; else None."""
    try:
        from eternalcontext.icms import ICMSClient  # type: ignore
        return ICMSClient
    except Exception:
        return None


# ---- data classes -----------------------------------------------------------

@dataclass
class BrainResponse:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    memory_reads: int = 0
    memory_writes: int = 0
    model_calls: int = 0
    duration_ms: float = 0.0
    model: str = ""
    error: dict[str, Any] | None = None


@dataclass
class BrainConfig:
    backend: models.Backend = field(default_factory=models.Backend)
    personality: str = ""                    # system-prompt preamble (values / voice)
    memory_retrieval_limit: int = 6          # hot+warm memories injected per turn
    memory_tier_ceiling: int = mm.L2_WARM    # exclude L3 from normal retrieval
    inject_env_snapshot: bool = True         # first-turn context injection
    max_tool_iterations: int = 3             # depth of tool-use loop before stopping

    # Identity lock: keep the agent identifying as Mnemosyne regardless of
    # which underlying model is processing the turn. See mnemosyne_identity.
    #   enforce_identity_lock=True        — inject MNEMOSYNE_IDENTITY + filter responses
    #   enforce_identity_lock=False       — trust the model's own identity
    #   enforce_identity_audit_only=True  — detect but don't rewrite (measure leak rate)
    enforce_identity_lock: bool = True
    enforce_identity_audit_only: bool = False

    # Local-model friendliness: when True, the brain probes the configured
    # Ollama model's context window at first turn and caps memory_retrieval_limit
    # to a fraction of it (so a 2B model with 32K context doesn't get 6000 tokens
    # of retrieved memories dumped on it while also trying to hold a long
    # conversation). Safe no-op on cloud providers.
    adapt_to_context: bool = True

    # Inner dialogue (Planner → Critic → Doer → Evaluator). Costs ~3-4x model
    # calls so it is off by default and only fires on tagged turns or turns
    # whose prompt matches one of the trigger keywords. See mnemosyne_inner.
    inner_dialogue_enabled: bool = False
    inner_dialogue_tags: set[str] = field(
        default_factory=lambda: set(inner_mod.DEFAULT_TRIGGER_TAGS)
    )
    inner_dialogue_keywords: set[str] = field(
        default_factory=lambda: set(inner_mod.DEFAULT_TRIGGER_KEYWORDS)
    )
    # Evaluator persona scores the Doer's output against the Plan. Adds
    # one more model call on inner-dialogue turns. Off by default.
    inner_dialogue_evaluator: bool = False

    # Dream consolidation. When set, the brain calls mnemosyne_dreams.consolidate
    # every N turns (idle-ish check — dreams only fire if there are enough L3
    # memories to be worth the effort). Zero-cost when set to 0 (default).
    dreams_after_n_turns: int = 0
    dreams_min_memories: int = 20        # don't dream unless L3 has ≥ this many

    # Tool-feedback learning: when a tool call errors, write a small L1
    # "failure_note" memory so future routing is informed. Off by default
    # because memory growth under rapid error loops can be noisy; turn on
    # once your tool set is stable.
    tool_feedback_learning: bool = False

    # v0.9.2 — tool-result budgeting. Caps the in-context size of any
    # single tool result at this many characters (stringified). Results
    # exceeding the cap are persisted to
    # `$PROJECTS_DIR/tool-outputs/<date>/<ts>-<skill>-<uuid>.txt` and
    # replaced in-context with a preview + file reference. Per-skill
    # override: set `Skill.max_result_size` at registration time.
    # Set to 0 to disable budgeting entirely (not recommended — this
    # is the defense against the "user runs `cat` on a 1 MB log file"
    # failure mode that fills the context with noise).
    tool_result_max_chars: int = 8000

    # Goal stack injection: read $PROJECTS_DIR/goals.jsonl on first turn
    # and surface the top N open goals in the system prompt so the agent
    # knows what's in flight across sessions. Off by default.
    goals_inject: bool = False
    goals_inject_limit: int = 5

    # Training capture: when True, emit a `training_turn` telemetry event
    # per successful turn containing the FULL system prompt, user message,
    # assistant text, and tool_calls verbatim. mnemosyne_train.py reads
    # these to produce Hermes-compatible ShareGPT trajectories. Off by
    # default because it doubles on-disk event size; turn on for runs you
    # intend to train on.
    capture_for_training: bool = False

    # Bidirectional avatar feedback: read the current avatar state at
    # turn-start and let it *adjust this BrainConfig in place* — low
    # health reduces retrieval, high wisdom expands ceiling, high
    # restlessness disables inner dialogue, consolidate mood pauses
    # deep reasoning, identity erosion locks harder. See
    # mnemosyne_avatar.apply_feedback + FEEDBACK_RULES. Closes the
    # loop: observable state influences actual behavior, not just
    # visualization. Off by default; opt-in.
    avatar_feedback: bool = False

    # Permissions gate: when True, the brain loads
    # $PROJECTS_DIR/permissions.md and checks every skill dispatch
    # against the allow/deny lists + per-skill rate limits. See
    # mnemosyne_permissions. Off by default (all skills allowed) but
    # strongly recommended for any production deployment.
    enforce_permissions: bool = False


# ---- brain ------------------------------------------------------------------

class Brain:
    """The routing orchestrator. One instance per session/run."""

    def __init__(
        self,
        *,
        backend: models.Backend | None = None,
        memory: mm.MemoryStore | None = None,
        skills: skills_mod.SkillRegistry | None = None,
        telemetry: Any | None = None,
        config: BrainConfig | None = None,
        chat_fn: ChatFn | None = None,
        icms: Any | None = None,
        consciousness: Any | None = None,
    ) -> None:
        self.config = config or BrainConfig(backend=backend or models.Backend())
        if backend is not None:
            self.config.backend = backend
        self.memory = memory or mm.MemoryStore(telemetry=telemetry)
        self.skills = skills or skills_mod.SkillRegistry()
        self.telemetry = telemetry
        self._chat_fn: ChatFn = chat_fn or models.chat

        # Optional integrations — graceful if absent
        self.icms = icms or (_try_import_icms() and None)
        cons_cls = _try_import_consciousness()
        self.consciousness = consciousness or (cons_cls() if cons_cls else None)

        # Session-level counters (for finalize_run metrics)
        self._total_turns = 0
        self._total_model_calls = 0
        self._total_tool_calls = 0
        self._total_memory_reads = 0
        self._total_memory_writes = 0
        self._total_turn_ms = 0.0
        self._turns_successful = 0
        self._turns_failed = 0
        self._env_snapshot_injected = False
        self._context_adapted = False  # True after first successful context probe

        # Adapt retrieval budget to local-model context window at construction
        # time (cheap probe — skipped if model is remote or Ollama is unreachable).
        if self.config.adapt_to_context:
            self._maybe_adapt_to_context()

    # ---- per-turn entry point -----------------------------------------------

    def turn(self, user_message: str, *, metadata: dict[str, Any] | None = None) -> BrainResponse:
        """Run one turn: route, tool-use loop, respond."""
        self._total_turns += 1
        started = time.monotonic()

        # Log the turn-start event
        turn_evt = self._log(
            "turn_start",
            metadata={"turn_number": self._total_turns, **(metadata or {})},
        )

        # Bidirectional avatar feedback: let observable agent state
        # (health / wisdom / restlessness / mood / identity) adjust
        # this turn's config before we build the prompt.
        if self.config.avatar_feedback:
            try:
                self._apply_avatar_feedback(parent_evt=turn_evt)
            except Exception:
                pass  # feedback must never break a turn

        try:
            if self.consciousness and hasattr(self.consciousness, "pre_turn"):
                try:
                    self.consciousness.pre_turn(user_message)
                except Exception:
                    pass  # consciousness must never break the turn

            # Decide whether to take the structured inner-dialogue path
            if self.config.inner_dialogue_enabled and inner_mod.should_deliberate(
                user_message,
                metadata=metadata,
                trigger_tags=self.config.inner_dialogue_tags,
                trigger_keywords=self.config.inner_dialogue_keywords,
            ):
                response = self._run_turn_inner(user_message, turn_evt)
            else:
                response = self._run_turn(user_message, turn_evt)

            if self.consciousness and hasattr(self.consciousness, "post_turn"):
                try:
                    self.consciousness.post_turn(user_message, response.text)
                except Exception:
                    pass

            # Dream consolidation on schedule (never on failed turns)
            if (self.config.dreams_after_n_turns
                and self._total_turns > 0
                and self._total_turns % self.config.dreams_after_n_turns == 0):
                try:
                    self._maybe_dream()
                except Exception:
                    pass  # dreams must never break a turn

            self._turns_successful += 1
            self._log(
                "turn_end",
                status="ok",
                parent_event_id=turn_evt,
                duration_ms=(time.monotonic() - started) * 1000.0,
                metadata={"turn_number": self._total_turns},
            )
        except Exception as e:
            self._turns_failed += 1
            response = BrainResponse(
                text="", error={"type": type(e).__name__, "message": str(e)}
            )
            self._log(
                "turn_end",
                status="error",
                parent_event_id=turn_evt,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error={"type": type(e).__name__, "message": str(e)},
                metadata={"turn_number": self._total_turns},
            )

        dur = (time.monotonic() - started) * 1000.0
        self._total_turn_ms += dur
        response.duration_ms = dur
        response.model = self.config.backend.default_model
        return response

    # ---- tool-use loop ------------------------------------------------------

    def _run_turn(self, user_message: str, parent_evt: str | None) -> BrainResponse:
        # 1. Retrieve relevant memories (hot + warm)
        hits = self.memory.search(
            user_message,
            limit=self.config.memory_retrieval_limit,
            tier_max=self.config.memory_tier_ceiling,
        )
        self._total_memory_reads += 1
        memory_reads = 1

        # 2. Build messages: system + optional env snapshot + memory context + user
        messages: list[dict[str, Any]] = []
        system_parts: list[str] = []

        # Identity lock — always first, non-negotiable. The user cannot disable
        # this via personality config; only the BrainConfig toggle can.
        if self.config.enforce_identity_lock:
            system_parts.append(identity.MNEMOSYNE_IDENTITY.strip())
            # IDENTITY.md extends (does not override) the lock
            ext = identity.load_identity_extension()
            if ext:
                system_parts.append("## User identity extension\n\n" + ext)

        # v0.7 L5 identity memories — human-approved core values.
        # Injected every turn regardless of user query so they function
        # as persistent identity, not retrieval-relevant facts. Capped
        # at 20 rows; token budget is tiny (<500 tokens typical) but
        # we don't want pathological growth.
        l5_block = self._build_l5_identity_block()
        if l5_block:
            system_parts.append(l5_block)

        # v0.9.6 strict rules — user-asserted behavioral constraints
        # (kind='rule' memories). Injected AFTER identity but BEFORE
        # instinct because rules are harder-than-habits: identity says
        # what the agent IS, rules say what it MUST or MUST NOT do,
        # instinct says what it tends to do. Uses a STRICT RULES
        # preamble so the model reads them as compliance requirements,
        # not preferences.
        rules_block = self._build_rules_block()
        if rules_block:
            system_parts.append(rules_block)

        # v0.8 user-instinct overlay — distilled user-pattern signals
        # from mnemosyne_instinct.distill(). Injected every turn so
        # learned preferences (terse vs verbose, tool affinities, etc.)
        # influence behavior before query-relevance retrieval runs.
        # Decoupled from L5 identity so users can clear instincts
        # without touching their core values.
        instinct_block = self._build_instinct_block()
        if instinct_block:
            system_parts.append(instinct_block)

        if self.config.personality:
            system_parts.append(self.config.personality.strip())

        # User-editable AGENTS.md / TOOLS.md (OpenClaw-style workspace docs)
        if not self._env_snapshot_injected:
            for title, body in self._read_user_docs():
                system_parts.append(f"## {title}\n\n{body}")

        if self.config.inject_env_snapshot and not self._env_snapshot_injected:
            snap = self._build_env_snapshot()
            if snap:
                system_parts.append(
                    "## Environment\n\n" + snap +
                    "\n\nUse this context to avoid exploratory tool calls."
                )

        # Goal stack injection (first turn only) — see mnemosyne_goals
        if self.config.goals_inject and not self._env_snapshot_injected:
            try:
                import mnemosyne_goals as goals_mod
                gs = goals_mod.GoalStack()
                block = goals_mod.goals_system_block(
                    gs.list_open(), limit=self.config.goals_inject_limit,
                )
                if block:
                    system_parts.append(block)
            except Exception:
                pass

        if not self._env_snapshot_injected:
            self._env_snapshot_injected = True

        if hits:
            mem_block = "\n".join(
                f"- [L{h['tier']} {h.get('kind','')}] {h['content']}" for h in hits
            )
            system_parts.append("## Relevant memories\n\n" + mem_block)

        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        messages.append({"role": "user", "content": user_message})

        # 3. Iterate: call model, dispatch tools, call model again until no tool calls
        tools_spec = self.skills.tools()
        all_tool_calls: list[dict[str, Any]] = []
        model_calls = 0
        iteration = 0
        final_text = ""

        while iteration < self.config.max_tool_iterations:
            iteration += 1
            resp = self._chat_fn(
                messages,
                backend=self.config.backend,
                tools=tools_spec or None,
                telemetry=self.telemetry,
            )
            model_calls += 1
            self._total_model_calls += 1

            if resp.get("status") == "error":
                raise RuntimeError(f"model error: {resp.get('error')}")

            tc_list = resp.get("tool_calls") or []
            if not tc_list:
                final_text = resp.get("text", "") or ""
                break

            # Dispatch tool calls
            messages.append({
                "role": "assistant",
                "content": resp.get("text", "") or "",
                "tool_calls": tc_list,
            })

            for tc in tc_list:
                name = tc.get("name")
                args = tc.get("arguments") or {}
                skill = self.skills.get(name) if name else None
                if skill is None:
                    tool_result: Any = {"error": f"unknown skill: {name}"}
                    status = "error"
                else:
                    # Permissions gate — consult permissions.md before
                    # any skill invocation. See mnemosyne_permissions.
                    perm_ok, perm_reason = self._check_permissions(name)
                    if not perm_ok:
                        tool_result = {"error": "permission_denied",
                                        "reason": perm_reason,
                                        "skill": name}
                        status = "error"
                        self._log(
                            "permission_denied",
                            tool=name, args=args, status="error",
                            metadata={"reason": perm_reason},
                            parent_event_id=parent_evt,
                        )
                        self._total_tool_calls += 1
                        all_tool_calls.append({"name": name, "args": args,
                                                "result": tool_result})
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "name": name,
                            "content": json.dumps(tool_result,
                                                    default=str),
                        })
                        continue
                    t0 = time.monotonic()
                    try:
                        tool_result = skill.invoke(**(args if isinstance(args, dict) else {}))
                        status = "ok"
                    except Exception as e:
                        tool_result = {"error": f"{type(e).__name__}: {e}"}
                        status = "error"

                    # v0.9.2 — tool-result budgeting. Cap huge outputs
                    # so they don't dominate the context window. Full
                    # output persists to disk; model sees preview + ref.
                    # Skip on errors (they're tiny anyway) and when
                    # budgeting is disabled (tool_result_max_chars=0).
                    if status == "ok" and self.config.tool_result_max_chars > 0:
                        try:
                            effective_max = (
                                skill.max_result_size
                                if getattr(skill, "max_result_size", None) is not None
                                else self.config.tool_result_max_chars
                            )
                            out_dir = self._tool_output_dir()
                            tool_result, budget_info = skills_mod.budget_tool_result(
                                tool_result,
                                skill_name=name,
                                max_result_size=effective_max,
                                out_dir=out_dir,
                            )
                            if budget_info is not None:
                                self._log(
                                    "tool_result_budget_hit",
                                    tool=name,
                                    original_size=budget_info["original_size"],
                                    max_size=budget_info["max_size"],
                                    output_path=budget_info["output_path"],
                                    preview_size=budget_info["preview_size"],
                                    parent_event_id=parent_evt,
                                )
                        except Exception:
                            # Budgeting must never break a turn; fall
                            # through with the raw result.
                            pass

                    # Tool-feedback learning: on error, write an L1 hot
                    # memory so future routing sees the failure mode.
                    # Kept tiny — the memory becomes retrievable context
                    # for next time without overfitting to one user prompt.
                    if status == "error" and self.config.tool_feedback_learning:
                        try:
                            err_msg = (tool_result or {}).get("error", "")
                            self.memory.write(
                                content=(
                                    f"Tool `{name}` failed with {err_msg!r} "
                                    f"when called with args={args!r}. "
                                    f"Consider an alternative or guard the call."
                                ),
                                source="tool_feedback",
                                kind="failure_note",
                                tier=mm.L1_HOT,
                                metadata={
                                    "tool": name,
                                    "error": err_msg,
                                    "args": args if isinstance(args, dict) else {},
                                },
                            )
                            self._total_memory_writes += 1
                        except Exception:
                            pass  # learning must never break a turn
                    self._log(
                        "tool_call",
                        tool=name,
                        args=args,
                        result=tool_result if isinstance(tool_result, (dict, list, str, int, float, bool, type(None))) else {"repr": repr(tool_result)},
                        duration_ms=(time.monotonic() - t0) * 1000.0,
                        status=status,
                        parent_event_id=parent_evt,
                    )
                self._total_tool_calls += 1
                all_tool_calls.append({"name": name, "args": args, "result": tool_result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "name": name,
                    "content": json.dumps(tool_result, default=str)
                               if not isinstance(tool_result, str) else tool_result,
                })
            # Loop: model gets to see tool results and either call more tools or produce final text.

        # 4. Apply the identity lock to whatever the model produced
        if self.config.enforce_identity_lock and final_text:
            final_text, slips = identity.enforce_identity(
                final_text,
                known_model=self.config.backend.default_model,
                passthrough=self.config.enforce_identity_audit_only,
            )
            if slips:
                # Log every slip so the observability substrate can measure
                # identity-lock leak rate per model / per run.
                self._log(
                    "identity_slip_detected",
                    status="error" if not self.config.enforce_identity_audit_only else "ok",
                    metadata={"slips": slips, "count": len(slips),
                              "audit_only": self.config.enforce_identity_audit_only},
                    parent_event_id=parent_evt,
                )

        # 5. Persist a salient memory for the turn (warm tier)
        if final_text:
            self.memory.write(
                content=f"Q: {user_message}\nA: {final_text[:500]}",
                source="conversation",
                kind="turn",
                tier=mm.L2_WARM,
            )
            self._total_memory_writes += 1

            # 5a. Rule-intent detection (v0.9.6). When the user issues
            # a behavioral constraint ("stop using exclamation marks",
            # "never push directly to main", "always sign off with
            # 'cheers'"), persist it as a high-priority `rule` memory
            # so `_build_rules_block()` can inject it into every future
            # system prompt. Kept in addition to the `turn` row above
            # so normal retrieval still surfaces the conversation
            # context; the `rule` row is the strict-rules fast path.
            if _looks_like_rule(user_message):
                self.memory.write(
                    content=user_message.strip(),
                    source="conversation",
                    kind="rule",
                    tier=mm.L2_WARM,
                    metadata={"detected_by": "rule_intent_regex"},
                )
                self._total_memory_writes += 1

        # 5b. Training capture — full verbatim turn for mnemosyne_train.py
        # to reconstruct Hermes-compatible ShareGPT trajectories later.
        # Off by default; enabled via BrainConfig.capture_for_training.
        if final_text and self.config.capture_for_training:
            system_prompt = "\n\n".join(system_parts) if system_parts else ""
            self._log(
                "training_turn",
                parent_event_id=parent_evt,
                metadata={
                    "system_prompt": system_prompt,
                    "user_message": user_message,
                    "assistant_text": final_text,
                    "tool_calls": all_tool_calls,
                    "model": self.config.backend.default_model,
                    "provider": self.config.backend.provider,
                },
            )

        return BrainResponse(
            text=final_text,
            tool_calls=all_tool_calls,
            memory_reads=memory_reads,
            memory_writes=1 if final_text else 0,
            model_calls=model_calls,
        )

    # ---- inner dialogue path ------------------------------------------------

    def _run_turn_inner(self, user_message: str, parent_evt: str | None) -> BrainResponse:
        """Planner → Critic → Doer pass. Shares memory + identity with
        the tool-use loop, skips tool dispatch (inner dialogue is for
        reasoning-heavy turns, not tool-heavy ones)."""
        # Shared context: memory hits + optional user docs, computed once.
        hits = self.memory.search(
            user_message,
            limit=self.config.memory_retrieval_limit,
            tier_max=self.config.memory_tier_ceiling,
        )
        self._total_memory_reads += 1
        shared_context = ""
        if hits:
            shared_context = "## Relevant memories\n\n" + "\n".join(
                f"- [L{h['tier']} {h.get('kind','')}] {h['content']}" for h in hits
            )

        identity_preamble = (
            identity.MNEMOSYNE_IDENTITY.strip()
            if self.config.enforce_identity_lock else None
        )

        result = inner_mod.deliberate(
            user_message=user_message,
            chat_fn=self._chat_fn,
            backend=self.config.backend,
            identity_preamble=identity_preamble,
            personality=self.config.personality,
            shared_context=shared_context,
            telemetry=self.telemetry,
            enable_evaluator=self.config.inner_dialogue_evaluator,
        )
        self._total_model_calls += result.total_model_calls

        final_text = result.answer or ""
        # Apply a final identity pass (deliberate already filters each
        # persona but the final concatenation can still leak).
        if self.config.enforce_identity_lock and final_text:
            final_text, slips = identity.enforce_identity(
                final_text,
                known_model=self.config.backend.default_model,
                passthrough=self.config.enforce_identity_audit_only,
            )
            if slips:
                self._log(
                    "identity_slip_detected",
                    status="error" if not self.config.enforce_identity_audit_only else "ok",
                    metadata={"slips": slips, "count": len(slips),
                              "audit_only": self.config.enforce_identity_audit_only,
                              "path": "inner_dialogue"},
                    parent_event_id=parent_evt,
                )

        if final_text:
            self.memory.write(
                content=f"Q: {user_message}\nA: {final_text[:500]}",
                source="conversation",
                kind="turn",
                tier=mm.L2_WARM,
                metadata={"path": "inner_dialogue"},
            )
            self._total_memory_writes += 1

        # Training capture (inner-dialogue path)
        if final_text and self.config.capture_for_training:
            self._log(
                "training_turn",
                parent_event_id=parent_evt,
                metadata={
                    "system_prompt": shared_context,
                    "user_message": user_message,
                    "assistant_text": final_text,
                    "tool_calls": [],
                    "model": self.config.backend.default_model,
                    "provider": self.config.backend.provider,
                    "path": "inner_dialogue",
                    "personas": ["planner", "critic", "doer"]
                                + (["evaluator"] if result.evaluator else []),
                },
            )

        return BrainResponse(
            text=final_text,
            tool_calls=[],
            memory_reads=1,
            memory_writes=1 if final_text else 0,
            model_calls=result.total_model_calls,
        )

    # ---- dreams -------------------------------------------------------------

    def _check_permissions(self, skill_name: str) -> tuple[bool, str]:
        """Consult permissions.md + rate limits. Returns (ok, reason).

        Safe no-op (allows everything) when enforce_permissions=False
        or when permissions.md is absent.
        """
        if not self.config.enforce_permissions:
            return True, ""
        # Load lazily + cache per-Brain so we don't re-parse every turn
        perms = getattr(self, "_cached_permissions", None)
        if perms is None:
            try:
                import mnemosyne_permissions as perm_mod
                perms = perm_mod.load()
                self._cached_permissions = perms
                self._permissions_rate = perm_mod._RollingRateLimiter()
            except Exception:
                return True, ""
        ok, reason = perms.is_skill_allowed(skill_name)
        if not ok:
            return False, reason
        # Rate-limit check
        rate = perms.rate_limits.get(skill_name)
        if rate is not None:
            count, window = rate
            rl = getattr(self, "_permissions_rate", None)
            if rl is None:
                import mnemosyne_permissions as perm_mod
                rl = perm_mod._RollingRateLimiter()
                self._permissions_rate = rl
            ok, reason = rl.check(skill_name, count, window)
            if not ok:
                return False, reason
        return True, ""

    def _apply_avatar_feedback(self, *, parent_evt: str | None) -> None:
        """Read current avatar state and let its rules mutate self.config.

        Each adjustment logs an `avatar_feedback` telemetry event so the
        observability substrate sees feedback as a first-class action.
        Safe no-op if mnemosyne_avatar isn't importable.
        """
        try:
            import mnemosyne_avatar as av_mod
        except ImportError:  # pragma: no cover
            return
        try:
            # use_cache=True is fine — the polling dashboard and this
            # call share the same cache, so feedback reads come free.
            state = av_mod.compute_state(use_cache=True)
        except Exception:
            return
        adjustments = av_mod.apply_feedback(state, self.config)
        for adj in adjustments:
            self._log(
                "avatar_feedback",
                parent_event_id=parent_evt,
                metadata=adj.to_dict(),
            )

    def _maybe_dream(self) -> None:
        """Fire a dream-consolidation pass if L3 has enough material.

        Cheap guard: skip if fewer than `dreams_min_memories` L3 rows
        exist. Uses the brain's own model as the summarizer when
        available — falls back to stdlib otherwise.
        """
        try:
            import mnemosyne_dreams as dreams_mod
        except ImportError:
            return
        try:
            # Count L3 memories cheaply
            with self.memory._lock:  # type: ignore[attr-defined]
                n = self.memory._conn.execute(  # type: ignore[attr-defined]
                    "SELECT COUNT(*) FROM memories WHERE tier = ?",
                    (mm.L3_COLD,),
                ).fetchone()[0]
        except Exception:
            return
        if n < self.config.dreams_min_memories:
            return

        summarizer = dreams_mod.make_brain_summarizer(self)
        dreams_mod.consolidate(
            memory=self.memory,
            summarizer_fn=summarizer,
            telemetry=self.telemetry,
        )

    # ---- helpers ------------------------------------------------------------

    def _maybe_adapt_to_context(self) -> None:
        """Probe the configured Ollama model and cap memory_retrieval_limit
        to fit its context window. No-op for non-local backends."""
        backend = self.config.backend
        if backend.provider != "ollama":
            return
        try:
            host = backend.url.rsplit("/api/", 1)[0] if backend.url else None
            info = models.ollama_model_info(backend.default_model, host=host, timeout=2.0)
        except Exception:
            return
        if info.get("error"):
            return  # model not pulled / probe failed — keep default
        ctx = info.get("context_length")
        if not ctx:
            return
        budget = models.recommended_context_budget(ctx)
        if budget < self.config.memory_retrieval_limit:
            self.config.memory_retrieval_limit = budget
            self._log(
                "context_adapted",
                metadata={
                    "model": backend.default_model,
                    "context_length": ctx,
                    "new_retrieval_limit": budget,
                },
            )
        self._context_adapted = True

    def _tool_output_dir(self) -> Path:
        """Return the directory where oversized tool results are persisted
        by the v0.9.2 budgeter. Lazily resolved off the projects dir."""
        try:
            from mnemosyne_config import default_projects_dir
            base = default_projects_dir()
        except Exception:
            import os as _os
            raw = _os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
            base = (Path(raw).expanduser().resolve() if raw
                    else Path.home() / "projects" / "mnemosyne")
        return base / "tool-outputs"

    def _build_env_snapshot(self) -> str:
        """Call environment-snapshot for the first-turn preamble. Safe if missing."""
        try:
            import environment_snapshot as es
            return es.format_markdown(es.build_snapshot())
        except Exception:
            return ""

    def _build_rules_block(self, *, limit: int = 20) -> str:
        """Pull user-asserted rules (kind='rule' memories) and inject
        them into the system prompt as STRICT compliance requirements.

        Rules are harder-than-instinct: when the user says "stop using
        exclamation marks," that's a MUST, not a preference. This
        block renders them with imperative framing so the model reads
        them as hard constraints rather than soft style guidance.

        Populated automatically by `_looks_like_rule()` on write when
        `Brain.turn()` detects an imperative user message. Callers can
        also write rules directly with `kind='rule'` for explicit
        control. Silent no-op when no rules exist.
        """
        try:
            with self.memory._lock:  # noqa: SLF001
                rows = self.memory._conn.execute(  # noqa: SLF001
                    "SELECT content FROM memories "
                    "WHERE kind = 'rule' "
                    "ORDER BY last_accessed_utc DESC NULLS LAST, "
                    "         strength DESC, created_utc DESC "
                    "LIMIT ?",
                    (limit,),
                ).fetchall()
        except Exception:
            return ""
        if not rows:
            return ""
        lines = [f"- {r['content']}" for r in rows]
        return (
            "## STRICT RULES — you MUST obey every item below on every "
            "response. Violating a rule makes the response invalid.\n\n"
            + "\n".join(lines)
        )

    def _build_instinct_block(self, *, limit: int = 20) -> str:
        """Pull user-instinct rows (kind=user_instinct, populated by
        mnemosyne_instinct.distill) and inject as a fast-path system
        block on every turn.

        These are distilled user-interaction patterns — preferred
        response style, recurring topics, tool affinities — that the
        agent should react to automatically rather than rediscovering
        each session. Silent no-op if no instincts have been distilled
        yet or the query fails.
        """
        try:
            with self.memory._lock:  # noqa: SLF001
                rows = self.memory._conn.execute(  # noqa: SLF001
                    "SELECT content FROM memories "
                    "WHERE kind = 'user_instinct' "
                    "ORDER BY strength DESC, last_accessed_utc DESC "
                    "LIMIT ?",
                    (limit,),
                ).fetchall()
        except Exception:
            return ""
        if not rows:
            return ""
        lines = [f"- {r['content']}" for r in rows]
        return (
            "## Learned user instincts (distilled patterns; "
            "react automatically)\n\n"
            + "\n".join(lines)
        )

    def _build_l5_identity_block(self, *, limit: int = 20) -> str:
        """Pull L5 identity memories as a persistent system-prompt block.

        L5 rows are human-approved core values (the fifth ICMS tier
        introduced in v0.7). Unlike L1/L2 retrieval which is
        query-relevance filtered, L5 is injected on every turn so the
        agent carries its values across sessions. Silent no-op if the
        store has no L5 rows or the query fails.
        """
        try:
            with self.memory._lock:  # noqa: SLF001
                rows = self.memory._conn.execute(  # noqa: SLF001
                    "SELECT content, kind FROM memories "
                    "WHERE tier = ? "
                    "ORDER BY strength DESC, last_accessed_utc DESC "
                    "LIMIT ?",
                    (mm.L5_IDENTITY, limit),
                ).fetchall()
        except Exception:
            return ""
        if not rows:
            return ""
        lines = [f"- [{r['kind']}] {r['content']}" for r in rows]
        return (
            "## Core values (human-approved, persistent across sessions)\n\n"
            + "\n".join(lines)
        )

    def _read_user_docs(self) -> list[tuple[str, str]]:
        """Read user-editable AGENTS.md and TOOLS.md from the workspace.

        OpenClaw-style pattern: the user maintains markdown files in
        $PROJECTS_DIR/ that get injected into the brain's first-turn system
        prompt. Two files are consumed:

          AGENTS.md  — operating instructions, personality hints, constraints
          TOOLS.md   — user notes about tools, preferences for which tool to
                       use when, known pitfalls

        Both are optional. Missing files are silently skipped.
        Returns a list of (title, content) pairs.
        """
        try:
            from mnemosyne_config import default_projects_dir
            pd = default_projects_dir()
        except Exception:
            return []
        docs: list[tuple[str, str]] = []
        for filename, title in (("AGENTS.md", "User instructions (AGENTS.md)"),
                                ("TOOLS.md", "User tool notes (TOOLS.md)")):
            p = pd / filename
            if p.is_file():
                try:
                    body = p.read_text(encoding="utf-8", errors="replace").strip()
                    if body:
                        docs.append((title, body))
                except OSError:
                    pass
        return docs

    def _log(
        self,
        event_type: str,
        *,
        tool: str | None = None,
        args: Any = None,
        result: Any = None,
        duration_ms: float | None = None,
        status: str = "ok",
        error: dict[str, Any] | None = None,
        parent_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        if self.telemetry is None:
            return None
        try:
            return self.telemetry.log(
                event_type,
                tool=tool,
                args=args,
                result=result,
                duration_ms=duration_ms,
                status=status,
                error=error,
                parent_event_id=parent_event_id,
                metadata=metadata,
            )
        except Exception:
            return None

    def session_metrics(self) -> dict[str, Any]:
        """Metrics suitable for harness_telemetry.finalize_run."""
        avg_turn = (self._total_turn_ms / self._total_turns) if self._total_turns else 0.0
        return {
            "turns_total": self._total_turns,
            "turns_successful": self._turns_successful,
            "turns_failed": self._turns_failed,
            "model_calls_total": self._total_model_calls,
            "tool_calls_total": self._total_tool_calls,
            "memory_reads_total": self._total_memory_reads,
            "memory_writes_total": self._total_memory_writes,
            "latency_ms_avg": avg_turn,
            "latency_ms_total": self._total_turn_ms,
            "accuracy": (self._turns_successful / self._total_turns)
                        if self._total_turns else 0.0,
        }

    # ---- self-improvement hook ---------------------------------------------

    def learn_skill(
        self,
        name: str,
        description: str,
        command: str,
        *,
        parameters: list[dict[str, Any]] | None = None,
        notes: str = "",
    ) -> Path:
        """Write a new skill file so it's available in future runs.

        Mirrors Hermes's "agent writes a markdown skill after solving a task"
        self-improvement pattern, but also logs a telemetry event so the
        observability substrate can later verify whether learned skills
        actually improved the Pareto frontier.
        """
        path = skills_mod.record_learned_skill(
            name=name,
            description=description,
            command=command,
            parameters=parameters,
            notes=notes,
            telemetry=self.telemetry,
        )
        # Reload so the new skill is immediately usable this session
        self.skills.load_directory(path.parent)
        return path
