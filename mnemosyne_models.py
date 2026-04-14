"""
mnemosyne_models.py — model-agnostic inference backend.

Purpose
-------
Gives the brain (and any other consumer) a single API that works against:

    - Ollama local (qwen3:8b, qwen3.5:9b, gemma4:e4b, any `ollama pull` model)
    - Any OpenAI-compatible HTTP endpoint, including:
        * OpenRouter (200+ models behind one URL)
        * Anthropic (via OpenAI-compat wrapper)
        * Together AI / Fireworks / DeepInfra
        * vLLM / TGI / LM Studio / Nous Portal
        * OpenAI itself

Hermes has this same breadth via dedicated provider classes. We get equivalent
reach with a ~200-line stdlib-only module because the OpenAI chat-completion
API is the de-facto standard and every serious provider exposes it.

Design
------
- Backend is selected by URL + optional API key. Defaults to Ollama local.
- `chat(messages, model=None)` is the only hot-path method. Returns a dict:
    {"text": str, "tool_calls": list[dict], "raw": dict, "usage": dict | None}
- Streaming is not supported in v1 — the brain doesn't need streaming for
  routing decisions and we want the code lean. Easy to add later.
- Zero runtime dependencies: urllib.request + json. No httpx, no openai SDK.
- Telemetry integration: pass a TelemetrySession and every chat() call gets
  logged as a "model_call" event with timing, tokens, and status.

Credentials
-----------
API keys are read from environment variables at call time:
    OLLAMA_HOST              default http://localhost:11434
    OPENROUTER_API_KEY       for openrouter.ai/api/v1
    OPENAI_API_KEY           for api.openai.com/v1
    ANTHROPIC_API_KEY        for Anthropic-compatible endpoints
    NOUS_PORTAL_API_KEY      for Nous Portal
    MNEMOSYNE_MODEL_API_KEY  generic override, wins over all of the above

Never logs the key. Never includes it in trace metadata. The TelemetrySession's
secret redaction would catch it anyway, but we also never hand it to the log.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


# Well-known provider endpoints. The brain or a user can override via Backend(url=...)
PROVIDERS: dict[str, str] = {
    "ollama":     "http://localhost:11434/api/chat",
    "openai":     "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "together":   "https://api.together.xyz/v1/chat/completions",
    "fireworks":  "https://api.fireworks.ai/inference/v1/chat/completions",
    "anthropic":  "https://api.anthropic.com/v1/messages",  # different shape, handled specially
    "nous":       "https://inference-api.nousresearch.com/v1/chat/completions",
    "lmstudio":   "http://localhost:1234/v1/chat/completions",
    "vllm":       "http://localhost:8000/v1/chat/completions",
}

# Env-var name for each provider's key
API_KEY_ENV: dict[str, str] = {
    "openai":     "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together":   "TOGETHER_API_KEY",
    "fireworks":  "FIREWORKS_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
    "nous":       "NOUS_PORTAL_API_KEY",
}


@dataclass
class Backend:
    """Configuration for a model inference endpoint."""
    provider: str = "ollama"
    url: str | None = None         # override the default endpoint for the provider
    api_key: str | None = None     # override env-var lookup
    default_model: str = "qwen3:8b"
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS and self.url is None:
            raise ValueError(
                f"unknown provider {self.provider!r}; "
                f"pass url=... or pick from {list(PROVIDERS)}"
            )

    @property
    def endpoint(self) -> str:
        return self.url or PROVIDERS[self.provider]

    def resolve_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        generic = os.environ.get("MNEMOSYNE_MODEL_API_KEY", "").strip()
        if generic:
            return generic
        env_var = API_KEY_ENV.get(self.provider)
        if env_var:
            return os.environ.get(env_var, "").strip() or None
        return None


# ---- chat API ----------------------------------------------------------------

def chat(
    messages: list[dict[str, Any]],
    *,
    backend: Backend | None = None,
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    telemetry: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a chat completion against the configured backend.

    Parameters
    ----------
    messages
        OpenAI-shaped list of {role, content} dicts.
    backend
        Backend config. Defaults to Ollama local with qwen3:8b.
    model
        Override backend.default_model for this call.
    tools
        OpenAI-shaped tool definitions. Not all providers support tools.
    temperature, max_tokens
        Standard sampling knobs.
    telemetry
        Optional TelemetrySession. Every call logs a `model_call` event.
    extra
        Provider-specific fields merged into the payload.

    Returns
    -------
    dict with keys:
        text        — concatenated assistant text
        tool_calls  — list of parsed tool calls (empty if none)
        raw         — the full provider response JSON
        usage       — {prompt_tokens, completion_tokens, total_tokens} if reported
    """
    backend = backend or Backend()
    mdl = model or backend.default_model

    if backend.provider == "ollama":
        payload = _ollama_payload(messages, mdl, tools, temperature, max_tokens, extra)
    elif backend.provider == "anthropic":
        payload = _anthropic_payload(messages, mdl, tools, temperature, max_tokens, extra)
    else:
        payload = _openai_payload(messages, mdl, tools, temperature, max_tokens, extra)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "mnemosyne/0.1.0",
    }
    api_key = backend.resolve_api_key()
    if api_key:
        if backend.provider == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        backend.endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    started = time.monotonic()
    status = "ok"
    error: dict[str, Any] | None = None
    raw: dict[str, Any] = {}

    try:
        with urllib.request.urlopen(req, timeout=backend.timeout_s) as r:
            raw = json.load(r)
    except urllib.error.HTTPError as e:
        status = "error"
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"http_status": e.code}
        error = {"type": "HTTPError", "code": e.code, "body": body}
    except urllib.error.URLError as e:
        status = "error"
        error = {"type": "URLError", "reason": str(e.reason)}
    except Exception as e:
        status = "error"
        error = {"type": type(e).__name__, "message": str(e)}

    duration_ms = (time.monotonic() - started) * 1000.0

    # Parse provider response into a uniform shape
    if status == "ok":
        if backend.provider == "ollama":
            parsed = _parse_ollama(raw)
        elif backend.provider == "anthropic":
            parsed = _parse_anthropic(raw)
        else:
            parsed = _parse_openai(raw)
    else:
        parsed = {"text": "", "tool_calls": [], "usage": None}

    result = {
        **parsed,
        "raw": raw,
        "status": status,
        "error": error,
        "duration_ms": duration_ms,
        "model": mdl,
        "provider": backend.provider,
    }

    # Telemetry
    if telemetry is not None:
        try:
            telemetry.log(
                "model_call",
                tool=None,
                args={
                    "provider": backend.provider,
                    "model": mdl,
                    "message_count": len(messages),
                    "has_tools": bool(tools),
                },
                result={
                    "text_len": len(parsed["text"]),
                    "tool_calls_count": len(parsed["tool_calls"]),
                    "usage": parsed.get("usage"),
                },
                duration_ms=duration_ms,
                status=status,
                error=error,
            )
        except Exception:
            pass  # telemetry must never break inference

    return result


# ---- payload builders -------------------------------------------------------

def _openai_payload(
    messages: list[dict], model: str, tools: list[dict] | None,
    temperature: float | None, max_tokens: int | None, extra: dict | None,
) -> dict:
    p: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        p["tools"] = tools
    if temperature is not None:
        p["temperature"] = temperature
    if max_tokens is not None:
        p["max_tokens"] = max_tokens
    if extra:
        p.update(extra)
    return p


def _ollama_payload(
    messages: list[dict], model: str, tools: list[dict] | None,
    temperature: float | None, max_tokens: int | None, extra: dict | None,
) -> dict:
    p: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if tools:
        p["tools"] = tools
    options: dict[str, Any] = {}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    if options:
        p["options"] = options
    if extra:
        p.update(extra)
    return p


def _anthropic_payload(
    messages: list[dict], model: str, tools: list[dict] | None,
    temperature: float | None, max_tokens: int | None, extra: dict | None,
) -> dict:
    # Anthropic splits system from messages
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    chat_msgs = [m for m in messages if m.get("role") != "system"]
    p: dict[str, Any] = {
        "model": model,
        "messages": chat_msgs,
        "max_tokens": max_tokens or 4096,
    }
    if system_parts:
        p["system"] = "\n\n".join(system_parts)
    if tools:
        p["tools"] = tools
    if temperature is not None:
        p["temperature"] = temperature
    if extra:
        p.update(extra)
    return p


# ---- response parsers -------------------------------------------------------

def _parse_openai(raw: dict) -> dict[str, Any]:
    text = ""
    tool_calls: list[dict] = []
    if raw.get("choices"):
        msg = raw["choices"][0].get("message", {})
        text = msg.get("content", "") or ""
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            tool_calls.append({
                "id": tc.get("id"),
                "name": fn.get("name"),
                "arguments": _parse_json_safe(fn.get("arguments")),
            })
    return {"text": text, "tool_calls": tool_calls, "usage": raw.get("usage")}


def _parse_ollama(raw: dict) -> dict[str, Any]:
    msg = raw.get("message") or {}
    text = msg.get("content", "") or ""
    tool_calls: list[dict] = []
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function", {}) or {}
        tool_calls.append({
            "id": tc.get("id") or fn.get("name"),
            "name": fn.get("name"),
            "arguments": fn.get("arguments", {}),
        })
    usage: dict[str, Any] | None = None
    if "prompt_eval_count" in raw or "eval_count" in raw:
        usage = {
            "prompt_tokens": raw.get("prompt_eval_count"),
            "completion_tokens": raw.get("eval_count"),
            "total_tokens": (raw.get("prompt_eval_count") or 0)
                          + (raw.get("eval_count") or 0),
        }
    return {"text": text, "tool_calls": tool_calls, "usage": usage}


def _parse_anthropic(raw: dict) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in raw.get("content", []) or []:
        t = block.get("type")
        if t == "text":
            text_parts.append(block.get("text", ""))
        elif t == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "name": block.get("name"),
                "arguments": block.get("input", {}),
            })
    usage = raw.get("usage")
    if usage:
        usage = {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "total_tokens": (usage.get("input_tokens") or 0)
                          + (usage.get("output_tokens") or 0),
        }
    return {"text": "".join(text_parts), "tool_calls": tool_calls, "usage": usage}


def _parse_json_safe(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return val
    return val


# ---- ergonomic helpers ------------------------------------------------------

def from_env(provider: str | None = None) -> Backend:
    """Construct a Backend from environment variables.

    If `provider` is None, picks the first provider for which an API key is
    present, else falls back to Ollama local.
    """
    if provider:
        return Backend(provider=provider)
    for prov in ("openrouter", "openai", "anthropic", "nous", "together", "fireworks"):
        env_var = API_KEY_ENV.get(prov)
        if env_var and os.environ.get(env_var, "").strip():
            return Backend(provider=prov)
    # Ollama host override
    ollama_host = os.environ.get("OLLAMA_HOST", "").strip()
    if ollama_host:
        return Backend(
            provider="ollama",
            url=ollama_host.rstrip("/") + "/api/chat",
            default_model=os.environ.get("OLLAMA_MODEL", "qwen3:8b").strip(),
        )
    return Backend()


def reachable(backend: Backend | None = None) -> bool:
    """Quick TCP-level health check. True if the endpoint accepts a connection."""
    backend = backend or Backend()
    try:
        from urllib.parse import urlparse
        parts = urlparse(backend.endpoint)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        import socket
        with socket.create_connection((host, port), timeout=2):
            return True
    except Exception:
        return False
