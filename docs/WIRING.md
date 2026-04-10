# Wiring `harness_telemetry` into `eternal-context`

This document shows concrete patterns for plugging the Mnemosyne observability layer (`harness_telemetry.py`) into the `eternal-context` base agent so that every tool call the agent makes lands in an `events.jsonl` file inside the current experiment run.

**This doc does NOT speculate about the actual `eternal-context` skill/tool registration interface.** I haven't seen a real skill file from that repo as of the time of writing. What's here is four *shapes* the wiring could take, with complete working code for each. You pick whichever one matches the pattern your skills actually use and adapt it; the change is ~20 lines either way.

---

## Preamble: what you're wiring

`harness_telemetry` provides three pieces the wiring needs:

```python
import harness_telemetry as ht

# 1. A run directory, created once per agent session
run_id = ht.create_run(
    model="gemma4:e4b",
    tags=["live", "prod"],
    notes="daily assistant, 2026-04-09",
    freeze_files=[
        "/path/to/sturdy-doodle/install-mnemosyne.sh",
        "/path/to/sturdy-doodle/mnemosyne-wizard.sh",
    ],
)

# 2. A session that writes events to that run's events.jsonl
session = ht.TelemetrySession(run_id)

# 3. A decorator for instrumenting callables
@session.trace
def some_tool(*args, **kwargs): ...
```

When the agent session ends (normal exit or exception), call:

```python
ht.finalize_run(run_id, metrics={
    "accuracy": computed_accuracy,
    "latency_ms_avg": computed_average_latency,
    "turns_successful": n_ok,
    "turns_failed": n_err,
})
```

The four patterns below all boil down to "get a `session`, decorate the tool dispatch point." They differ only in **where** the decoration lives.

---

## Pattern A — Decorator on each tool function

**When to use:** if skills are defined as top-level Python functions registered via a decorator like `@register_tool` or a module-level list.

```python
# In eternal-context/skills/eternal-context/tools.py (or wherever tools live)

from eternalcontext.tools import register_tool  # hypothetical
import harness_telemetry as ht

# Session is created once per agent boot — see Pattern D for how.
_session: ht.TelemetrySession | None = None

def set_telemetry_session(session: ht.TelemetrySession) -> None:
    global _session
    _session = session

def _maybe_trace(fn):
    """Apply @session.trace only if telemetry is on; otherwise return fn unchanged."""
    if _session is None:
        return fn
    return _session.trace(fn)

@register_tool
@_maybe_trace
def obsidian_search(query: str, limit: int = 10) -> dict:
    # ... existing implementation ...
    return {"matches": [...]}

@register_tool
@_maybe_trace
def notion_search(query: str) -> dict:
    # ... existing implementation ...
    return {"results": [...]}
```

**Pros:** explicit, easy to reason about, works even if the tool registry is just a list comprehension.
**Cons:** you touch every tool file. If you have 11 tools, that's 11 places to add the `@_maybe_trace`.

---

## Pattern B — Central registry wrapper

**When to use:** if `eternal-context` has a central place that knows about all tools (a registry class, a `TOOLS = [...]` dict, a plugin loader).

```python
# In whatever file loads the tool registry

import harness_telemetry as ht
from eternalcontext.registry import Registry  # hypothetical

def install_telemetry(registry: Registry, session: ht.TelemetrySession) -> None:
    """Wrap every tool in the registry with the telemetry trace decorator."""
    for tool_name, tool_fn in list(registry.items()):
        registry[tool_name] = session.trace(tool_fn)
```

**Pros:** one-call install, no per-tool changes, reversible (you can toggle telemetry on/off for debugging without touching tool code).
**Cons:** requires that the registry is mutable and exposes `items()` / `__setitem__` or an equivalent.

---

## Pattern C — Middleware around the dispatch function

**When to use:** if `eternal-context` has a single `dispatch(tool_name, args)` choke-point — the function that actually runs whichever tool the LLM asked for.

```python
# In eternal-context/skills/eternal-context/__init__.py or dispatcher module

import harness_telemetry as ht
import time
import traceback
from functools import wraps

def telemetry_middleware(dispatch_fn, session: ht.TelemetrySession):
    """Wrap a dispatch function so every call is logged as a tool_call event."""
    @wraps(dispatch_fn)
    def wrapper(tool_name, args, **kwargs):
        start = time.monotonic()
        try:
            result = dispatch_fn(tool_name, args, **kwargs)
        except Exception as exc:
            session.log(
                "tool_call",
                tool=tool_name,
                args={"tool_args": args, "kwargs": kwargs},
                result=None,
                duration_ms=(time.monotonic() - start) * 1000.0,
                status="error",
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
            raise
        session.log(
            "tool_call",
            tool=tool_name,
            args={"tool_args": args, "kwargs": kwargs},
            result={"value": result},
            duration_ms=(time.monotonic() - start) * 1000.0,
            status="ok",
        )
        return result
    return wrapper

# Install once at agent boot:
from eternalcontext.dispatcher import dispatch  # hypothetical
instrumented_dispatch = telemetry_middleware(dispatch, session)
# ... replace `dispatch` wherever it's called ...
```

**Pros:** exactly one wrap point, works for any tool the agent ever adds, captures tool calls that don't go through the Python-level tool registry (e.g. shell-outs, RPC).
**Cons:** requires a single dispatch function. If your agent has multiple entry points (direct LLM tool calls + MCP + manual invocations), each needs its own middleware wrap.

This is my recommended pattern if `eternal-context` has a dispatcher. It matches how observability is typically done in large Python agent frameworks (Zipkin middleware, OpenTelemetry span decorators).

---

## Pattern D — Where to create the session

All three patterns above assume a `session` object already exists. Here's where to create it in each common entry-point shape.

### D.1 — CLI REPL (`python -m eternalcontext`)

```python
# In eternal-context/skills/eternal-context/__main__.py

import harness_telemetry as ht
from eternalcontext.repl import run_repl  # hypothetical

def main():
    run_id = ht.create_run(
        model="gemma4:e4b",
        tags=["repl", "interactive"],
        notes="interactive session started from CLI",
    )
    session = ht.TelemetrySession(run_id)
    turns_ok = 0
    turns_err = 0
    try:
        with session:
            # wire the session in via one of the patterns above, then:
            turns_ok, turns_err = run_repl(session=session)
    except KeyboardInterrupt:
        ht.mark_run_failed(run_id, error="KeyboardInterrupt")
        raise
    else:
        ht.finalize_run(run_id, metrics={
            "turns_successful": turns_ok,
            "turns_failed": turns_err,
            # accuracy/latency metrics can be added if you compute them
        })
```

### D.2 — Multi-channel server (`python -m eternalcontext.server`)

One run per server boot, or one run per *conversation*:

```python
# Per-boot (simpler): events from all channels/users land in one run
run_id = ht.create_run(model="...", tags=["server", "all-channels"])
global_session = ht.TelemetrySession(run_id)
# ... wire into the dispatcher, never finalize until server shutdown ...

# Per-conversation (richer): one run per Telegram/Slack chat thread
def on_new_conversation(chat_id: str):
    run_id = ht.create_run(
        model="...",
        tags=["conversation", f"chat-{chat_id}"],
        slug=f"chat{chat_id}",
    )
    session = ht.TelemetrySession(run_id)
    return session  # store it in the conversation state

def on_conversation_end(chat_id: str, session: ht.TelemetrySession, metrics: dict):
    ht.finalize_run(session.run_id, metrics=metrics)
```

Per-conversation is more work but lets you use `mnemosyne-experiments diff` to compare how the harness behaved across two users, or top-k to find the best-performing channel/hour.

### D.3 — The ConsciousnessLoop (mnemosyne-consciousness)

If you want to instrument the consciousness layer as well (and you should), create the session at the loop level and pass it into the base harness:

```python
# Hypothetical mnemosyne.ConsciousnessLoop extension

from mnemosyne import ConsciousnessLoop  # hypothetical
import harness_telemetry as ht

class InstrumentedConsciousnessLoop(ConsciousnessLoop):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_id = ht.create_run(
            model=self.model_name,
            tags=["consciousness", "live"],
            notes=f"ConsciousnessLoop booted at {self.start_time}",
        )
        self.session = ht.TelemetrySession(self.run_id)

    def turn(self, user_message: str):
        self.session.log("prompt", args={"message": user_message})
        response = super().turn(user_message)
        self.session.log("response", result={"text": response})
        return response

    def shutdown(self):
        ht.finalize_run(self.run_id, metrics=self.collected_metrics())
```

This gives you the consciousness-layer events (dream consolidation, autobiography updates, metacognition pulses) in the same `events.jsonl` as the base harness tool calls, so the whole stack is observable from one file.

---

## Preflight: inject an environment snapshot into the first turn

The Meta-Harness paper's Terminal-Bench 2 result suggests starting the agent with a pre-computed environment snapshot instead of letting it discover via tool calls. Use `environment-snapshot.py` for this:

```python
import subprocess
import json
from pathlib import Path

def get_environment_preamble() -> str:
    helper = Path(__file__).parent / "environment-snapshot.py"  # adjust path
    result = subprocess.run(
        ["python3", str(helper)],  # default = markdown
        capture_output=True, text=True, check=True,
    )
    return result.stdout

def build_system_prompt(base_prompt: str) -> str:
    return (
        base_prompt
        + "\n\n## Current environment\n\n"
        + get_environment_preamble()
    )
```

Pass `build_system_prompt(...)` as the first turn's system message. The agent gets the environment for free and stops wasting turns discovering what's already known.

---

## Testing your wiring

After wiring, verify with the stack's own tools — no real workload needed:

```bash
# Boot your agent for one turn (however you normally do that)
python -m eternalcontext

# Check that a run was created
./mnemosyne-experiments.py list | head -3

# Inspect the most recent run's events
./mnemosyne-experiments.py events "$(./mnemosyne-experiments.py list --limit 1 | awk '{print $1}')" --event-type tool_call

# Get per-tool stats
./mnemosyne-experiments.py aggregate "$(./mnemosyne-experiments.py list --limit 1 | awk '{print $1}')"
```

If events.jsonl has `tool_call` entries with your tool names, the wiring is working.

---

## Removing telemetry later

Telemetry is opt-in. To remove it:

- **Pattern A:** remove the `@_maybe_trace` decorators.
- **Pattern B:** don't call `install_telemetry(...)`.
- **Pattern C:** use the original dispatch function instead of `instrumented_dispatch`.
- **Pattern D:** stop creating sessions at boot; the tools still work unchanged.

There are no runtime side effects from loading `harness_telemetry` — it doesn't fork threads, doesn't register signal handlers, doesn't touch the filesystem until you call `create_run`. Importing it is safe.

---

## Security notes for production wiring

- `TelemetrySession.log` redacts values at keys matching token/secret/api_key/password/bearer/credential/signing_key by default. If your tool arguments contain secrets under *other* key names, extend `redact_patterns` when constructing the session.
- Event logs contain raw tool arguments and results. If those include user PII, treat `$PROJECTS_DIR/experiments/` as a user-data surface and apply the same retention / encryption you'd apply to any user-facing log store.
- The `harness/` snapshot directory contains a frozen copy of whatever files you pass to `freeze_files`. Don't freeze files that themselves contain secrets (e.g., a `.env` file).
- Running `test-harness.sh` and `python3 tests/test_all.py` after wiring verifies that the observability layer itself is still clean.
