/* mnemosyne_ui/static/avatar.js
 *
 * Pure SVG avatar rendered into an existing <div id="avatar-stage">.
 * No frameworks. Reads a state dict from the /avatar endpoint and
 * draws/animates accordingly.
 *
 * Visual elements (each maps to one observable agent trait — see
 * mnemosyne_avatar.py):
 *
 *   core      — central orb. Color = palette.core, brightness = health.
 *   aura      — soft outer glow. Radius = state.aura_radius.
 *               Pulse rate = state.pulses_per_minute.
 *   rings     — concentric thin circles. Count = state.rings (inner-
 *               dialogue activations, capped 8).
 *   orbiters  — small dots circling the core. One per learned skill;
 *               speed proportional to recent activity.
 *   scars     — small dim arc segments on the rim. One per identity
 *               slip. Visible reminder of past failures; fade with
 *               additional time.
 *   roots     — three downward-extending lines representing L1/L2/L3
 *               memory tier counts.
 *   eye       — opens wider on focus, narrows on rest.
 *
 * Mood phase changes the animation ambience:
 *   rest        — slow breathing, dim aura
 *   focus       — sharp eye, slight forward lean (tilt animation)
 *   explore     — orbiters speed up, hue shifts subtly
 *   consolidate — petal-like inward shimmer (dream halo)
 */

"use strict";

const NS = "http://www.w3.org/2000/svg";
const AVATAR_VIEWBOX = 500;

function el(name, attrs) {
  const node = document.createElementNS(NS, name);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      node.setAttribute(k, v);
    }
  }
  return node;
}

function clip(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }

function setCssVars(palette) {
  const r = document.documentElement.style;
  r.setProperty("--core",   palette.core);
  r.setProperty("--accent", palette.accent);
  r.setProperty("--rim",    palette.rim);
}

// ---- 8-bit pixel owl sprite (v0.9.3) ----------------------------------------
// Mirror of mnemosyne_avatar._OWL_SPRITE — any change here must be
// reflected in the Python renderer. Strict left/right symmetry.
//   .  transparent     T  ear tuft (rim)
//   F  feather primary (core)       D  dark feather (rim)
//   B  belly (core+bg mix)          E  eye iris/white
//   P  pupil (accent)               K  beak (rim)
//   C  L0 instinct chest glow (accent)
const OWL_SPRITE = [
  "..TT........TT..",
  ".TTT........TTT.",
  ".TFF........FFT.",
  "..DFFFFFFFFFFD..",
  ".DFFFFFFFFFFFFD.",
  ".FFEEEFFFFEEEFF.",
  ".FEPPEFFFFEPPEF.",
  ".FEPPEFFFFEPPEF.",
  ".FFEEEFFFFEEEFF.",
  "..FFFFFKKFFFFF..",
  "..FFFFKKKKFFFF..",
  ".FFBBBCCCCBBBFF.",
  ".FFBBBBCCBBBBFF.",
  ".FFBBBBBBBBBBFF.",
  "..FFBBBBBBBBFF..",
  "...DDFFFFFFDD...",
];

function mixHex(a, b, t) {
  if (!/^#[0-9a-f]{6}$/i.test(a) || !/^#[0-9a-f]{6}$/i.test(b)) return a;
  const ar = parseInt(a.slice(1, 3), 16);
  const ag = parseInt(a.slice(3, 5), 16);
  const ab = parseInt(a.slice(5, 7), 16);
  const br = parseInt(b.slice(1, 3), 16);
  const bg = parseInt(b.slice(3, 5), 16);
  const bb = parseInt(b.slice(5, 7), 16);
  const rr = Math.round(ar * (1 - t) + br * t);
  const rg = Math.round(ag * (1 - t) + bg * t);
  const rb = Math.round(ab * (1 - t) + bb * t);
  return "#" + [rr, rg, rb].map(n => n.toString(16).padStart(2, "0")).join("");
}

function renderPixelOwl(svg, opts) {
  const { cx, cy, coreR, palette, state } = opts;
  const cell = Math.max(3, (2 * coreR) / 16);
  const x0 = cx - cell * 8;
  const y0 = cy - cell * 8;

  const health = Math.max(0, Math.min(1, state.health ?? 1.0));
  const featherOp = 0.65 + 0.35 * health;
  const darkOp    = 0.55 + 0.40 * health;
  const mood = state.mood_phase || "rest";
  const cal = state.calibration;
  const restlessness = state.restlessness ?? 0.0;
  const pulseS = Math.max(0.6, Math.min(6.0,
    60.0 / Math.max(1, state.pulses_per_minute || 6)));

  const bellyColour = palette.core.startsWith("#")
    ? mixHex(palette.core, palette.bg, 0.55)
    : palette.core;
  const eyeBg = palette.bg.startsWith("#")
    ? mixHex(palette.bg, palette.core, 0.2)
    : palette.bg;

  const group = el("g", { id: "mnemo-owl" });

  const rect = (r, c, fill, opacity) => el("rect", {
    x: (x0 + c * cell - 0.2).toFixed(2),
    y: (y0 + r * cell - 0.2).toFixed(2),
    width: (cell + 0.4).toFixed(2),
    height: (cell + 0.4).toFixed(2),
    fill,
    "fill-opacity": opacity.toFixed(2),
    "shape-rendering": "crispEdges",
  });

  // Pass 1: non-eye, non-tuft cells
  for (let r = 0; r < OWL_SPRITE.length; r++) {
    const row = OWL_SPRITE[r];
    for (let c = 0; c < row.length; c++) {
      const ch = row[c];
      if (ch === "." || ch === "E" || ch === "P" || ch === "T") continue;
      if (ch === "F")      group.appendChild(rect(r, c, palette.core, featherOp));
      else if (ch === "D") group.appendChild(rect(r, c, palette.rim,  darkOp));
      else if (ch === "B") group.appendChild(rect(r, c, bellyColour, featherOp));
      else if (ch === "K") group.appendChild(rect(r, c, palette.rim,  0.95));
      else if (ch === "C") group.appendChild(rect(r, c, palette.accent, 0.55));
    }
  }

  // Pass 2: eye cells with mood-aware rendering
  for (let r = 0; r < OWL_SPRITE.length; r++) {
    const row = OWL_SPRITE[r];
    for (let c = 0; c < row.length; c++) {
      const ch = row[c];
      if (ch === "E") {
        if (mood === "rest") {
          // Closed: fill with feather colour so eye disappears
          group.appendChild(rect(r, c, palette.core, featherOp));
        } else {
          group.appendChild(rect(r, c, eyeBg, 0.95));
        }
      } else if (ch === "P") {
        if (mood === "rest") {
          group.appendChild(rect(r, c, palette.rim, darkOp));  // slit
        } else if (mood === "focus") {
          group.appendChild(rect(r, c, palette.accent, 1.0));
        } else {
          group.appendChild(rect(r, c, palette.accent, 0.92));
        }
      }
    }
  }

  // Pass 3: ear tufts (with optional sway animation based on restlessness)
  const tuftSway = Math.max(0, Math.min(4, restlessness * 4));
  const leftTufts  = [];
  const rightTufts = [];
  for (let r = 0; r < OWL_SPRITE.length; r++) {
    const row = OWL_SPRITE[r];
    for (let c = 0; c < row.length; c++) {
      if (row[c] === "T") {
        (c < 8 ? leftTufts : rightTufts).push([r, c]);
      }
    }
  }
  const mkTuftGroup = (cells, angleSign) => {
    if (cells.length === 0) return;
    if (tuftSway < 0.01) {
      for (const [r, c] of cells) {
        group.appendChild(rect(r, c, palette.rim, darkOp * 0.9));
      }
      return;
    }
    const g = el("g");
    const pivotC = cells.reduce((s, x) => s + x[1], 0) / cells.length;
    const pivotR = Math.max(...cells.map(x => x[0])) + 1;
    const pivotX = x0 + pivotC * cell + cell / 2;
    const pivotY = y0 + pivotR * cell;
    for (const [r, c] of cells) {
      g.appendChild(rect(r, c, palette.rim, darkOp * 0.9));
    }
    g.appendChild(el("animateTransform", {
      attributeName: "transform",
      type: "rotate",
      values: `0 ${pivotX.toFixed(2)} ${pivotY.toFixed(2)};` +
              `${(angleSign * tuftSway).toFixed(1)} ${pivotX.toFixed(2)} ${pivotY.toFixed(2)};` +
              `0 ${pivotX.toFixed(2)} ${pivotY.toFixed(2)}`,
      dur: `${Math.max(1.2, 3.0 - restlessness * 2.0).toFixed(2)}s`,
      repeatCount: "indefinite",
    }));
    group.appendChild(g);
  };
  mkTuftGroup(leftTufts,  +1);
  mkTuftGroup(rightTufts, -1);

  // Breathing animation — whole owl translates gently on the pulse
  group.appendChild(el("animateTransform", {
    attributeName: "transform",
    type: "translate",
    additive: "sum",
    values: `0 0; 0 -${(cell * 0.12).toFixed(2)}; 0 0`,
    dur: `${pulseS.toFixed(2)}s`,
    repeatCount: "indefinite",
  }));

  // Pupil drift on low calibration
  if (cal !== null && cal !== undefined) {
    const driftPx = Math.max(0, Math.min(cell * 0.25, (1.0 - cal) * cell * 0.25));
    if (driftPx > 0.1) {
      group.appendChild(el("animateTransform", {
        attributeName: "transform",
        type: "translate",
        additive: "sum",
        values: `0 0; ${driftPx.toFixed(2)} 0; 0 0; -${driftPx.toFixed(2)} 0; 0 0`,
        dur: "3.5s",
        repeatCount: "indefinite",
      }));
    }
  }

  svg.appendChild(group);
}

function buildSvg(state) {
  const svg = el("svg", {
    viewBox: `0 0 ${AVATAR_VIEWBOX} ${AVATAR_VIEWBOX}`,
    xmlns: NS,
    role: "img",
    "aria-label":
      `Mnemosyne avatar — mood ${state.mood_phase}, ` +
      `health ${(state.health * 100).toFixed(0)}%`,
  });

  const cx = AVATAR_VIEWBOX / 2;
  const cy = AVATAR_VIEWBOX / 2 + 10;
  const palette = state.palette;

  // ---- background gradient defs ----
  const defs = el("defs");
  const auraGrad = el("radialGradient", {
    id: "auraGrad", cx: "50%", cy: "50%", r: "60%",
  });
  auraGrad.appendChild(el("stop", { offset: "0%",
    "stop-color": palette.core, "stop-opacity": "0.55" }));
  auraGrad.appendChild(el("stop", { offset: "55%",
    "stop-color": palette.core, "stop-opacity": "0.18" }));
  auraGrad.appendChild(el("stop", { offset: "100%",
    "stop-color": palette.core, "stop-opacity": "0" }));
  defs.appendChild(auraGrad);

  const coreGrad = el("radialGradient", {
    id: "coreGrad", cx: "50%", cy: "45%", r: "60%",
  });
  coreGrad.appendChild(el("stop", { offset: "0%",
    "stop-color": palette.rim, "stop-opacity": "1" }));
  coreGrad.appendChild(el("stop", { offset: "55%",
    "stop-color": palette.core, "stop-opacity": "1" }));
  coreGrad.appendChild(el("stop", { offset: "100%",
    "stop-color": palette.bg, "stop-opacity": "1" }));
  defs.appendChild(coreGrad);

  svg.appendChild(defs);

  // ---- habitat: memory-tier terrain at the bottom ----
  // Three soft wave bands whose heights scale with L1/L2/L3 memory
  // counts. Thematic grounding for the avatar, not a game background.
  const totalMem = state.l1_count + state.l2_count + state.l3_count;
  if (totalMem > 0) {
    const habH = 100;
    const l1H = Math.min(habH * 0.50, (state.l1_count / totalMem) * habH * 0.9);
    const l2H = Math.min(habH * 0.70, (state.l2_count / totalMem) * habH * 0.9);
    const l3H = Math.min(habH * 0.90, (state.l3_count / totalMem) * habH * 0.9);
    const W = AVATAR_VIEWBOX, H = AVATAR_VIEWBOX;
    const wave = (h, amp) =>
      `M0,${H} L0,${H - h} Q${W * 0.3},${H - h - amp}`
      + ` ${W * 0.5},${H - h - amp / 2}`
      + ` T${W},${H - h} L${W},${H} Z`;
    svg.appendChild(el("path", {
      d: wave(l3H, 12), fill: palette.rim, "fill-opacity": 0.10,
    }));
    svg.appendChild(el("path", {
      d: wave(l2H, 8), fill: palette.core, "fill-opacity": 0.12,
    }));
    svg.appendChild(el("path", {
      d: wave(l1H, 6), fill: palette.accent, "fill-opacity": 0.15,
    }));
  }

  // ---- wisdom ring (very outer, subtle — appears only when measured) ----
  if (state.wisdom != null && state.wisdom > 0) {
    const wr = state.aura_radius * 1.95;
    const wisdom = el("circle", {
      cx, cy, r: wr,
      fill: "none",
      stroke: palette.accent,
      "stroke-opacity": 0.10 + 0.30 * state.wisdom,
      "stroke-width": 0.8,
      "stroke-dasharray": "4 6",
    });
    wisdom.appendChild(el("animateTransform", {
      attributeName: "transform",
      type: "rotate",
      from: `0 ${cx} ${cy}`,
      to: `360 ${cx} ${cy}`,
      dur: `${(80 - 60 * state.wisdom).toFixed(0)}s`,
      repeatCount: "indefinite",
    }));
    svg.appendChild(wisdom);
  }

  // ---- self_assessment rays (straight lines radiating from core) ----
  if (state.self_assessment != null) {
    const rayCount = Math.max(0, Math.min(12, Math.round(state.self_assessment * 12)));
    for (let i = 0; i < rayCount; i++) {
      const a = (i / 12) * Math.PI * 2 + Math.PI / 12;
      const r1 = state.aura_radius * 0.35;
      const r2 = state.aura_radius * 0.52;
      svg.appendChild(el("line", {
        x1: cx + r1 * Math.cos(a), y1: cy + r1 * Math.sin(a),
        x2: cx + r2 * Math.cos(a), y2: cy + r2 * Math.sin(a),
        stroke: palette.rim,
        "stroke-opacity": 0.55,
        "stroke-width": 1.1,
        "stroke-linecap": "round",
      }));
    }
  }

  // ---- restlessness: core orb jitter animation when high ----
  // (applied later to the core element; flag here)
  const restless = state.restlessness != null && state.restlessness > 0.3;

  // ---- aura ring (the breathing halo) ----
  const aura = el("circle", {
    cx, cy, r: state.aura_radius * 1.55,
    fill: "url(#auraGrad)",
    opacity: 0.85,
  });
  // Pulse via SMIL — supported in every modern browser.
  const pulseSec = clip(60 / Math.max(1, state.pulses_per_minute), 0.6, 6);
  aura.appendChild(el("animate", {
    attributeName: "opacity",
    values: "0.35;0.95;0.35",
    dur: `${pulseSec.toFixed(2)}s`,
    repeatCount: "indefinite",
  }));
  aura.appendChild(el("animateTransform", {
    attributeName: "transform",
    type: "scale",
    values: "0.96;1.04;0.96",
    additive: "sum",
    dur: `${pulseSec.toFixed(2)}s`,
    repeatCount: "indefinite",
  }));
  svg.appendChild(aura);

  // ---- inner-dialogue rings ----
  for (let i = 0; i < state.rings; i++) {
    const r = state.aura_radius * (0.65 + i * 0.12);
    const ring = el("circle", {
      cx, cy, r,
      fill: "none",
      stroke: palette.accent,
      "stroke-opacity": 0.18 + 0.06 * (state.rings - i),
      "stroke-width": 1.2,
    });
    ring.appendChild(el("animate", {
      attributeName: "stroke-opacity",
      values: "0.35;0.05;0.35",
      dur: `${(2 + i * 0.5).toFixed(2)}s`,
      repeatCount: "indefinite",
    }));
    svg.appendChild(ring);
  }

  // ---- core orb halo (behind pixel owl) ----
  const coreR = 56 + state.health * 12;
  const coreHalo = el("circle", {
    cx, cy: cy + 2, r: coreR * 0.9,
    fill: "url(#coreGrad)",
    "fill-opacity": 0.45,
    stroke: palette.rim,
    "stroke-opacity": 0.35,
    "stroke-width": 1.2,
  });
  svg.appendChild(coreHalo);

  // ---- 8-bit pixel owl (v0.9.3) ----
  //
  // Mirrors mnemosyne_avatar._render_pixel_owl (server-side Python).
  // 16x16 sprite grid, each cell a single <rect>.
  renderPixelOwl(svg, {
    cx, cy, coreR, palette, state,
    restless,
  });

  // ---- orbiters (one per learned skill, capped 12) ----
  const orbiterCount = clip(state.skills_count, 0, 12);
  const orbitR = state.aura_radius + 28;
  const orbitSpeed = clip(20 - state.activity_score * 14, 4, 20);
  const orbitGroup = el("g");
  for (let i = 0; i < orbiterCount; i++) {
    const a = (i / orbiterCount) * Math.PI * 2;
    const dot = el("circle", {
      cx: cx + orbitR * Math.cos(a),
      cy: cy + orbitR * Math.sin(a),
      r: 3,
      fill: palette.accent,
      opacity: 0.78,
    });
    orbitGroup.appendChild(dot);
  }
  // Rotate the whole group continuously
  const rot = el("animateTransform", {
    attributeName: "transform",
    type: "rotate",
    from: `0 ${cx} ${cy}`,
    to: `360 ${cx} ${cy}`,
    dur: `${orbitSpeed.toFixed(1)}s`,
    repeatCount: "indefinite",
  });
  orbitGroup.appendChild(rot);
  svg.appendChild(orbitGroup);

  // ---- scars (one short arc per identity slip, max 12) ----
  const scarCount = clip(state.identity_slip_count, 0, 12);
  for (let i = 0; i < scarCount; i++) {
    const a = (i / Math.max(1, scarCount)) * Math.PI * 2;
    const r1 = 60 + state.health * 12 + 4;
    const r2 = r1 + 4;
    const x1 = cx + r1 * Math.cos(a);
    const y1 = cy + r1 * Math.sin(a);
    const x2 = cx + r2 * Math.cos(a + 0.16);
    const y2 = cy + r2 * Math.sin(a + 0.16);
    svg.appendChild(el("line", {
      x1, y1, x2, y2,
      stroke: "#f25c6f",
      "stroke-opacity": 0.45,
      "stroke-width": 1.6,
      "stroke-linecap": "round",
    }));
  }

  // ---- memory roots (three downward lines, length proportional to tier counts) ----
  const tiers = [
    { count: state.l1_count, color: palette.accent, x: cx - 20 },
    { count: state.l2_count, color: palette.core,   x: cx },
    { count: state.l3_count, color: palette.rim,    x: cx + 20 },
  ];
  const rootBase = cy + (60 + state.health * 12) - 4;
  const maxRoot = 110;
  for (const t of tiers) {
    const len = clip(20 + Math.log10(1 + t.count) * 30, 20, maxRoot);
    const root = el("line", {
      x1: t.x, y1: rootBase,
      x2: t.x, y2: rootBase + len,
      stroke: t.color,
      "stroke-opacity": 0.55,
      "stroke-width": 2,
      "stroke-linecap": "round",
    });
    svg.appendChild(root);
  }

  // ---- consolidate-mode petals (only on dream cadence) ----
  if (state.mood_phase === "consolidate" && state.dreams_count > 0) {
    const petalCount = 6;
    for (let i = 0; i < petalCount; i++) {
      const a = (i / petalCount) * Math.PI * 2;
      const r1 = state.aura_radius * 1.1;
      const px = cx + r1 * Math.cos(a);
      const py = cy + r1 * Math.sin(a);
      const petal = el("circle", {
        cx: px, cy: py, r: 6,
        fill: palette.accent, opacity: 0.4,
      });
      petal.appendChild(el("animate", {
        attributeName: "r",
        values: "3;9;3",
        dur: `${(3 + i * 0.2).toFixed(2)}s`,
        repeatCount: "indefinite",
      }));
      svg.appendChild(petal);
    }
  }

  return svg;
}

function pct(x) { return x == null ? "—" : (x * 100).toFixed(0) + "%"; }

function renderTraitGrid(state, container) {
  const traits = [
    ["mood",            state.mood_phase],
    ["age (days)",      state.age_days.toFixed(1)],
    ["memories",        state.memory_count],
    ["skills",          state.skills_count + (state.learned_skills
                          ? ` (+${state.learned_skills} learned)` : "")],
    ["goals (open)",    state.goals_open],
    ["goals resolved",  state.goals_resolved],
    ["dreams",          state.dreams_count],
    ["inner dialogues", state.inner_dialogues],
    ["identity",        (state.identity_strength * 100).toFixed(1) + "%"],
    ["activity",        pct(state.activity_score)],
    ["health",          pct(state.health)],
    ["pulse",           state.pulses_per_minute + " bpm"],
    // AGI-scaling traits. "—" when null (honest: not yet measured).
    ["wisdom",          pct(state.wisdom)],
    ["restlessness",    pct(state.restlessness)],
    ["novelty",         pct(state.novelty)],
    ["self-assessment", pct(state.self_assessment)],
  ];
  container.innerHTML = "";
  for (const [k, v] of traits) {
    const div = document.createElement("div");
    div.className = "trait";
    div.innerHTML = `<span class="k">${k}</span><span class="v">${v}</span>`;
    container.appendChild(div);
  }
}

function renderAvatar(state) {
  setCssVars(state.palette);
  const stage = document.getElementById("avatar-stage");
  if (!stage) return;
  stage.innerHTML = "";
  stage.appendChild(buildSvg(state));
  const tg = document.getElementById("trait-grid");
  if (tg) renderTraitGrid(state, tg);
  const hint = document.getElementById("avatar-mood-hint");
  if (hint) hint.textContent = state.mood_phase;
}

window.MnemoAvatar = { render: renderAvatar };
