"""
mnemosyne_tool_parsers.py — text-embedded tool-call extraction.

Purpose
-------
Some models emit tool calls as structured fields in the response
(OpenAI / Anthropic / Ollama with a compatible server — already handled
in mnemosyne_models). Others emit tool calls as *text inside the
assistant message* using a vendor-specific envelope:

    <tool_call>{"name": "...", "arguments": {...}}</tool_call>

The server doesn't parse these — it just returns the raw text. This
module recovers structured tool calls from assistant text across the
formats we've seen in the wild:

    - Hermes / Qwen-Agent:  <tool_call>{...}</tool_call>
    - Mistral instruct:     [TOOL_CALLS][{...}]
    - Llama-3 instruct:     <|python_tag|>{...}<|eom_id|>
    - Functionary:          ```json\n{"name": ..., "arguments": ...}```
    - Plain JSON fallback:  raw JSON object at end of message

Inspired by Hermes Agent's eleven-parser coverage. We ship five
parsers plus a `parse_any()` dispatcher that tries them in order.

Every parser returns a list of normalized dicts matching our internal
shape:

    [{"name": str, "arguments": dict, "id": str | None}, ...]

If no parser matches, returns []. Parsers never raise on malformed
input — they just fail to match and let the next parser try.

Stdlib only. Zero deps.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Callable


def _gen_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


def _coerce_arguments(args: object) -> dict:
    """Normalize an arguments field — Mistral/Llama sometimes emit
    strings that are themselves JSON; we unwrap one layer."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


# ---- parsers ----------------------------------------------------------------

_HERMES_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL
)


def parse_hermes(text: str) -> list[dict]:
    """Nous Hermes / Qwen-Agent tag format.

    <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    """
    out: list[dict] = []
    for m in _HERMES_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "name" not in obj:
            continue
        out.append({
            "id": obj.get("id") or _gen_id(),
            "name": str(obj["name"]),
            "arguments": _coerce_arguments(obj.get("arguments", {})),
        })
    return out


_MISTRAL_RE = re.compile(r"\[TOOL_CALLS\]\s*(\[.*?\])\s*(?:\[/TOOL_CALLS\])?", re.DOTALL)


def parse_mistral(text: str) -> list[dict]:
    """Mistral instruct inline token format.

    [TOOL_CALLS][{"name": "...", "arguments": {...}}]
    """
    m = _MISTRAL_RE.search(text)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []
    out: list[dict] = []
    for item in arr:
        if not isinstance(item, dict) or "name" not in item:
            continue
        out.append({
            "id": item.get("id") or _gen_id(),
            "name": str(item["name"]),
            "arguments": _coerce_arguments(item.get("arguments", {})),
        })
    return out


_LLAMA_RE = re.compile(
    r"<\|python_tag\|>(.*?)(?:<\|eom_id\|>|<\|eot_id\|>|$)", re.DOTALL
)


def parse_llama3(text: str) -> list[dict]:
    """Llama 3 instruct python-tag format.

    <|python_tag|>{"name": "...", "parameters": {...}}<|eom_id|>
    """
    out: list[dict] = []
    for m in _LLAMA_RE.finditer(text):
        body = m.group(1).strip()
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "name" not in obj:
            continue
        # Llama-3 sometimes uses "parameters" instead of "arguments"
        args = obj.get("arguments") or obj.get("parameters") or {}
        out.append({
            "id": obj.get("id") or _gen_id(),
            "name": str(obj["name"]),
            "arguments": _coerce_arguments(args),
        })
    return out


_FUNCTIONARY_RE = re.compile(
    r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", re.DOTALL
)


def parse_functionary(text: str) -> list[dict]:
    """Functionary / fenced-JSON format.

    ```json
    {"name": "...", "arguments": {...}}
    ```
    Only matches JSON with a "name" field so it doesn't over-fire on
    generic fenced JSON output.
    """
    out: list[dict] = []
    for m in _FUNCTIONARY_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "name" not in obj:
            continue
        out.append({
            "id": obj.get("id") or _gen_id(),
            "name": str(obj["name"]),
            "arguments": _coerce_arguments(obj.get("arguments", {})),
        })
    return out


_TRAILING_JSON_RE = re.compile(r"(\{[^{]*?\"name\"\s*:\s*\"[^\"]+\".*?\})\s*$",
                                  re.DOTALL)


def parse_trailing_json(text: str) -> list[dict]:
    """Plain-JSON fallback — a JSON object at the end of the message
    with a "name" and "arguments" field. Some small local models emit
    this without any envelope. Conservative: only matches at EOS."""
    m = _TRAILING_JSON_RE.search(text)
    if not m:
        return []
    try:
        obj = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict) or "name" not in obj:
        return []
    return [{
        "id": obj.get("id") or _gen_id(),
        "name": str(obj["name"]),
        "arguments": _coerce_arguments(obj.get("arguments", {})),
    }]


# ---- dispatcher -------------------------------------------------------------

#: Parser order — most specific first so generic regex doesn't steal
#: matches that belong to a structured format.
PARSERS: dict[str, Callable[[str], list[dict]]] = {
    "hermes":      parse_hermes,
    "mistral":     parse_mistral,
    "llama3":      parse_llama3,
    "functionary": parse_functionary,
    "trailing":    parse_trailing_json,
}


def parse_any(text: str, *, hint: str | None = None) -> list[dict]:
    """Try parsers in order until one matches. `hint` (if given) tries
    that parser first; on miss, falls through to the usual order."""
    if not text:
        return []
    order: list[str] = list(PARSERS)
    if hint and hint in PARSERS:
        order = [hint] + [k for k in order if k != hint]
    for name in order:
        result = PARSERS[name](text)
        if result:
            return result
    return []


def strip_tool_calls(text: str) -> str:
    """Remove tool-call envelopes from assistant text so the user-
    visible message doesn't show raw `<tool_call>…</tool_call>` gunk."""
    if not text:
        return text
    out = _HERMES_RE.sub("", text)
    out = _MISTRAL_RE.sub("", out)
    out = _LLAMA_RE.sub("", out)
    # Don't strip fenced JSON — it might be legitimate user-visible JSON
    return out.strip()


def detect_format(text: str) -> str | None:
    """Return the name of the first parser that matches, else None."""
    for name, fn in PARSERS.items():
        if fn(text):
            return name
    return None
