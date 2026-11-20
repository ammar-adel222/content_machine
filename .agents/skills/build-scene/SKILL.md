---
name: build-scene
description: Implement deterministic, non-overlapping 2D dark-blueprint SVG visuals inside runs/<slug>/scene.html using window.CM.primitives and runtime timing context.
---

# Skill: Building Deterministic 2D Scenes (`scene.html`)

Use this workflow after `cm validate <slug>` and `cm tts <slug>` pass.

## Step 1: Scaffold `scene.html`

```powershell
cm scene init <slug>
```
This copies your `blueprint.json` and `audio/timeline.json` into `runs/<slug>/scene.html` and wires `tokens.css`, `runtime.js`, and `primitives.js`. Study `examples/password_system.scene.html` and `examples/rate_limiter.scene.html` for reference layouts.

## Step 2: Canvas Geometry & Safe Zones

Your drawing code renders inside `<svg id="cm-stage-svg">` managed automatically below the top HUD/headline band and above kinetic captions:

- **`short_9x16` (`1080x1920` output):**
  - `stageViewBox`: **`856 x 840`** (`x: 0..856`, `y: 0..840`, outer safe margins already cropped around HUD/captions: `top 160`, `bottom 360`, `left 64`, `right 160`).
  - Keep all shapes cleanly within `x: [16, 840]` and `y: [16, 824]` with breathing room.
  - Minimum readable font size in full frame is `28px` (HUD/captions handled by runtime; keep custom SVG labels concise and readable — `primitives.js` already sizes text appropriately).
- **`long_16x9` (`1920x1080` output):**
  - `stageViewBox`: **`1760 x 560`** (`x: 0..1760`, `y: 0..560`).

## Step 3: `renderStage(ctx)` Context & Determinism Rules

Runtime calls `renderStage(ctx)` for arbitrary seek timestamps `t` during parallel Chromium rendering:
- `ctx.svg` — cleared/managed SVG root (always call `P.clear(ctx.svg)` at top of `renderStage`).
- `ctx.t` — global timestamp in seconds (`>= 0`).
- `ctx.beat` — current beat object (`ctx.beat.id`, `kind`, `version_label`, `system_status`, ...).
- `ctx.beatIndex` — `0`-based beat index.
- `ctx.beatProgress` (`0..1`) — normalized progress inside current beat (`speechSpan` version is `ctx.speechProgress`).
- `ctx.CM.math` — `clamp`, `lerp`, `invLerp`, `remap`, `easeOutCubic`, `easeInOutCubic`, `easeOutExpo`, `easeOutBack`, `spring`.

**Strict rules:**
- **Never** use `Math.random()`, `Date.now()`, `setTimeout`, `requestAnimationFrame`, or CSS transitions/animations inside `renderStage`. All motion must derive purely from `ctx.t` and `ctx.beatProgress`.
- Every beat MUST visually mutate the stage clearly (`>= 0.5%` pixel diff between adjacent beats inside `cm-stage-svg`, checked automatically by `cm snapshot`).
- Never overlap node rectangles, gauges, table cards, or `failureStamp` banners unintentionally — offset vertically/horizontally (`y + h + gap >= next_y`) or place `failureStamp` as an intentional angled badge over an affected card's center without burying critical gauges below it.

## Step 4: `window.CM.primitives` Reference

- `P.clear(svg)` — Clears SVG and installs arrow markers (`#cm-arrow-<accent>`) and glow filter.
- `P.node(svg, { x, y, w, h, title, subtitle?, badge?, icon?, accent?, opacity?, scale?, pulse? })`
  - Accents: `"cyan"`, `"emerald"`, `"amber"`, `"crimson"`, `"violet"`, `"neutral"`.
  - Icons: `"user"`, `"database"`, `"shield"`, `"lock"`, `"unlock"`, `"hash"`, `"dice"`, `"gpu"`, `"cpu"`, `"attacker"`, `"warning"`, `"server"`.
- `P.pipe(svg, { from: {x,y}, to: {x,y}, accent?, progress?, curve?, strokeWidth?, dashed?, arrow?, label? })`
- `P.packetFlow(svg, { from: {x,y}, to: {x,y}, t, count?, speed?, accent?, radius?, blockedAt?, curve? })`
  - Pass `blockedAt` (e.g., `0.78`) during dropped-request / firewall / rate-limit beats to show packets colliding/bouncing cleanly with an `X` marker!
- `P.tableCard(svg, { x, y, w, title, columns, rows, highlightRows?, cracked?, accent?, opacity? })`
- `P.gauge(svg, { x, y, w, label, valueText, ratio, accent?, opacity? })`
- `P.codeCard(svg, { x, y, w, title, lines, highlightLine?, accent?, opacity? })`
- `P.failureStamp(svg, { text, x, y, progress?, angle?, accent? })`
- `P.shieldAura(svg, { x, y, w, h, t, progress?, accent? })`

## Step 5: Interactive Preview Check

```powershell
cm preview <slug>
```
Scrub across beat transitions using the timeline bar and safe-zone overlay (`S` toggle) before running automated `cm snapshot`.
