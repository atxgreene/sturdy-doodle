#!/usr/bin/env python3
"""Mnemosyne memory provider plugin for Hermes Agent (Nous Research).

Wraps the Mnemosyne ICMS (6-tier SQLite FTS5) as a Hermes MemoryProvider.
Local-first, no cloud dependency, stdlib-only core.

Drop this directory into hermes-agent/plugins/memory/mnemosyne/ and set:
    memory.provider: mnemosyne          (in Hermes config.yaml)
    MNEMOSYNE_PATH=/path/to/Mnemosyne   (env var, or set db_path in config)

The provider exposes three agent tools:
    memory_search(query, limit=8)
    memory_write(content, kind="fact", tier=2)
    memory_stats()

Turns are persisted asynchronously (daemon thread) so sync_turn() never
blocks the agent loop. A single write thread serialises all SQLite writes.
"""

from __future__ import annotations

import importlib
import json
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Minimal MemoryProvider ABC shim — used when running outside Hermes
# (tests, lab harness). When installed inside Hermes the real ABC takes over.
# ---------------------------------------------------------------------------
try:
    from agent.memory_provider import MemoryProvider  # type: ignore
except ImportError:
    import abc

    class MemoryProvider(abc.ABC):  # type: ignore[no-redef]
        @property
        @abc.abstractmethod
        def name(self) -> str: ...

        def is_available(self) -> bool: return True
        def initialize(self, session_id: str, **kwargs: Any) -> None: pass
        def system_prompt_block(self) -> str: return ""
        def prefetch(self, query: str, *, session_id: str = "") -> str: return ""
        def queue_prefetch(self, query: str, *, session_id: str = "") -> None: pass
        def sync_turn(self, user_content: str, assistant_content: str,
                      *, session_id: str = "",
                      messages: Optional[List[Dict[str, Any]]] = None) -> None: pass
        def get_tool_schemas(self) -> List[Dict[str, Any]]: return []
        def handle_tool_call(self, tool_name: str, args: Dict[str, Any],
                             **kwargs: Any) -> str: return ""
        def get_config_schema(self) -> List[Dict[str, Any]]: return []
        def save_config(self, values: Dict[str, Any], hermes_home: str) -> None: pass
        def on_session_end(self, messages: List[Dict[str, Any]]) -> None: pass
        def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str: return ""
        def shutdown(self) -> None: pass


# ---------------------------------------------------------------------------
# Mnemosyne path resolution (mirrors experiments/evals/common.py)
# ---------------------------------------------------------------------------
_MNEMOSYNE_CANDIDATES = [
    os.environ.get("MNEMOSYNE_PATH", ""),
    "/tmp/Mnemosyne",
    str(Path.home() / "mnemosyne-kali-test" / "Mnemosyne"),
    str(Path.home() / "Mnemosyne"),
]


def _find_mnemosyne() -> Optional[Path]:
    for cand in _MNEMOSYNE_CANDIDATES:
        if cand and (Path(cand) / "mnemosyne_memory.py").is_file():
            root = Path(cand).resolve()
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            return root
    return None


def _is_mnemosyne_importable() -> bool:
    root = _find_mnemosyne()
    if root is None:
        return False
    try:
        importlib.import_module("mnemosyne_memory")
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Background write worker
# ---------------------------------------------------------------------------
_SENTINEL = object()


class _WriteWorker(threading.Thread):
    """Serialises all MemoryStore writes and async prefetch onto one thread."""

    def __init__(self, store: Any) -> None:
        super().__init__(daemon=True, name="mnemosyne-write-worker")
        self._store = store
        self._q: queue.Queue = queue.Queue()

    def run(self) -> None:
        while True:
            item = self._q.get()
            if item is _SENTINEL:
                break
            fn, args, kwargs = item
            try:
                fn(*args, **kwargs)
            except Exception:
                pass  # log if needed; never crash the agent loop
            self._q.task_done()

    def submit(self, fn, *args, **kwargs) -> None:
        self._q.put((fn, args, kwargs))

    def stop(self) -> None:
        self._q.put(_SENTINEL)
        self.join(timeout=5)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class MnemosyneMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider backed by the Mnemosyne ICMS."""

    @property
    def name(self) -> str:
        return "mnemosyne"

    def is_available(self) -> bool:
        return _is_mnemosyne_importable()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = kwargs.get("hermes_home") or os.environ.get("HERMES_HOME", ".")
        cfg = self._load_config(hermes_home)

        db_path = Path(cfg.get("db_path") or
                       Path(hermes_home) / "mnemosyne" / "memory.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        _find_mnemosyne()  # ensure sys.path
        from mnemosyne_memory import MemoryStore  # type: ignore

        self._store = MemoryStore(path=db_path)
        self._session_id = session_id
        self._prefetch_limit = int(cfg.get("prefetch_limit", 8))
        self._prefetch_cache: str = ""
        self._worker = _WriteWorker(self._store)
        self._worker.start()

    def shutdown(self) -> None:
        if hasattr(self, "_worker"):
            self._worker.stop()
        if hasattr(self, "_store"):
            self._store.close()

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "## Mnemosyne Memory\n"
            "You have access to a persistent 6-tier ICMS (Integrated Cognitive "
            "Memory System) backed by a local SQLite database.\n"
            "- **memory_search(query)** — retrieve relevant memories before answering.\n"
            "- **memory_write(content, kind, tier)** — store a new memory "
            "(kind: fact/preference/goal/pattern; tier 2=event, 3=fact, 4=pattern, 5=identity).\n"
            "- **memory_stats()** — show tier distribution.\n"
            "Always search memory before answering questions about the user's "
            "preferences, history, projects, or personal context."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._prefetch_cache

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        def _do_prefetch():
            hits = self._store.search(query, limit=self._prefetch_limit)
            self._prefetch_cache = _format_hits(hits)

        self._worker.submit(_do_prefetch)

    # ------------------------------------------------------------------
    # Turn persistence
    # ------------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str,
                  *, session_id: str = "",
                  messages: Optional[List[Dict[str, Any]]] = None) -> None:
        sid = session_id or getattr(self, "_session_id", "hermes")

        def _write():
            if user_content:
                self._store.write(
                    f"[user] {user_content}",
                    source=sid, kind="turn", tier=2,
                )
            if assistant_content:
                self._store.write(
                    f"[assistant] {assistant_content}",
                    source=sid, kind="turn", tier=2,
                )

        self._worker.submit(_write)

    # ------------------------------------------------------------------
    # Agent tools
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Hermes MemoryManager expects already-normalized tool schemas with
        # top-level ``name``/``description``/``parameters`` fields. Returning
        # raw OpenAI ``{"type":"function","function":...}`` objects imports
        # fine standalone but leaves MemoryManager unable to route tool calls.
        return [
            {
                "name": "memory_search",
                "description": (
                    "Search the Mnemosyne ICMS for memories relevant to a query. "
                    "Returns up to `limit` results with content and tier."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "Natural-language search query"},
                        "limit": {"type": "integer", "default": 8,
                                  "description": "Max results to return (1-20)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "memory_write",
                "description": (
                    "Store a new memory in the ICMS. Use for facts, preferences, "
                    "goals, patterns, or identity-level notes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string",
                                    "description": "Memory content to store"},
                        "kind": {
                            "type": "string",
                            "enum": ["fact", "preference", "goal", "pattern", "identity"],
                            "default": "fact",
                        },
                        "tier": {
                            "type": "integer",
                            "description": "ICMS tier (2=event, 3=fact, 4=pattern, 5=identity)",
                            "default": 2,
                            "minimum": 0, "maximum": 5,
                        },
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "memory_stats",
                "description": "Return a summary of memory tier distribution.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any],
                         **kwargs: Any) -> str:
        if tool_name == "memory_search":
            query = args.get("query", "")
            limit = min(int(args.get("limit", 8)), 20)
            hits = self._store.search(query, limit=limit)
            if not hits:
                return "No memories found."
            return _format_hits(hits)

        if tool_name == "memory_write":
            content = args.get("content", "").strip()
            if not content:
                return "Error: content is required."
            kind = args.get("kind", "fact")
            tier = int(args.get("tier", 2))
            mem_id = self._store.write(
                content,
                source=getattr(self, "_session_id", "hermes"),
                kind=kind,
                tier=tier,
            )
            return f"Memory stored (id={mem_id}, tier={tier}, kind={kind})."

        if tool_name == "memory_stats":
            return _stats(self._store)

        return f"Unknown tool: {tool_name}"

    # ------------------------------------------------------------------
    # Session lifecycle hooks
    # ------------------------------------------------------------------

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        extractions = _try_extract(messages)
        if extractions:
            self._worker.submit(self._write_extractions, extractions)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        extractions = _try_extract(messages)
        if not extractions:
            return ""
        self._worker.submit(self._write_extractions, extractions)
        parts = []
        for cat in ("facts", "preferences", "goals", "patterns"):
            items = extractions.get(cat, [])
            if items:
                parts.append(f"{cat}: " + "; ".join(items))
        return "Preserved to memory — " + " | ".join(parts) if parts else ""

    def _write_extractions(self, extractions: Dict[str, Any]) -> None:
        _TIER = {"facts": 3, "preferences": 3, "goals": 4, "patterns": 4,
                 "relationships": 3}
        sid = getattr(self, "_session_id", "hermes")
        for cat, tier in _TIER.items():
            for item in extractions.get(cat, []):
                self._store.write(item, source=sid, kind=cat.rstrip("s"), tier=tier)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "mnemosyne_path",
                "label": "Mnemosyne checkout path",
                "description": "Directory containing mnemosyne_memory.py",
                "env_var": "MNEMOSYNE_PATH",
                "required": True,
            },
            {
                "name": "db_path",
                "label": "Database path",
                "description": "SQLite DB location (default: $HERMES_HOME/mnemosyne/memory.db)",
                "required": False,
            },
            {
                "name": "prefetch_limit",
                "label": "Prefetch result limit",
                "description": "Max memories retrieved before each turn (default: 8)",
                "required": False,
                "default": 8,
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        cfg_path = Path(hermes_home) / "mnemosyne.json"
        cfg_path.write_text(json.dumps(values, indent=2))

    def _load_config(self, hermes_home: str) -> Dict[str, Any]:
        cfg_path = Path(hermes_home) / "mnemosyne.json"
        if cfg_path.exists():
            try:
                return json.loads(cfg_path.read_text())
            except Exception:
                pass
        return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_hits(hits: List[Dict[str, Any]]) -> str:
    lines = []
    for i, h in enumerate(hits, 1):
        content = h.get("content") or h.get("text") or str(h)
        tier = h.get("tier", "?")
        lines.append(f"{i}. [tier {tier}] {content}")
    return "\n".join(lines)


def _stats(store: Any) -> str:
    try:
        from collections import Counter

        # Prefer direct SQLite counts when running against Mnemosyne's
        # MemoryStore. Search-based sampling can report "empty" for a real DB
        # if none of the broad probe terms match the stored rows.
        conn = getattr(store, "_conn", None)
        if conn is not None:
            rows = conn.execute(
                "SELECT tier, COUNT(*) FROM memories GROUP BY tier ORDER BY tier"
            ).fetchall()
            total = sum(int(row[1]) for row in rows)
            lines = [f"tier {row[0]}: {row[1]}" for row in rows]
            return f"Memory stats (total {total}): " + (", ".join(lines) if lines else "empty")

        # Fallback for MemoryStore-compatible objects that only expose search.
        probe_terms = ["the", "a", "is", "and", "or", "to", "of", "memory"]
        seen: dict = {}
        for term in probe_terms:
            try:
                for h in store.search(term, limit=500):
                    seen[h["id"]] = h.get("tier", "?")
            except Exception:
                pass
        counts = Counter(seen.values())
        lines = [f"tier {t}: {n}" for t, n in sorted(counts.items())]
        total = len(seen)
        return f"Memory stats ({total} sampled): " + (", ".join(lines) if lines else "empty")
    except Exception as exc:
        return f"Stats unavailable: {exc}"


def _try_extract(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Run extract_salient() if the extraction module is on the path."""
    try:
        lab_root = Path(__file__).resolve().parents[3]  # experiments/ parent
        ext_path = lab_root / "experiments" / "extraction_layer"
        if str(ext_path) not in sys.path:
            sys.path.insert(0, str(ext_path))
        from extractor import extract_salient  # type: ignore
        text = "\n".join(
            m.get("content", "") for m in messages
            if isinstance(m.get("content"), str)
        )
        return extract_salient(text) if text.strip() else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Hermes registration entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:  # noqa: ANN001
    ctx.register_memory_provider(MnemosyneMemoryProvider())
