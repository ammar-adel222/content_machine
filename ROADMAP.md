# Content Machine 3.0 — Implementation Roadmap

Each milestone is designed to be verifiable on its own and committed/pushed upon completion.

1. **Milestone 1 — Core Harness Skeleton & Blueprint Validator**
   - Python 3.13 `.venv`, `pyproject.toml` (`cm` CLI), file-backed state machine (`runs/<slug>/state.json`), and `channels/catalog.json`.
   - Pydantic schema & retention linter (`cm new`, `cm status`, `cm validate`) enforcing Hook + Build → Stress-Test → Fail → Upgrade chains.

2. **Milestone 2 — 2D Dark-Blueprint Visual Engine (`engine/`)**
   - Deterministic `runtime.js` (`window.__HARNESS__.seek(t)` + `?preview=1` interactive scrubber).
   - `tokens.css` (`short_9x16` + future `long_16x9` safe-zone presets).
   - `primitives.js` (CS/AI nodes, animated packet flows, load/latency gauges, version HUD, breach stamps + camera shake, synced kinetic captions).

3. **Milestone 3 — Visual QA & Safe-Zone Snapshotter (`cm snapshot`)**
   - Headless Chromium (Playwright) keyframe capture per beat + contact sheet grid.
   - Automated DOM audit checking text legibility (`>= 28px`), YouTube Shorts / IG Reels UI safe-zone collisions, clipping, and beat-to-beat visual change.

4. **Milestone 4 — Gemini TTS, Procedural SFX & Audio Mastering (`cm tts`, `cm sfx`, `cm audio`)**
   - Per-beat Gemini TTS (`google-genai`) with SHA-256 caching + offline mock TTS mode for testing, outputting exact `timeline.json` beat boundaries.
   - Semantic SFX registry + procedural WAV generator (`cm sfx init`), FFmpeg SFX mixing + music auto-ducking + `-14 LUFS` normalization.

5. **Milestone 5 — Parallel Video Renderer (`cm render`)**
   - Parallel headless Chromium chunk capture piped into FFmpeg H.264/AAC MP4 (`1080×1920`, 30fps preview / 60fps final) muxed with mastered audio.

6. **Milestone 6 — Portable Agent Skills & End-to-End Prototype Short**
   - `AGENTS.md`, `CLAUDE.md`, and `.agents/skills/` (`ideate`, `blueprint`, `build-scene`, `visual-qa`, `publish`).
   - Produce and verify 1 complete flagship prototype Short end-to-end.

7. **Milestone 7 — Auto-Publishing (YouTube Shorts & Instagram Reels)**
   - `cm auth youtube`, `cm publish <slug> --dry-run` / live upload via YouTube Data API v3 + Instagram Graph API (with pluggable staging uploader) + `channels/catalog.json` publish receipts.
