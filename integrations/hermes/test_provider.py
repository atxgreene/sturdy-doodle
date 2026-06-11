#!/usr/bin/env python3
"""Standalone smoke test for MnemosyneMemoryProvider.

No Hermes install required. Requires MNEMOSYNE_PATH to be set.

Usage:
    MNEMOSYNE_PATH=/path/to/Mnemosyne python3 test_provider.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from __init__ import MnemosyneMemoryProvider  # noqa: E402


def main() -> int:
    p = MnemosyneMemoryProvider()
    if not p.is_available():
        print("SKIP: Mnemosyne not found. Set MNEMOSYNE_PATH.")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        p.initialize("test-session-001", hermes_home=tmp)

        # --- system prompt block -----------------------------------------------
        block = p.system_prompt_block()
        assert "memory_search" in block, "system_prompt_block missing memory_search"
        assert "ICMS" in block, "system_prompt_block missing ICMS description"
        print("OK  system_prompt_block")

        # --- tool schemas --------------------------------------------------------
        schemas = p.get_tool_schemas()
        names = {s.get("name") or s.get("function", {}).get("name") for s in schemas}
        assert names == {"memory_search", "memory_write", "memory_stats"}, \
            f"unexpected tools: {names}"
        print("OK  get_tool_schemas")

        # --- tool: memory_write --------------------------------------------------
        result = p.handle_tool_call("memory_write",
                                    {"content": "Austin prefers Python for agent code.",
                                     "kind": "preference", "tier": 3})
        assert "stored" in result.lower(), f"unexpected write result: {result}"
        p.handle_tool_call("memory_write",
                           {"content": "BEN runs on Dell Latitude 7420 with Kali.",
                            "kind": "fact", "tier": 2})
        p.handle_tool_call("memory_write",
                           {"content": "Long-term goal: portable agent with device handoff.",
                            "kind": "goal", "tier": 4})
        print("OK  memory_write ×3")

        # --- sync_turn (async) ---------------------------------------------------
        p.sync_turn("What is my preferred language?", "You prefer Python.",
                    session_id="test-session-001")
        time.sleep(0.2)  # let worker thread flush
        print("OK  sync_turn")

        # --- prefetch / queue_prefetch -------------------------------------------
        p.queue_prefetch("preferred programming language")
        time.sleep(0.2)
        context = p.prefetch("preferred programming language")
        print(f"OK  prefetch (got {len(context)} chars)")

        # --- tool: memory_search -------------------------------------------------
        result = p.handle_tool_call("memory_search",
                                    {"query": "Python agent code", "limit": 5})
        assert "Python" in result, f"search did not find planted memory: {result}"
        print("OK  memory_search")

        # --- tool: memory_stats --------------------------------------------------
        stats = p.handle_tool_call("memory_stats", {})
        assert "total" in stats.lower() or "tier" in stats.lower(), \
            f"unexpected stats: {stats}"
        print("OK  memory_stats")

        # --- on_session_end ------------------------------------------------------
        messages = [
            {"role": "user", "content": "Tell me about BCE heat kernels."},
            {"role": "assistant", "content": "Seeley-DeWitt coefficients describe boundary QED effects."},
        ]
        p.on_session_end(messages)
        time.sleep(0.3)
        print("OK  on_session_end")

        # --- on_pre_compress -----------------------------------------------------
        summary = p.on_pre_compress(messages)
        print(f"OK  on_pre_compress (summary: {summary[:80]!r}{'...' if len(summary) > 80 else ''})")

        # --- shutdown ------------------------------------------------------------
        p.shutdown()
        print("OK  shutdown")

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
