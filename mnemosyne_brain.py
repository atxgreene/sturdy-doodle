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


ChatFn = Callable[..., dict[str, Any]]


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

        try:
            if self.consciousness and hasattr(self.consciousness, "pre_turn"):
                try:
                    self.consciousness.pre_turn(user_message)
                except Exception:
                    pass  # consciousness must never break the turn

            response = self._run_turn(user_message, turn_evt)

            if self.consciousness and hasattr(self.consciousness, "post_turn"):
                try:
                    self.consciousness.post_turn(user_message, response.text)
                except Exception:
                    pass

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
                    t0 = time.monotonic()
                    try:
                        tool_result = skill.invoke(**(args if isinstance(args, dict) else {}))
                        status = "ok"
                    except Exception as e:
                        tool_result = {"error": f"{type(e).__name__}: {e}"}
                        status = "error"
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

        return BrainResponse(
            text=final_text,
            tool_calls=all_tool_calls,
            memory_reads=memory_reads,
            memory_writes=1 if final_text else 0,
            model_calls=model_calls,
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

    def _build_env_snapshot(self) -> str:
        """Call environment-snapshot for the first-turn preamble. Safe if missing."""
        try:
            import environment_snapshot as es
            return es.format_markdown(es.build_snapshot())
        except Exception:
            return ""

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
