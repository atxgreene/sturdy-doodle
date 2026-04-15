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
from typing import Any


# Well-known provider endpoints. The brain or a user can override via Backend(url=...)
# Every OpenAI-compatible endpoint "just works" through this module — you can add
# a new provider by dropping a URL into this dict and an env-var name into
# API_KEY_ENV. No subclassing, no adapter code.
PROVIDERS: dict[str, str] = {
    # Local runtimes (no API key required)
    "ollama":     "http://localhost:11434/api/chat",
    "lmstudio":   "http://localhost:1234/v1/chat/completions",
    "vllm":       "http://localhost:8000/v1/chat/completions",
    "tgi":        "http://localhost:8080/v1/chat/completions",           # HuggingFace TGI

    # First-party commercial
    "openai":     "https://api.openai.com/v1/chat/completions",
    "anthropic":  "https://api.anthropic.com/v1/messages",               # native shape, handled specially
    "google":     "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "mistral":    "https://api.mistral.ai/v1/chat/completions",
    "cohere":     "https://api.cohere.ai/compatibility/v1/chat/completions",
    "xai":        "https://api.x.ai/v1/chat/completions",

    # Aggregators / inference platforms
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "together":   "https://api.together.xyz/v1/chat/completions",
    "fireworks":  "https://api.fireworks.ai/inference/v1/chat/completions",
    "nous":       "https://inference-api.nousresearch.com/v1/chat/completions",
    "groq":       "https://api.groq.com/openai/v1/chat/completions",
    "deepseek":   "https://api.deepseek.com/v1/chat/completions",
    "cerebras":   "https://api.cerebras.ai/v1/chat/completions",
    "hyperbolic": "https://api.hyperbolic.xyz/v1/chat/completions",
    "perplexity": "https://api.perplexity.ai/chat/completions",
    "novita":     "https://api.novita.ai/v3/openai/chat/completions",
}

# Env-var name for each provider's key. Providers marked (local) don't need one.
API_KEY_ENV: dict[str, str] = {
    # local — no key:
    #   ollama, lmstudio, vllm, tgi

    # first-party commercial
    "openai":     "OPENAI_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
    "google":     "GOOGLE_API_KEY",          # Gemini via OpenAI-compat endpoint
    "mistral":    "MISTRAL_API_KEY",
    "cohere":     "COHERE_API_KEY",
    "xai":        "XAI_API_KEY",

    # aggregators
    "openrouter": "OPENROUTER_API_KEY",
    "together":   "TOGETHER_API_KEY",
    "fireworks":  "FIREWORKS_API_KEY",
    "nous":       "NOUS_PORTAL_API_KEY",
    "groq":       "GROQ_API_KEY",
    "deepseek":   "DEEPSEEK_API_KEY",
    "cerebras":   "CEREBRAS_API_KEY",
    "hyperbolic": "HYPERBOLIC_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "novita":     "NOVITA_API_KEY",
}

# Providers that don't require an API key (local runtimes)
LOCAL_PROVIDERS: set[str] = {"ollama", "lmstudio", "vllm", "tgi"}


@dataclass
class Backend:
    """Configuration for a model inference endpoint."""
    provider: str = "ollama"
    url: str | None = None         # override the default endpoint for the provider
    api_key: str | None = None     # override env-var lookup
    default_model: str = "qwen3:8b"
    timeout_s: float = 120.0
    rate_limiter: Any = None       # optional RateLimiter; shared across Backends is fine

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
    stream: bool = False,
    rate_limiter: "RateLimiter | None" = None,
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

    # Rate limit if a limiter is configured on the backend or passed explicitly.
    limiter = rate_limiter or getattr(backend, "rate_limiter", None)
    if limiter is not None:
        try:
            limiter.acquire(backend.provider)
        except Exception:
            pass  # limiter must never break inference

    if backend.provider == "ollama":
        payload = _ollama_payload(messages, mdl, tools, temperature, max_tokens, extra)
    elif backend.provider == "anthropic":
        payload = _anthropic_payload(messages, mdl, tools, temperature, max_tokens, extra)
    else:
        payload = _openai_payload(messages, mdl, tools, temperature, max_tokens, extra)

    if stream:
        payload["stream"] = True
        return _chat_stream(messages, payload, backend, mdl, telemetry)

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


# ---- streaming chat ---------------------------------------------------------

def _chat_stream(
    messages: list[dict[str, Any]],
    payload: dict[str, Any],
    backend: Backend,
    mdl: str,
    telemetry: Any | None,
) -> dict[str, Any]:
    """Streaming chat. Returns a dict with the same keys as chat() plus
    a `chunks` generator. The caller iterates `chunks` to get deltas.
    Providers emit different line shapes — we normalise:

        Ollama:   NDJSON (one JSON object per line)
        OpenAI-compatible: SSE ("data: {json}\\n\\n", terminated by "data: [DONE]")
        Anthropic: SSE with typed events

    The generator yields dicts of shape {"delta": str, "raw": <provider event>}.
    When the stream ends, the generator also sets `.text` / `.usage` on the
    outer result so callers that want the final concatenation can wait for
    completion.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if backend.provider != "ollama" else "application/x-ndjson",
        "User-Agent": "mnemosyne/0.2.0",
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
    text_parts: list[str] = []
    usage: dict[str, int] | None = None

    def gen():
        nonlocal usage
        try:
            with urllib.request.urlopen(req, timeout=backend.timeout_s) as r:
                for raw_line in r:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue
                    delta = ""
                    raw_evt: Any = line
                    if backend.provider == "ollama":
                        try:
                            obj = json.loads(line)
                            raw_evt = obj
                            delta = (obj.get("message") or {}).get("content", "") or obj.get("response", "")
                            if obj.get("done"):
                                if obj.get("eval_count"):
                                    usage = {
                                        "prompt_tokens": obj.get("prompt_eval_count", 0),
                                        "completion_tokens": obj.get("eval_count", 0),
                                        "total_tokens": obj.get("prompt_eval_count", 0) + obj.get("eval_count", 0),
                                    }
                        except Exception:
                            continue
                    else:
                        # SSE style: "data: {...}" or "data: [DONE]"
                        if not line.startswith("data:"):
                            continue
                        payload_s = line[5:].strip()
                        if payload_s in ("[DONE]", ""):
                            continue
                        try:
                            obj = json.loads(payload_s)
                            raw_evt = obj
                        except Exception:
                            continue
                        if backend.provider == "anthropic":
                            if obj.get("type") == "content_block_delta":
                                d = obj.get("delta") or {}
                                delta = d.get("text") or ""
                            elif obj.get("type") == "message_delta":
                                u = (obj.get("usage") or {})
                                if u:
                                    usage = {
                                        "prompt_tokens": u.get("input_tokens", 0),
                                        "completion_tokens": u.get("output_tokens", 0),
                                        "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0),
                                    }
                        else:
                            # OpenAI-compatible
                            choices = obj.get("choices") or []
                            if choices:
                                d = choices[0].get("delta") or {}
                                delta = d.get("content") or ""
                            u = obj.get("usage")
                            if u:
                                usage = {
                                    "prompt_tokens": u.get("prompt_tokens", 0),
                                    "completion_tokens": u.get("completion_tokens", 0),
                                    "total_tokens": u.get("total_tokens", 0),
                                }
                    if delta:
                        text_parts.append(delta)
                    yield {"delta": delta, "raw": raw_evt}
        finally:
            duration_ms = (time.monotonic() - started) * 1000.0
            if telemetry is not None:
                try:
                    telemetry.log(
                        "model_call",
                        args={"provider": backend.provider, "model": mdl,
                              "message_count": len(messages), "stream": True},
                        result={"text_len": sum(len(p) for p in text_parts),
                                "usage": usage},
                        duration_ms=duration_ms,
                        status="ok",
                    )
                except Exception:
                    pass

    return {
        "chunks": gen(),
        "text_parts": text_parts,     # populated as stream consumed
        "usage_ref": lambda: usage,   # lambda so callers can read after consumption
        "raw": {},
        "status": "ok",
        "error": None,
        "model": mdl,
        "provider": backend.provider,
        # Convenience: drain the stream into a final dict
        "drain": lambda: {
            "text": "".join(list(text_parts)) if isinstance(text_parts, list) else "",
            "tool_calls": [],
            "usage": usage,
            "status": "ok",
            "provider": backend.provider,
            "model": mdl,
        },
    }


# ---- rate limiter -----------------------------------------------------------

class RateLimiter:
    """Per-provider token-bucket rate limiter.

    Backends can carry their own limiter (`Backend(rate_limiter=...)`) or
    callers can pass a shared one to `chat(rate_limiter=...)`. The limiter
    blocks until a token is available.

    Not the fanciest limiter — no smoothing, no adaptive rates — just
    enough to keep cloud bills bounded for bursty agent work.

    Usage:
        limiter = RateLimiter(default_rps=1.0,
                              per_provider={"openai": 5.0, "anthropic": 2.0})
        chat(messages, backend=backend, rate_limiter=limiter)
    """

    def __init__(
        self,
        *,
        default_rps: float = 5.0,
        per_provider: dict[str, float] | None = None,
        burst: int = 3,
    ) -> None:
        import threading
        self.default_rps = max(0.001, default_rps)
        self.per_provider = dict(per_provider or {})
        self.burst = max(1, burst)
        self._tokens: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _rate(self, provider: str) -> float:
        return self.per_provider.get(provider, self.default_rps)

    def acquire(self, provider: str, timeout: float = 30.0) -> None:
        """Block until a token is available for `provider`, or raise on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                rate = self._rate(provider)
                last = self._last.get(provider, now)
                tokens = self._tokens.get(provider, float(self.burst))
                # Refill
                tokens = min(float(self.burst), tokens + (now - last) * rate)
                if tokens >= 1.0:
                    self._tokens[provider] = tokens - 1.0
                    self._last[provider] = now
                    return
                # How long to wait for one token
                wait = (1.0 - tokens) / rate
                self._tokens[provider] = tokens
                self._last[provider] = now
            if time.monotonic() + wait > deadline:
                raise TimeoutError(f"rate-limit timeout for provider {provider!r}")
            time.sleep(min(wait, 0.25))


# ---- cost pricing -----------------------------------------------------------

# Prices are USD per 1M tokens. Sourced from public provider pages as of 2026-04;
# these will drift — override at call time via `cost(...price_override=...)`.
# Keys match Backend(default_model=...) exactly when possible; fall back to
# substring match (e.g. "gpt-4o" matches "gpt-4o-2024-08-06").
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o":              {"prompt": 2.50,  "completion": 10.00},
    "gpt-4o-mini":         {"prompt": 0.15,  "completion": 0.60},
    "gpt-4-turbo":         {"prompt": 10.00, "completion": 30.00},
    "o1":                  {"prompt": 15.00, "completion": 60.00},
    "o1-mini":             {"prompt": 3.00,  "completion": 12.00},
    # Anthropic
    "claude-opus-4":       {"prompt": 15.00, "completion": 75.00},
    "claude-sonnet-4":     {"prompt": 3.00,  "completion": 15.00},
    "claude-haiku-4":      {"prompt": 0.80,  "completion": 4.00},
    "claude-3-5-sonnet":   {"prompt": 3.00,  "completion": 15.00},
    "claude-3-5-haiku":    {"prompt": 0.80,  "completion": 4.00},
    # Google
    "gemini-2.0-flash":    {"prompt": 0.10,  "completion": 0.40},
    "gemini-2.0-pro":      {"prompt": 1.25,  "completion": 5.00},
    # xAI
    "grok-3":              {"prompt": 2.00,  "completion": 10.00},
    # Local / free
    "ollama":              {"prompt": 0.00,  "completion": 0.00},
    "qwen3.5":             {"prompt": 0.00,  "completion": 0.00},
    "gemma4":              {"prompt": 0.00,  "completion": 0.00},
}


def cost_for(
    model: str,
    usage: dict[str, int] | None,
    *,
    price_override: dict[str, float] | None = None,
) -> dict[str, float]:
    """Estimate USD cost given a usage dict {prompt_tokens, completion_tokens}.

    Returns {"prompt_usd": x, "completion_usd": y, "total_usd": z, "matched": "model-key"}.
    """
    if not usage:
        return {"prompt_usd": 0.0, "completion_usd": 0.0, "total_usd": 0.0, "matched": ""}

    price = price_override
    matched = model
    if price is None:
        # exact match wins
        if model in DEFAULT_PRICING:
            price = DEFAULT_PRICING[model]
        else:
            # substring match (prefix)
            for k, v in DEFAULT_PRICING.items():
                if model.startswith(k):
                    price = v
                    matched = k
                    break
    if price is None:
        # Unknown model — no pricing data
        return {"prompt_usd": 0.0, "completion_usd": 0.0, "total_usd": 0.0, "matched": ""}

    pt = usage.get("prompt_tokens", 0) or 0
    ct = usage.get("completion_tokens", 0) or 0
    p_usd = pt * price["prompt"] / 1_000_000.0
    c_usd = ct * price["completion"] / 1_000_000.0
    return {
        "prompt_usd": round(p_usd, 6),
        "completion_usd": round(c_usd, 6),
        "total_usd": round(p_usd + c_usd, 6),
        "matched": matched,
    }


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
    # Fallback: if no structured tool_calls came back but the assistant
    # text contains Hermes/Mistral/Llama-3 embedded calls, recover them.
    # See mnemosyne_tool_parsers. Strips envelopes from `text` on match.
    if not tool_calls and text:
        tool_calls, text = _recover_embedded_tool_calls(text)
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
    # Fallback for Ollama models that emit text-embedded tool calls
    # (Hermes/Qwen-Agent/Mistral tags) rather than the structured field.
    if not tool_calls and text:
        tool_calls, text = _recover_embedded_tool_calls(text)
    usage: dict[str, Any] | None = None
    if "prompt_eval_count" in raw or "eval_count" in raw:
        usage = {
            "prompt_tokens": raw.get("prompt_eval_count"),
            "completion_tokens": raw.get("eval_count"),
            "total_tokens": (raw.get("prompt_eval_count") or 0)
                          + (raw.get("eval_count") or 0),
        }
    return {"text": text, "tool_calls": tool_calls, "usage": usage}


def _recover_embedded_tool_calls(text: str) -> tuple[list[dict], str]:
    """Extract text-embedded tool calls (Hermes/Qwen/Mistral/Llama-3
    envelopes). Returns (tool_calls, cleaned_text). On no match, the
    original text is returned untouched and tool_calls is [].
    """
    try:
        import mnemosyne_tool_parsers as tp
    except ImportError:  # pragma: no cover
        return [], text
    calls = tp.parse_any(text)
    if not calls:
        return [], text
    return calls, tp.strip_tool_calls(text)


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

def detect_providers() -> dict[str, dict[str, Any]]:
    """Return a map of {provider: {status, endpoint, env_var, reachable?}}
    for every provider known to this module.

    status is one of:
      "authorized"   — API key env var is set (or provider is local)
      "unauthorized" — cloud provider, no API key set
      "local"        — local provider, reachable over TCP (if checked)
      "unreachable"  — local provider, no TCP listener at its endpoint

    Intended for CLI tools like `mnemosyne-models-ls` that help the user
    understand which backends are available right now without writing
    them out of the config.
    """
    result: dict[str, dict[str, Any]] = {}
    for prov, endpoint in PROVIDERS.items():
        info: dict[str, Any] = {"endpoint": endpoint}
        if prov in LOCAL_PROVIDERS:
            info["kind"] = "local"
            info["env_var"] = None
            # Don't require reachability check — just report it was polled
            info["status"] = "local"
            info["reachable"] = reachable(Backend(provider=prov))
        else:
            env_var = API_KEY_ENV.get(prov)
            info["kind"] = "cloud"
            info["env_var"] = env_var
            has_key = bool(env_var and os.environ.get(env_var, "").strip())
            info["status"] = "authorized" if has_key else "unauthorized"
        result[prov] = info
    return result


# Preference order for from_env() auto-selection. Local-first, then aggregators
# that give broad model access, then first-party. Users override with an
# explicit provider= argument or MNEMOSYNE_MODEL_PROVIDER env var.
_AUTOSELECT_ORDER = [
    "ollama", "lmstudio", "vllm", "tgi",
    "openrouter", "nous", "groq", "cerebras",
    "anthropic", "openai", "google", "mistral", "xai", "cohere",
    "deepseek", "together", "fireworks", "hyperbolic", "perplexity", "novita",
]


def from_env(provider: str | None = None) -> Backend:
    """Construct a Backend from environment variables.

    Selection logic:
      1. If `provider` is given, use it.
      2. If $MNEMOSYNE_MODEL_PROVIDER is set, use it.
      3. Pick the first authorized cloud provider OR reachable local
         provider from _AUTOSELECT_ORDER.
      4. Fall back to Ollama local with whatever $OLLAMA_HOST / $OLLAMA_MODEL
         suggest (or the stdlib defaults).
    """
    chosen = provider or os.environ.get("MNEMOSYNE_MODEL_PROVIDER", "").strip() or None
    if chosen:
        # Honor OLLAMA_HOST override for ollama provider
        if chosen == "ollama":
            ollama_host = os.environ.get("OLLAMA_HOST", "").strip()
            if ollama_host:
                return Backend(
                    provider="ollama",
                    url=ollama_host.rstrip("/") + "/api/chat",
                    default_model=os.environ.get("OLLAMA_MODEL", "qwen3:8b").strip(),
                )
        return Backend(provider=chosen)

    detected = detect_providers()
    for prov in _AUTOSELECT_ORDER:
        info = detected.get(prov, {})
        if info.get("status") == "authorized":
            return Backend(provider=prov)
        if info.get("status") == "local" and info.get("reachable"):
            if prov == "ollama":
                return Backend(
                    provider="ollama",
                    default_model=os.environ.get("OLLAMA_MODEL", "qwen3:8b").strip(),
                )
            return Backend(provider=prov)

    # Nothing authorized, nothing local reachable — return default Ollama
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


# ---- Ollama-specific local-model helpers ------------------------------------

def _ollama_base(host: str | None = None) -> str:
    h = (host or os.environ.get("OLLAMA_HOST", "") or "http://localhost:11434").strip()
    return h.rstrip("/")


def ollama_list_pulled(host: str | None = None, timeout: float = 3.0) -> list[str]:
    """List models that `ollama pull` has already downloaded.

    Returns an empty list if Ollama isn't running. Never raises on timeout
    — this is meant for discovery, not fatal errors.
    """
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(f"{_ollama_base(host)}/api/tags", timeout=timeout) as r:
            data = json.load(r)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return []
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def ollama_model_info(model: str, host: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    """Fetch metadata for an Ollama model via /api/show.

    Returns {context_length, family, parameter_size, quantization, details}
    when available, or {"error": ...} on failure.

    Important for local-first tuning: callers use this to pick a
    `memory_retrieval_limit` that won't blow past the model's context window.
    """
    import urllib.request
    import urllib.error
    body = json.dumps({"model": model}).encode("utf-8")
    req = urllib.request.Request(
        f"{_ollama_base(host)}/api/show",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "model": model}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"error": f"network: {e!r}", "model": model}

    info = data.get("model_info") or {}
    details = data.get("details") or {}

    # Ollama reports context length under architecture-specific keys.
    # Scan common ones rather than hard-coding per-family.
    context_length: int | None = None
    for k, v in info.items():
        if k.endswith("context_length") and isinstance(v, int):
            context_length = v
            break

    return {
        "model": model,
        "context_length": context_length,
        "family": details.get("family") or details.get("families", [None])[0],
        "parameter_size": details.get("parameter_size"),
        "quantization": details.get("quantization_level"),
        "format": details.get("format"),
    }


def ollama_ensure_pulled(
    model: str,
    host: str | None = None,
    auto_pull: bool = False,
    timeout: float = 600.0,
) -> tuple[bool, str]:
    """Return (ready, status_message).

    - (True, "already pulled") if the model is present
    - (True, "pulled") if auto_pull=True and the pull succeeded
    - (False, reason)  otherwise

    By default auto_pull=False so we never silently trigger a multi-GB
    download from a library function. The wizard / install script can
    pass auto_pull=True explicitly.
    """
    pulled = ollama_list_pulled(host)
    if not pulled and not reachable(Backend(provider="ollama")):
        return (False, "ollama daemon not reachable")
    if model in pulled:
        return (True, "already pulled")
    if not auto_pull:
        return (False, f"model {model!r} not pulled (pass auto_pull=True or run: ollama pull {model})")

    import urllib.request
    import urllib.error
    body = json.dumps({"model": model, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        f"{_ollama_base(host)}/api/pull",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # Pull returns a stream of JSON updates; last line has status
            last = b""
            for chunk in r:
                if chunk.strip():
                    last = chunk
            try:
                data = json.loads(last)
            except json.JSONDecodeError:
                data = {}
            if data.get("status") == "success":
                return (True, "pulled")
            return (False, f"pull finished without success: {data}")
    except urllib.error.HTTPError as e:
        return (False, f"pull failed HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return (False, f"pull failed: {e!r}")


def recommended_context_budget(context_length: int | None) -> int:
    """Conservative retrieval-memory count given a model's context window.

    The brain uses this to cap memory_retrieval_limit when
    BrainConfig.adapt_to_context=True. Rough rule: reserve ~1/3 of context
    for memories + system prompt + tool catalog, assume ~300 tokens per
    memory row.
    """
    if not context_length or context_length <= 0:
        return 6  # safe default for unknown models
    memory_budget_tokens = context_length // 3
    per_memory_tokens = 300
    return max(2, min(20, memory_budget_tokens // per_memory_tokens))


# ---- CLI: mnemosyne-models ---------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    """CLI: list detected providers, show current auto-selection, test a call.

    Subcommands:
      list        print all known providers with status + endpoint
      current     show which provider from_env() would auto-select
      ping        TCP-level reachability probe for a given provider (or all)
    """
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(
        prog="mnemosyne-models",
        description="Inspect and manage model backends. Shows which providers "
                    "are authorized (have API keys) or reachable (local).",
    )
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=False)

    sub.add_parser("list", help="list all known providers and their status")
    sub.add_parser("current", help="show which provider from_env() picks")

    pg = sub.add_parser("ping", help="TCP reachability probe for one or all providers")
    pg.add_argument("provider", nargs="?", help="provider name, or omit to ping all")

    sub.add_parser("pulled", help="list models already pulled into local Ollama")

    ip = sub.add_parser("info", help="Ollama metadata for a model (context, family, quant)")
    ip.add_argument("model", help="e.g. qwen3:8b, gemma4:e4b, qwen3.5:9b")

    args = p.parse_args(argv)
    cmd = args.cmd or "list"

    if cmd == "list":
        detected = detect_providers()
        if args.json:
            json.dump(detected, sys.stdout, indent=2, default=str)
            print()
            return 0
        # Human-readable table
        print(f"{'provider':<14}  {'kind':<6}  {'status':<13}  {'env var':<22}  endpoint")
        print("-" * 120)
        for prov in sorted(detected):
            info = detected[prov]
            env_var = info.get("env_var") or "-"
            status = info["status"]
            if info["kind"] == "local":
                status += "/reachable" if info.get("reachable") else "/unreachable"
            print(f"{prov:<14}  {info['kind']:<6}  {status:<13}  {env_var:<22}  {info['endpoint']}")
        return 0

    if cmd == "current":
        b = from_env()
        out = {
            "provider": b.provider,
            "endpoint": b.endpoint,
            "default_model": b.default_model,
            "has_api_key": bool(b.resolve_api_key()),
        }
        if args.json:
            json.dump(out, sys.stdout, indent=2)
            print()
        else:
            print(f"provider:       {b.provider}")
            print(f"endpoint:       {b.endpoint}")
            print(f"default_model:  {b.default_model}")
            print(f"has_api_key:    {out['has_api_key']}")
        return 0

    if cmd == "ping":
        targets: list[str]
        if args.provider:
            if args.provider not in PROVIDERS:
                print(f"unknown provider: {args.provider}", file=sys.stderr)
                return 2
            targets = [args.provider]
        else:
            targets = list(PROVIDERS.keys())
        results: dict[str, bool] = {}
        for prov in targets:
            ok = reachable(Backend(provider=prov))
            results[prov] = ok
            if not args.json:
                mark = "✓" if ok else "✗"
                print(f"  {mark} {prov:<14} {PROVIDERS[prov]}")
        if args.json:
            json.dump(results, sys.stdout, indent=2)
            print()
        return 0

    if cmd == "pulled":
        names = ollama_list_pulled()
        if args.json:
            json.dump(names, sys.stdout, indent=2)
            print()
        else:
            if not names:
                print("(ollama not reachable or no models pulled)")
            else:
                for n in names:
                    print(f"  {n}")
        return 0

    if cmd == "info":
        info = ollama_model_info(args.model)
        if args.json:
            json.dump(info, sys.stdout, indent=2, default=str)
            print()
        else:
            if info.get("error"):
                print(f"error: {info['error']}")
                return 4
            print(f"model:            {info['model']}")
            print(f"family:           {info.get('family') or '-'}")
            print(f"parameter_size:   {info.get('parameter_size') or '-'}")
            print(f"quantization:     {info.get('quantization') or '-'}")
            print(f"format:           {info.get('format') or '-'}")
            ctx = info.get("context_length")
            print(f"context_length:   {ctx or '-'}")
            if ctx:
                budget = recommended_context_budget(ctx)
                print(f"memory budget:    {budget}  (suggested memory_retrieval_limit for this context)")
        return 0

    return 2


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main())
