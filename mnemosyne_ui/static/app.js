/* mnemosyne_ui/static/app.js
 *
 * Orchestrates the dashboard:
 *   - polls /avatar every N seconds, hands state to MnemoAvatar.render
 *   - subscribes to /events_stream (SSE) to update the event list
 *   - polls /goals
 *   - renders memory tier bars from /stats
 *   - submits /turn from the chat panel
 *
 * No build step. No frameworks. No dependencies.
 */

"use strict";

const POLL_AVATAR_MS = 4000;
const POLL_STATS_MS  = 5000;
const POLL_GOALS_MS  = 8000;
const MAX_EVENT_ROWS = 80;

const tokenKey = "mnemosyne.token";
const authHeader = () => {
  const tok = localStorage.getItem(tokenKey);
  return tok ? { "Authorization": `Bearer ${tok}` } : {};
};

async function jget(url) {
  const r = await fetch(url, { headers: { ...authHeader() } });
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}
async function jpost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`${url} → ${r.status} ${text.slice(0, 80)}`);
  }
  return r.json();
}

// ---- avatar polling --------------------------------------------------------

async function refreshAvatar() {
  try {
    const state = await jget("/avatar");
    if (window.MnemoAvatar) MnemoAvatar.render(state);
    updatePills(state);
  } catch (e) {
    setStatus("avatar", "error", `avatar: ${e.message}`);
  }
}

function updatePills(state) {
  const set = (id, text, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = `pill ${cls || ""}`.trim();
  };
  const moodCls = state.mood_phase === "rest" ? "" :
                  state.mood_phase === "consolidate" ? "ok" : "";
  set("pill-mood",     state.mood_phase, moodCls);
  set("pill-mem",      `memory: ${state.memory_count}`);
  set("pill-skills",   `skills: ${state.skills_count}`);
  set("pill-goals",    `goals: ${state.goals_open} open`);
  const idPct = (state.identity_strength * 100).toFixed(1);
  const idCls = state.identity_strength > 0.95 ? "ok"
              : state.identity_strength > 0.85 ? "warn"
              : "error";
  set("pill-identity", `identity: ${idPct}%`, idCls);
}

// ---- chat ------------------------------------------------------------------

function chatBubble(role, text, opts = {}) {
  const log = document.getElementById("chat-log");
  const wrap = document.createElement("div");
  wrap.className = `chat-msg ${role}`;
  const bub = document.createElement("div");
  bub.className = `chat-bubble ${opts.className || ""}`;
  bub.textContent = text;
  wrap.appendChild(bub);
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
  return bub;
}

async function submitTurn(text, opts = {}) {
  chatBubble("user", text);
  const thinking = chatBubble("assistant", "thinking", { className: "thinking" });
  try {
    const meta = opts.hard ? { tags: ["hard"] } : {};
    const resp = await jpost("/turn", {
      user_message: text, metadata: meta,
    });
    if (resp.error) {
      thinking.classList.remove("thinking");
      thinking.textContent = `(error: ${resp.error.type || "unknown"})`;
    } else {
      thinking.classList.remove("thinking");
      thinking.textContent = resp.text || "(empty response)";
    }
  } catch (e) {
    thinking.classList.remove("thinking");
    thinking.textContent = `(network: ${e.message})`;
  }
  // Refresh state immediately so the avatar reflects the new turn
  refreshAvatar();
}

function wireChatForm() {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const hard = document.getElementById("hard-toggle");
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    submitTurn(text, { hard: hard.checked });
  });
  // Shift+Enter newline; Enter sends.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
}

// ---- event stream ----------------------------------------------------------

function setStatus(_kind, cls, text) {
  const el = document.getElementById("events-status");
  if (!el) return;
  el.textContent = text;
  el.classList.remove("warn", "error", "ok");
  if (cls) el.classList.add(cls);
}

function appendEventRow(evt) {
  const ul = document.getElementById("event-stream");
  if (!ul) return;
  const li = document.createElement("li");
  li.className = (evt.status === "error") ? "error" : "ok";
  const ts = (evt.timestamp_utc || "").slice(11, 19);
  const type = evt.event_type || "?";
  const detail = evt.tool || (evt.metadata && (evt.metadata.persona ||
                                                  evt.metadata.cluster_key))
                  || (evt.error && evt.error.type) || "";
  li.innerHTML = `<span>${ts}</span><span><span class="evt-type">`
               + `${type}</span> ${detail || ""}</span>`;
  ul.insertBefore(li, ul.firstChild);
  while (ul.children.length > MAX_EVENT_ROWS) ul.removeChild(ul.lastChild);
}

function startEventStream() {
  // Use SSE. EventSource doesn't honor custom headers, so token-protected
  // servers fall back to /recent_events polling below.
  if (localStorage.getItem(tokenKey)) {
    setStatus("events", "warn", "polling (token mode)");
    pollRecentEvents();
    return;
  }
  let es;
  try {
    es = new EventSource("/events_stream");
  } catch (e) {
    setStatus("events", "error", `sse: ${e.message}`);
    pollRecentEvents();
    return;
  }
  es.onopen = () => setStatus("events", "ok", "live");
  es.onerror = () => {
    setStatus("events", "warn", "reconnecting");
  };
  es.onmessage = (ev) => {
    try {
      const obj = JSON.parse(ev.data);
      appendEventRow(obj);
    } catch (_) {}
  };
}

let lastSeenEventTs = "";
async function pollRecentEvents() {
  try {
    const r = await jget("/recent_events?limit=20");
    for (const evt of (r.events || [])) {
      if (!evt.timestamp_utc || evt.timestamp_utc <= lastSeenEventTs) continue;
      lastSeenEventTs = evt.timestamp_utc;
      appendEventRow(evt);
    }
    setStatus("events", "ok", "polling");
  } catch (e) {
    setStatus("events", "error", `events: ${e.message}`);
  }
  setTimeout(pollRecentEvents, 3000);
}

// ---- memory bars -----------------------------------------------------------

async function refreshMemory() {
  try {
    const stats = await jget("/stats");
    const tiers = (stats.memory && stats.memory.by_tier) || {};
    const l1 = tiers.L1_hot || 0;
    const l2 = tiers.L2_warm || 0;
    const l3 = tiers.L3_cold || 0;
    const max = Math.max(1, l1, l2, l3);
    const bars = document.getElementById("memory-bars");
    bars.innerHTML = "";
    for (const [label, n, cls] of [["L1", l1, "l1"],
                                      ["L2", l2, "l2"],
                                      ["L3", l3, "l3"]]) {
      const row = document.createElement("div");
      row.className = `memory-row ${cls}`;
      row.innerHTML = `<span class="label">${label}</span>`
                    + `<div class="bar"><span style="width:`
                    + `${(n / max * 100).toFixed(1)}%"></span></div>`
                    + `<span class="count">${n}</span>`;
      bars.appendChild(row);
    }
    document.getElementById("memory-numbers").innerHTML =
      `total: ${stats.memory && stats.memory.total || 0} · `
      + `fts5: ${stats.memory && stats.memory.fts5_enabled ? "yes" : "no"} · `
      + `runs: ${(stats.brain && stats.brain.turns_total) || 0}`;
  } catch (e) {
    // memory panel is best-effort
  }
}

// ---- goals -----------------------------------------------------------------

async function refreshGoals() {
  try {
    const r = await jget("/goals");
    const list = document.getElementById("goal-list");
    list.innerHTML = "";
    for (const g of (r.goals || [])) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="pri p${g.priority}">P${g.priority}</span>`
                   + `<span class="text">${escapeHtml(g.text)}</span>`
                   + `<button class="resolve" data-id="${g.id}">resolve</button>`;
      list.appendChild(li);
    }
    list.querySelectorAll(".resolve").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await jpost("/goals", { op: "resolve", id: parseInt(btn.dataset.id, 10) });
          refreshGoals();
        } catch (e) { console.warn(e); }
      });
    });
  } catch (e) {
    // goals panel is best-effort
  }
}

function wireGoalForm() {
  const form = document.getElementById("goal-form");
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = document.getElementById("goal-input").value.trim();
    if (!text) return;
    const priority = parseInt(document.getElementById("goal-priority").value, 10);
    try {
      await jpost("/goals", { op: "add", text, priority });
      document.getElementById("goal-input").value = "";
      refreshGoals();
    } catch (e) { console.warn(e); }
  });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ---- memory browser --------------------------------------------------------

async function searchMemory() {
  const q = document.getElementById("mb-input").value.trim();
  const tier = document.getElementById("mb-tier").value;
  const results = document.getElementById("mb-results");
  results.innerHTML = `<li style="color:var(--text-dim);padding:6px 0">searching…</li>`;
  const url = new URL("/memory/search", window.location.origin);
  if (q) url.searchParams.set("q", q);
  url.searchParams.set("limit", "30");
  if (tier) url.searchParams.set("tier_max", tier);
  try {
    const r = await jget(url.pathname + url.search);
    results.innerHTML = "";
    const hits = r.hits || [];
    if (hits.length === 0) {
      results.innerHTML = `<li style="color:var(--text-dim);padding:6px 0">no hits</li>`;
      return;
    }
    for (const h of hits) {
      const li = document.createElement("li");
      const ts = (h.created_utc || "").slice(0, 10);
      li.innerHTML =
        `<span class="mb-tier t${h.tier}">L${h.tier}</span>`
        + `<span class="mb-kind">${escapeHtml(h.kind || "")}</span>`
        + `<span class="mb-content">${escapeHtml(h.content || "")}</span>`
        + `<span class="mb-meta">${ts} · ×${h.access_count || 0}</span>`;
      results.appendChild(li);
    }
  } catch (e) {
    results.innerHTML = `<li style="color:var(--error);padding:6px 0">error: ${escapeHtml(e.message)}</li>`;
  }
}

function wireMemoryBrowser() {
  const form = document.getElementById("mb-form");
  if (!form) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    searchMemory();
  });
}

// ---- bootstrap -------------------------------------------------------------

function startPolling(fn, interval) {
  fn();
  setInterval(fn, interval);
}

document.addEventListener("DOMContentLoaded", () => {
  wireChatForm();
  wireGoalForm();
  wireMemoryBrowser();
  startPolling(refreshAvatar, POLL_AVATAR_MS);
  startPolling(refreshMemory, POLL_STATS_MS);
  startPolling(refreshGoals,  POLL_GOALS_MS);
  startEventStream();
});
