# AGENTS.md — Content Machine 3.0 Harness Guide

You are working inside **Content Machine 3.0**, an agent-native 2D AI/CS video production harness built for coding agents (**Antigravity**, **Claude Code**, **Codex**). You handle research, ideation, blueprint scripting, 2D animated scene coding, Playwright visual QA, and publishing directly through the `cm` CLI — without fragile LLM-wrapper orchestration.

## Core Format & Storytelling Loop

Every video follows **2D AI/CS "Build From Scratch -> Stress-Test -> Break -> Upgrade"**:
- Open with a curiosity/paradox **Hook** (`<= 4.0s`, no generic filler openers).
- Build a minimal **v1.0** system (`build`), apply an explicit stress-test, watch it break loudly (`fail` with crisp `failure_stamp`), then **Upgrade** (`v2.0 -> v3.0 -> v4.0`), repeating at least **3** failure->upgrade cycles for `short_9x16` (**45–85s** ideal, hard bounds 30–95s) or **5+** for `long_16x9`.
- Keep one **persistent subject / architecture diagram** evolving across beats rather than throwing away the visual metaphor every beat.
- End on a punchy engineering **Payoff** summarizing the resilient architecture.

## Repository Map

- `cm/` — CLI & core Python harness (`cli.py`, `models.py`, `validators.py`, `catalog.py`, `state.py`, `research.py`, `scene.py`, `snapshot.py`, `sfx.py`, `tts.py`, `audio.py`, `renderer.py`, `paths.py`)
- `engine/` — Deterministic browser runtime (`tokens.css`, `runtime.js`, `primitives.js`) exposing `window.__HARNESS__.seek(t)` and interactive `?preview=1`
- `assets/sfx/` — Semantic SFX cue slots (`manifest.json`) + procedural/CC0 WAV cues (`cm sfx init`)
- `assets/music/` — Optional background music bed (`bed_default.wav` or `--music ...`) auto-ducked under voiceover
- `channels/` — Channel memory (`catalog.json` idea backlog + deduplication, `research.json` competitor & trend playbook)
- `examples/` — Reference blueprints & scenes (`password_system.*`, `rate_limiter.*`)
- `runs/<slug>/` — Per-video workspaces (`state.json`, `blueprint.json`, `audio/`, `scene.html`, `qa/`, `final.mp4`, `render_report.json`; gitignored)
- `.agents/skills/` — Portable step-by-step agent runbooks (`ideate`, `blueprint`, `build-scene`, `visual-qa`, `publish`)

## Canonical CLI Pipeline (`cm`)

Invoke CLI commands via `.venv\Scripts\cm.exe` (Windows) or `.venv/bin/cm` (Linux/macOS):

1. **Research & Ideation:**
   - `cm research trends --sources youtube,google,hn,reddit --limit 10`
   - `cm research youtube "<query>" --limit 15 --sort outlier` *(requires `YOUTUBE_API_KEY`)*
   - `cm research show` / `cm research insight "<rule>"`
   - `cm ideas list` / `cm ideas add --title ... --domain ... --hook ... --failure ...`
   - `cm ideas pick <id-or-slug>` *(or `cm new <slug> --title "..." --domain <domain>`)*
2. **Blueprint & Audio Synthesis:**
   - Edit `runs/<slug>/blueprint.json` (`cm schema blueprint` prints the JSON Schema)
   - `cm validate <slug>` *(must pass with `0` errors before TTS or scene scaffolding)*
   - `cm tts <slug> --provider auto` *(uses `GEMINI_API_KEY` with strongest-first model cascade, caches per beat, masters SFX+voice to `-14 LUFS`, and injects measured timings into `scene.html`; use `--provider mock` offline only for layout checks)*
3. **2D Scene Coding & Interactive Preview:**
   - `cm scene init <slug>` *(scaffolds `runs/<slug>/scene.html` with synced timings if `tts` already ran)*
   - Edit `runs/<slug>/scene.html` using `window.CM.primitives` + pure deterministic `ctx.t` / `ctx.beatProgress`
   - `cm preview <slug>` *(opens `?preview=1` interactive scrubber)*
4. **Visual QA & Rendering:**
   - `cm snapshot <slug> --mode all` *(captures 3 frames per beat = start/mid/end, builds `qa/contact_sheet.png`, audits DOM overlap, safe zones, font sizes, and visual diff >= `0.5%`)*
   - **Visually inspect** `runs/<slug>/qa/contact_sheet.png` before rendering! Fix any crowding or layout bugs and re-run `cm snapshot <slug> --mode all`.
   - `cm render <slug> --quality preview` *(30fps quick check)* -> `cm render <slug> --quality final` *(60fps H.264 `yuv420p` + AAC `192k`)*
5. **Publishing & Analytics:**
   - `cm auth youtube` *(browser OAuth 2.0 PKCE flow -> `secrets/youtube_token.json`)*
   - `cm auth instagram` *(interactive Meta Graph API wizard -> `.env`, or `cm auth instagram --check` to verify link)*
   - `cm publish <slug> --dry-run` -> `cm publish <slug> --platform youtube --privacy private` *(also `--platform instagram`, which uploads local MP4s directly via `rupload.facebook.com` by default; `--video-url` or R2/S3 optional)*
   - `cm analytics sync` *(pulls live views/likes/comments into `channels/catalog.json`)*
6. **Status & Schemas:**
   - `cm status` / `cm status <slug>`
   - `cm schema <blueprint|state|catalog|qa|timeline|render|research|publish|analytics>`

## Non-Negotiable Rules

1. **Secrets & Environment Isolation:** Never commit `.env`, `secrets/`, or API keys, and never inspect or echo `.env` secrets into logs. Do **not** configure LLM/TTS model names, voice names, or tone in `.env` — TTS model selection stays in code (`cm/tts.py` strongest-first fallback cascade) and voice/tone live per-video inside `blueprint.json`.
2. **No Silent Visuals or Stale Timings:** Always run `cm validate <slug>` before TTS, re-run `cm tts <slug>` after editing narration/pauses, and run `cm snapshot <slug> --mode all` + open `contact_sheet.png` before `cm render <slug>`.
3. **Strict Determinism in Scenes:** `renderStage(ctx)` must be a pure function of `ctx.t`, `ctx.beatProgress`, `ctx.speechProgress`, and `ctx.beat`. Never use `Math.random()`, `Date.now()`, `setTimeout`, or CSS transitions inside scenes; parallel Chromium workers seek arbitrary frames out of order.
4. **Harness Code Verification:** If you modify files inside `cm/` or `engine/`, always run both before committing:
   - `ruff check cm tests` (`0` errors)
   - `pytest -v --cov=cm --cov-branch --cov-fail-under=95`

## Portable Skills (`.agents/skills/`)

| Skill | When to Use | Path |
| :--- | :--- | :--- |
| `ideate` | Research outlier Shorts, mine trend curiosity feeds, deduplicate & pick topics | [.agents/skills/ideate/SKILL.md](.agents/skills/ideate/SKILL.md) |
| `blueprint` | Author & validate a 9:16 (or 16:9) Build -> Fail -> Upgrade blueprint and generate TTS | [.agents/skills/blueprint/SKILL.md](.agents/skills/blueprint/SKILL.md) |
| `build-scene` | Implement deterministic animated 2D dark-blueprint SVG visuals in `scene.html` | [.agents/skills/build-scene/SKILL.md](.agents/skills/build-scene/SKILL.md) |
| `visual-qa` | Run Chromium keyframe QA, inspect contact sheets, fix layout faults, and render MP4 | [.agents/skills/visual-qa/SKILL.md](.agents/skills/visual-qa/SKILL.md) |
| `publish` | Verify render report, metadata, catalog receipts, and prepare Shorts/Reels upload | [.agents/skills/publish/SKILL.md](.agents/skills/publish/SKILL.md) |
