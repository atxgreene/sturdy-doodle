# Positioning: what this is, what it isn't, what to use instead

*Honest comparison against the three reference points in the 2026 local-agent landscape: Hermes Agent (Nous Research), OpenClaw, and the observability-tool cluster (Langfuse / OpenLLMetry / Phoenix). Maintained so anyone evaluating this repo gets the real picture, not marketing.*

---

## One-line summary

**Mnemosyne is a complete, local-first, consciousness-aware agent framework whose differentiators are (1) a three-tier ICMS memory model backed by SQLite+FTS5, (2) a consciousness/meta-harness layer (TurboQuant, metacognition, dream consolidation, autobiography, behavioral coupling) that operates on the base harness between turns, and (3) a Meta-Harness-aligned observability substrate (telemetry, experiments, sweep, scenarios, Pareto) designed for optimization — not monitoring.** It is deliberately zero-dependency Python (stdlib + optional eternal-context/fantastic-disco) and is packaged for one-command install via `pip install -e .`.

---

## Where we sit vs. the rest of the landscape

| Dimension | Mnemosyne | Hermes Agent | OpenClaw | Langfuse / Phoenix / OTEL |
|---|---|---|---|---|
| Primary purpose | Complete agent framework w/ built-in observability+optimization substrate | Complete agent framework | Complete agent framework | Observability dashboards for cloud-API SaaS |
| Runtime deps | Zero (stdlib only) | SQLite, voice libs, browser drivers, MCP client, many SDKs | Pi runtime, plugin loader, many SDKs | Web stack, DB, OTEL collector |
| Install footprint | ~150 KB source + zero libs | Tens of MB | Full Electron-ish stack | Hosted service or docker-compose |
| Memory | SQLite+FTS5 + ICMS 3-tier (L1/L2/L3) | SQLite+FTS5 + MEMORY.md/USER.md | AGENTS.md/TOOLS.md + Active Memory plugin | N/A (not an agent) |
| Consciousness layer | TurboQuant, metacognition, dream consolidation, autobiography, behavioral coupling (via fantastic-disco) | No equivalent | No equivalent | N/A |
| Meta-Harness observability | ✓ telemetry + experiments + sweep + scenarios + Pareto frontier | Compressed feedback via "periodic nudge" (the exact pattern the paper argues against) | Event stream, no Pareto | ✓ for monitoring, not optimization |
| Self-improving skills | ✓ via `record_learned_skill()` with telemetry integration | ✓ writes markdown skills after task success | ✓ via ClawHub marketplace | N/A |
| Channels | Inherited from eternal-context: Telegram/Slack/Discord/REST (4) | 10+: Telegram/Discord/Slack/WhatsApp/Signal/SMS/Email/Matrix/Mattermost/voice | 20+: WhatsApp/Telegram/Slack/Discord/Signal/iMessage/Teams/Matrix/etc. | N/A |
| Model backends | Ollama + any OpenAI-compatible HTTP (OpenRouter, Anthropic, OpenAI, Together, Fireworks, Nous Portal, vLLM, LM Studio) | 200+ via OpenRouter + native providers | Pi agent core, local + cloud | N/A |
| Browser / voice | No (future) | Full browser automation + voice | Full browser (ClawHub plugins) | N/A |
| MCP integration | Planned (not shipped in v1) | ✓ stdio + HTTP | ✓ via ClawHub | N/A |
| Tool format | agentskills.io-compatible markdown + Python decorators | agentskills.io-compatible | ClawHub (agentskills.io-compatible) | N/A |
| Maturity (stars, age) | Pre-release, single author | 57k stars, 6 weeks, large community | 120k stars, ~year old, huge community | Mature, thousands of production deployments |
| Test coverage | 100/100 (71 unit + 29 integration), shellcheck clean | Unknown | Unknown | N/A |
| Deploy topology | Local-first, designed for WSL2 / Linux personal boxes | Gateway daemon, multi-platform | Gateway daemon, multi-platform | Hosted service |

---

## What we honestly DO NOT have (yet) that Hermes or OpenClaw do

1. **Browser automation.** Hermes has Browserbase / Browser Use / local Chrome CDP. OpenClaw has first-class browser + canvas. We have nothing. This is a legitimate gap for any agent that needs to interact with web UIs.
2. **Voice.** Hermes has voice across CLI + Discord voice channels. We don't. Realistic path: delegate to whisper.cpp + piper via subprocess, but not shipped.
3. **Broad channel surface.** OpenClaw supports 20+ messaging platforms including WhatsApp, iMessage, Teams, Matrix, Feishu, LINE, Zalo. We inherit eternal-context's four (Telegram/Slack/Discord/REST). Adding more is eternal-context's job, not ours.
4. **MCP integration.** Both Hermes and OpenClaw can connect to any MCP server. We haven't shipped an MCP client yet. High-priority follow-up because it's how most new 2026 tools expose themselves.
5. **Skills marketplace.** OpenClaw's ClawHub has 15,000+ community skills. We define our own skill format (agentskills.io-compatible so they're portable) but there's no catalog to pull from. Real skills come from writing them.
6. **Community.** We have 0 external users. Hermes has 57k stars in 6 weeks. OpenClaw has 120k stars. This isn't a "feature" but it determines which issues get fixed, which skills get written, and whether the project survives.

## Why we still built this instead of using Hermes or OpenClaw

1. **eternal-context is the user's existing code.** Migrating off it discards ICMS 3-tier memory (L1 hot / L2 warm / L3 cold with explicit promote/demote semantics), TurboQuant compressed meta-reasoning, dream consolidation, autobiography persistence, and behavioral coupling. None of those exist in Hermes or OpenClaw. They're architectural commitments, not plugins.

2. **Meta-Harness-aligned observability is a different design target than monitoring.** Hermes has a "periodic nudge" that decides what's worth keeping — the paper argues this compressed-feedback pattern is the exact failure mode prior optimizers fell into. Our `harness_telemetry` deliberately does **not** compress. This isn't "better" in every sense — it's optimized for a different downstream user (an agentic proposer doing harness search), not for a human reading a dashboard.

3. **Zero runtime dependencies.** Stdlib only. No SQLite client lib (sqlite3 is stdlib), no httpx, no openai SDK, no voice libs, no browser drivers. For a personal local-first agent running on a WSL2 box that might be air-gapped, that matters. Hermes and OpenClaw are ~orders of magnitude larger installs.

4. **Consciousness layer.** TurboQuant, metacognition, dream consolidation, autobiography, behavioral coupling — these are fantastic-disco's experimental features for cross-session personality persistence and self-monitoring. Neither Hermes nor OpenClaw has anything in this category. This is where the Mnemosyne stack is genuinely novel, and it's the hardest part to reproduce elsewhere.

## When to use Mnemosyne

- You have an existing eternal-context or fantastic-disco deployment and want deployment + observability tooling that speaks the same ICMS/consciousness language.
- You care more about *optimizing* an agent (sweep + Pareto + scenarios) than about *running* one (dashboards + alerts).
- You want a zero-dep, local-first, pip-installable stack that works on WSL2 without network access.
- You're interested in the consciousness/meta-harness research direction.

## When to use Hermes Agent instead

- You want the most feature-complete, most-tested open-source personal agent available today.
- You need voice, browser automation, or 10+ channel support out of the box.
- You want to use OpenRouter / Anthropic / many cloud providers without writing adapter code.
- You don't have a preexisting consciousness layer you want to preserve.

## When to use OpenClaw instead

- You want 120k-star scale community + a marketplace of 15k community-built skills.
- You want 20+ messaging channels including WhatsApp / iMessage / Teams.
- You want a fully GUI-driven installation experience with plugin management.

## When to use Langfuse / Phoenix / OTEL instead

- You're running a cloud-API-backed SaaS agent and need production monitoring.
- You need dashboards, alerting, cost tracking, team collaboration.
- Your team reads compressed metrics and pre-aggregated traces; no one is running a harness-optimization loop over raw traces.

---

## Summary: strategic position

**Mnemosyne is narrower and more opinionated than Hermes or OpenClaw; different category from Langfuse.** It serves a specific combination of needs (local-first + consciousness layer + Meta-Harness observability + zero deps + existing eternal-context integration) that no other project currently serves. It is not trying to beat Hermes on feature breadth — that's not the game.

If we ever want to broaden the surface area (voice, browser, more channels), the efficient path is probably to wire Hermes's or OpenClaw's tool modules as Mnemosyne skills via MCP rather than re-implement. The existing skill registry is agentskills.io-compatible for exactly this reason.

---

## References

- [Meta-Harness: End-to-End Optimization of Model Harnesses (Lee et al., Stanford/MIT/KRAFTON, 2026)](https://arxiv.org/abs/2603.28052)
- [Hermes Agent (Nous Research, 2026)](https://hermes-agent.nousresearch.com/) — [GitHub](https://github.com/NousResearch/hermes-agent)
- [OpenClaw](https://docs.openclaw.ai/) — [GitHub](https://github.com/openclaw/openclaw)
- [Langfuse](https://langfuse.com/), [OpenLLMetry](https://github.com/traceloop/openllmetry), [Arize Phoenix](https://phoenix.arize.com/), [TruLens](https://github.com/truera/trulens)
- [agentskills.io](https://agentskills.io/) — skill format standard
