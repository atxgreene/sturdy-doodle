# Image assets

Brand and architecture visuals for README, articles, and marketing
posts. Image binaries are gitignored — drop your own files at the
paths below.

## Canonical filenames (referenced from README + articles)

| Filename | Purpose | Status |
|---|---|---|
| `hero-owl-banner.png` | README header banner. Mayan-styled owl + hexagonal core orb on temple background. Tagline: "Cognitive OS for Local-First AI Agents". | Save your `Mnemosyne` hero banner here |
| `architecture-overview.png` | Full system diagram: Channels → Brain → Tool Executor + ICMS 5-tier memory + Inner Dialogue + Dream Consolidation + Meta-Harness loop + Telemetry. Used in `docs/articles/v0.8-launch-substack.md` and embedded in README under "Architecture at a glance". | Save your `Mnemosyne Agent Architecture` diagram here |
| `owl-portrait.png` | Square avatar / quote-card visual. Detailed owl head with hieroglyph border, teal eyes, dark slate. | Save your owl portrait here |
| `architecture-tier-stack.png` | **DO NOT USE as canonical architecture doc** — see "Known image issues" below. | Save here only if you want to use it for marketing-only contexts with the caveat noted. |

## Known image issues

### `architecture-tier-stack.png` — 6-layer "L1 Instinct → L6 Reflection" diagram

**Status: rejected as canonical architecture documentation. Usable only for marketing-only contexts with a caveat.**

This diagram visualizes a 6-layer model that doesn't match the v0.8.0
code:

| Diagram says | Code actually has |
|---|---|
| L1: Instinct (separate tier) | L4 with `kind="user_instinct"` (overlay, not a tier) |
| L2 omitted | L1 hot, L2 warm both exist |
| L3: Recent | L2 warm |
| L4: Core (Stable Knowledge + Identity) | L4 pattern (separate from L5 identity) |
| L5: Archival | L3 cold |
| L6: Reflection | L5 identity |

Using this image as-is in `README.md` or `docs/ARCHITECTURE.md` would
recreate the exact docs-vs-code drift problem v0.8.0 fixed (where
external LLMs were misreading our tiers as "L4=archival /
L5=meta-memory" because of ambiguous earlier docs).

**Two options if you want to use this aesthetic:**

1. Re-render the diagram against the actual L1-L5 layout shown in
   `docs/ARCHITECTURE.md`. Keep the visual style (ancient stone slabs,
   teal/orange palette, hieroglyph border). Just relabel:
   - L1: Hot (Working Memory)
   - L2: Warm (Short-Term)
   - L3: Cold (Long-Term)
   - L4: Pattern (+ user_instinct overlay)
   - L5: Identity (Human-Approved Core Values)
2. Use it in marketing-only contexts (a Substack header image,
   Instagram, etc.) with a caption: *"Conceptual rendering — see
   docs/ARCHITECTURE.md for the canonical L1-L5 layout."*

The aesthetic is great. The labels just need to match the code.

### `architecture-overview.png` — minor label nit

The big system diagram says "L4 Dreams consolidation" in the ICMS
panel. Strictly speaking, our L4 holds **patterns** (rows promoted by
`mnemosyne_compactor`); dream consolidation is the offline process
that produces them. Not wrong enough to reject — most readers will
read "Dreams" as the source of L4 content and understand. If you
re-render in the future, label it "L4 Pattern (compactor)" and add
the "Dream Consolidation" arrow flowing INTO it rather than naming
the tier after the process.

## Format guidance

- PNG preferred for README embeds (renders reliably on GitHub).
- Keep total weight under 1 MB per image. Compress with ImageOptim
  or `pngquant` before committing if needed.
- For Substack: Substack auto-resizes; upload at 1500-2000px wide
  for crisp retina display.
- For X: hero banner cropped to 1500×500 (header) and 1024×512
  (in-thread embed) work best.
