# Mnemosyne UI

Browser dashboard. Single-page, vanilla JS, served by `mnemosyne-serve`.
No build step, no framework, no dependencies beyond what `mnemosyne-serve`
already brings in.

![dashboard](dashboard.png)

## Quick start

```sh
pip install -e .
mnemosyne-serve --port 8484 &           # daemon owns the memory store
open http://127.0.0.1:8484/ui            # dashboard
```

If you want bearer-token auth:

```sh
MNEMOSYNE_SERVE_TOKEN=hunter2 mnemosyne-serve --port 8484 &
# then in the browser console: localStorage.setItem('mnemosyne.token','hunter2')
```

## What you see

Six panels arranged in a responsive grid:

- **Avatar (left)** — SVG that visualizes the agent's current state. See
  [Avatar visual contract](#avatar-visual-contract) for what each
  element represents.
- **Chat (middle top)** — POSTs to `/turn`. The "hard turn (inner
  dialogue)" toggle adds `metadata.tags=["hard"]` so the brain takes
  the Planner → Critic → Doer path.
- **Events (right)** — SSE-streamed `events.jsonl` rows from the
  daemon's run. Falls back to polling when an auth token is set
  (EventSource can't carry custom headers).
- **Memory tiers (bottom middle)** — L1 hot / L2 warm / L3 cold counts
  with proportional bars.
- **Goals (bottom right)** — open goals from `goals.jsonl`. Add and
  resolve inline.

The **status pills** in the topbar show: mood phase, memory count,
skills count, open-goals count, identity strength %.

## Avatar visual contract

Every visual element maps to one observable agent trait. No magic, no
opaque "personality engine."

| Element | Trait | Source |
|---|---|---|
| **Aura halo (breathing)** | `pulses_per_minute` derived from recent activity | derived in `mnemosyne_avatar.py` |
| **Core orb size + brightness** | `health` (composite of identity strength + activity) | derived |
| **Core orb jitter animation** | fires when `restlessness > 0.3` | CV of inter-turn gaps |
| **Concentric rings** | `rings` = inner-dialogue activations (capped 8) | `inner_dialogue_done` events |
| **Self-assessment rays** | 0–12 short rays between core and inner rings, scaled by `self_assessment` | Evaluator persona accept/revise verdicts |
| **Orbiting dots** | `skills_count` (capped 12) | skill registry size |
| **Outer dashed wisdom ring** | opacity scales with `wisdom`; rotates slowly | derived from memory × age × identity |
| **Eye openness** | `mood_phase` ("focus" wide, "rest" narrow) | derived |
| **Memory roots (3 lines down)** | `l1_count`, `l2_count`, `l3_count` log-scaled | `memory.db` row counts |
| **Habitat wave bands (bottom)** | three soft bands; heights scale with L1/L2/L3 proportions | same counts as roots |
| **Red rim scars** | `identity_slip_count` | `identity_slip_detected` events |
| **Consolidate-mode petals** | only when `mood_phase=="consolidate"` | dream cadence > inner cadence |
| **Color palette** | `palette.{core, accent, rim, bg}` derived from health × activity | deterministic mapping |

### AGI-scaling traits (v1 schema, populated where observable)

Four traits computed from observable signals. Each is `null` when the
signal isn't available — we don't fake numbers when we don't know.

| Trait | Derivation | Null when |
|---|---|---|
| `wisdom` | `log10(memory_count+1)/4 × min(age_days/90, 1) × identity_strength` | no memory or age < 0.5 days |
| `restlessness` | coefficient of variation of inter-turn gaps, clipped [0..1] | fewer than 3 successful turns |
| `novelty` | `skill_learned` events per week, clipped [0..1] | `age_days < 1` or `skills_count == 0` |
| `self_assessment` | evaluator accept ratio: `accept / (accept + revise)` | Evaluator persona has never fired |

The UI trait grid renders `—` for null — honest signal that the
measurement isn't yet available.

Two examples:

| Resting (empty agent) | Active (memories, dreams, slips, inner-dialogue) |
|---|---|
| ![rest](avatar-rest.png) | ![active](avatar-active.png) |

## Schema versioning (AGI-scaling brief)

The avatar state JSON carries `schema_version: 1`. Future additions
are append-only — keys never get renamed or removed. Old `avatar.json`
files load forever, and old UI code keeps rendering old states without
rebuilding.

Reserved-but-unset slots in v1 (always present, populated `null` until
we have an honest way to compute them):

- `wisdom` — agreement with self over time (does the agent contradict
  past memories?)
- `restlessness` — variance in inter-turn gap
- `novelty` — rate of new skills learned per week
- `self_assessment` — result of the Evaluator persona scoring the
  Doer's output

Each of these would become a new visual element in `avatar.js` without
breaking anything that already exists.

## Architecture

```
   +----- browser ---------------------------------------+
   |  index.html                                          |
   |  ├─ avatar.js  ── builds SVG from /avatar JSON       |
   |  └─ app.js     ── polls /avatar /stats /goals        |
   |                  ── SSE /events_stream               |
   |                  ── POST /turn /goals                |
   +-------------------- HTTP / SSE ----------------------+
                          │
   +----- mnemosyne-serve --------------------------------+
   |  GET  /ui                 → static HTML              |
   |  GET  /ui/static/*        → CSS, JS, SVG             |
   |  GET  /avatar             → mnemosyne_avatar.compute_state |
   |  GET  /events_stream      → tails events.jsonl (SSE) |
   |  GET  /stats /goals       → existing JSON endpoints  |
   |  POST /turn /goals /dream → existing handlers        |
   +-------+----------------------------+-----------------+
           │                            │
   mnemosyne_avatar.py        Brain + MemoryStore + telemetry
   (state derived from        (single shared instance owned
    memory.db + events.jsonl)  by the daemon)
```

## Endpoints added by the UI work

| Endpoint | Purpose | Module |
|---|---|---|
| `GET /ui` | dashboard HTML | `mnemosyne_serve._serve_ui_index` |
| `GET /ui/static/*` | CSS / JS / SVG (path-traversal rejected) | `_serve_static` |
| `GET /avatar` | current `compute_state()` JSON | `Service.handle_avatar` |
| `GET /events_stream` | Server-Sent Events tail of run's events.jsonl | `_stream_events` |
| `GET /memory/search?q=…&limit=N&tier_max=N` | FTS5 search across memories, capped at 50 results | `Service.handle_memory_search` |

Existing endpoints (`/turn`, `/stats`, `/goals`, `/recent_events`,
`/healthz`, etc.) are unchanged and consumed by `app.js`.

## Memory browser

The bottom panel of the dashboard is a live FTS5 search over the
agent's memory. Type a query, pick a tier ceiling (all / L1 / L1+L2 /
all three), hit `Find` — results stream back with the matching
content, tier pill, kind, creation date, and access count.

Use cases:

- **Debug a bad retrieval.** If the agent answered wrong, search for
  the user's keywords and confirm the memory you expected was in the
  store. If it wasn't, you know retrieval is the bottleneck.
- **Audit what the agent knows.** Before trusting the agent with a
  new task, browse the memory for anything that could leak.
- **Spot duplicates.** Frequent near-duplicates in L1 are a signal
  that dream consolidation should run.

The endpoint caps results at 50 per request — enough to be useful,
not enough to leak the whole database in one query. Content is
truncated at 500 chars per row to keep responses bounded.

## Security

- **Bind address.** Defaults to `127.0.0.1`. Override with `--host`
  only if you trust your network.
- **Bearer token.** `MNEMOSYNE_SERVE_TOKEN=…` (or `--token …`)
  protects every endpoint *except* the SSE stream — `EventSource`
  cannot send custom headers, so when a token is set the UI falls
  back to polling `/recent_events` over `Authorization: Bearer …`.
- **Constant-time token compare.** The token check uses
  `hmac.compare_digest` so the response time doesn't leak the
  matching prefix length. Protects against a LAN attacker with access
  to request timings.
- **Body-size cap.** `POST` requests are rejected with HTTP 413 if
  `Content-Length > 1 MiB`. Prevents lazy DoS from a client sending
  oversized payloads.
- **Static path traversal.** `/ui/static/<path>` is resolved against
  the `mnemosyne_ui/static/` root and rejected if it escapes.
- **Memory browser bounds.** `/memory/search` caps `limit` at 50 and
  truncates each returned content to 500 chars.
- **No CORS.** The UI is served from the same origin as the API; we
  never set `Access-Control-Allow-Origin: *`.

## Generating a screenshot

The dashboard is HTML, so any browser can screenshot it. For headless
contexts, render the avatar to a static SVG instead:

```sh
mnemosyne-avatar render-svg --out /tmp/mnemo-avatar.svg --size 500
```

The SVG carries SMIL animations that play in browsers and image
viewers that support them. Convert to PNG with any SVG renderer
(`cairosvg`, `rsvg-convert`, `inkscape`).

## Future-facing extensions (deliberately not shipped yet)

- **Bidirectional avatar.** The avatar state could *signal back* into
  the agent: "low health" lowers `memory_retrieval_limit`; "consolidate
  mood" pauses new turn dispatch and lets dreams catch up. Today the
  signal flows one way — agent state → visualization.
- **Habitat**. The current background is a flat dark panel. A
  visualized environment (objects per learned skill, biome per mood,
  weather per identity-strength) is on the list but kept off the v1
  ship to avoid scope creep before the data justifies it.
- **Inter-agent visibility.** When two Mnemosyne instances negotiate
  over a shared store, both avatars on one screen showing the
  handshake. Speculative; needs the negotiation protocol first.
- **`wisdom`, `restlessness`, `novelty`, `self_assessment`** —
  reserved slots in the state schema, ready for new visual elements.
