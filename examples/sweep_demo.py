#!/usr/bin/env python3
"""
sweep_demo.py — end-to-end Mnemosyne observability demo.

Demonstrates the full flow without needing a real LLM or network:

  1. harness_sweep.run(...) iterates over a parameter space.
  2. For each combination, a fresh telemetry run is created.
  3. scenario_runner.run_scenarios(...) evaluates a fake harness against
     scenarios.example.jsonl.
  4. The fake harness returns deterministic-but-variable outputs driven
     by the sweep parameters, so different combinations produce
     different accuracy/latency numbers.
  5. Every run is finalized with metrics.
  6. After the sweep, mnemosyne-experiments CLI commands are suggested
     for inspecting the results.

Run:
    python3 examples/sweep_demo.py
    python3 examples/sweep_demo.py --projects-dir /tmp/demo-experiments

The demo uses a fake harness so it has zero external dependencies. Real
usage would replace `fake_harness` with a call into your actual agent.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# After `pip install -e .` these are plain imports. Fallback for an
# un-installed clone keeps the demo standalone.
try:
    import harness_sweep as sweep
    import harness_telemetry as ht
    import scenario_runner as sr
except ImportError:
    sys.path.insert(0, str(_HERE.parent))
    import harness_sweep as sweep  # noqa: E402
    import harness_telemetry as ht  # noqa: E402
    import scenario_runner as sr  # noqa: E402

SCENARIOS_FILE = _HERE.parent / "scenarios.example.jsonl"


def make_fake_harness(params: dict):
    """Build a fake harness that responds to prompts in a way that depends
    on the sweep parameters, so different param combinations give
    different accuracy and latency numbers."""

    # Parameters that influence the fake agent's behavior
    model = params.get("model", "qwen3:8b")
    retrieval_limit = int(params.get("retrieval_limit", 10))
    temperature = float(params.get("temperature", 0.0))

    # Rough capability model: bigger-context models are "smarter" at
    # long-context and tool-use scenarios; higher temperature reduces
    # accuracy; low retrieval_limit hurts tool-use scenarios.
    is_long_context = "gemma4" in model
    base_accuracy = 0.88 if is_long_context else 0.80
    base_latency = 900.0 if is_long_context else 1300.0

    rng = random.Random(f"{model}-{retrieval_limit}-{temperature}")

    def harness(prompt: str, session: ht.TelemetrySession) -> dict:
        # Simulate some tool-use for retrieval-y prompts
        tool_calls: list[str] = []

        @session.trace
        def obsidian_search(query: str, limit: int = retrieval_limit) -> dict:
            # simulate a ~50ms search
            time.sleep(0.01 + rng.random() * 0.02)
            return {"matches": [{"path": f"note-{i}.md"} for i in range(limit)]}

        @session.trace
        def notion_search(query: str) -> dict:
            time.sleep(0.02 + rng.random() * 0.02)
            return {"results": []}

        prompt_lower = prompt.lower()
        # Simulate tool dispatch
        if "obsidian" in prompt_lower or "vault" in prompt_lower:
            obsidian_search(prompt)
            tool_calls.append("obsidian_search")
        if "notion" in prompt_lower or "quarterly" in prompt_lower:
            notion_search(prompt)
            tool_calls.append("notion_search")
        if "both" in prompt_lower or "summarize the mnemosyne" in prompt_lower:
            obsidian_search("mnemosyne")
            notion_search("mnemosyne")
            tool_calls = ["obsidian_search", "notion_search"]

        # Simulate reasoning latency
        think_ms = base_latency * (1.0 + temperature * 0.3)
        time.sleep(think_ms / 1000.0 * 0.05)  # scaled way down so the demo runs in seconds

        # Canned responses that usually satisfy the example scenarios
        text_parts = []
        if "capital of france" in prompt_lower:
            text_parts.append("Paris is the capital of France.")
        if "17 + 28" in prompt_lower:
            text_parts.append("The answer is 45.")
        if "email" in prompt_lower:
            text_parts.append("demo@example.org")
        if "delete all" in prompt_lower:
            text_parts.append("I cannot perform destructive actions.")
        if "fibonacci" in prompt_lower:
            text_parts.append(
                "def fib(n: int) -> int:\n    return n if n < 2 else fib(n-1) + fib(n-2)"
            )
        if "day of the week" in prompt_lower:
            text_parts.append("That will be a Friday.")
        if "mnemosyne" in prompt_lower:
            text_parts.append(
                "Mnemosyne is a local-first LLM agent with ICMS memory."
            )

        # Inject noise: higher temperature -> more likely to drop correct answer
        if rng.random() < temperature * 0.4 and text_parts:
            text_parts.pop()

        # For long-context scenarios, short-context models sometimes miss
        if not is_long_context and "first three things" in prompt_lower:
            text_parts = ["(context dropped due to 32K limit)"]

        return {
            "text": " ".join(text_parts) or "I'm not sure.",
            "tool_calls": tool_calls,
        }

    return harness


def evaluate(params: dict, session: ht.TelemetrySession) -> dict:
    """Run the full scenario set through the fake harness and return metrics."""
    scenarios = sr.load_scenarios(SCENARIOS_FILE)
    harness = make_fake_harness(params)
    result = sr.run_scenarios(scenarios=scenarios, harness=harness, session=session)

    # Log a summary event so the metrics are visible in the event stream
    session.log(
        "scenario_summary",
        result=result["metrics"],
        metadata={
            "passed": result["metrics"]["passed"],
            "failed": result["metrics"]["failed"],
        },
    )
    return result["metrics"]


def main() -> int:
    p = argparse.ArgumentParser(
        description="End-to-end Mnemosyne observability demo. Runs a 2x2 "
                    "parameter sweep against a fake harness and the "
                    "scenarios.example.jsonl scenario set. Produces a set "
                    "of experiment runs you can inspect with the "
                    "mnemosyne-experiments CLI.",
    )
    p.add_argument("--projects-dir",
                   help="override $MNEMOSYNE_PROJECTS_DIR for this demo")
    args = p.parse_args()

    parameter_space = {
        "model": ["qwen3:8b", "gemma4:e4b"],
        "retrieval_limit": [5, 15],
        "temperature": [0.0, 0.5],
    }

    print("Starting demo sweep...", file=sys.stderr)
    run_ids = sweep.run(
        parameter_space=parameter_space,
        evaluator=evaluate,
        projects_dir=args.projects_dir,
        tags=["demo", "example"],
        notes="sweep_demo.py — fake harness, not real metrics",
    )

    print()
    print(f"Demo sweep finished: {len(run_ids)} runs created.")
    print()
    if args.projects_dir:
        env_hint = f"MNEMOSYNE_PROJECTS_DIR={args.projects_dir} "
    else:
        env_hint = ""
    print("Inspect the results:")
    print()
    print(f"  {env_hint}./mnemosyne-experiments.py list")
    print(f"  {env_hint}./mnemosyne-experiments.py top-k 3 --metric accuracy")
    print(f"  {env_hint}./mnemosyne-experiments.py top-k 3 --metric latency_ms_avg --direction min")
    print(f"  {env_hint}./mnemosyne-experiments.py pareto \\")
    print(f"      --axes accuracy,latency_ms_avg --directions max,min --plot")
    print(f"  {env_hint}./mnemosyne-experiments.py aggregate {run_ids[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
