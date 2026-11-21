# Content Machine 3.0

An agent-native content production harness built for coding agents (**Antigravity**, **Claude Code**, **Codex**) rather than brittle LLM-wrapper pipelines.

## Core Format

**2D AI/CS "Build From Scratch -> Stress-Test -> Break -> Upgrade"**

- Starting with **`9:16` short-form** (45–85s ideal for YouTube Shorts & Instagram Reels) so hooks, pacing, and visual quality iterate rapidly.
- Structured with layout presets to expand cleanly to **`16:9` long-form** (`1920x1080`, 5+ iterations).

## Stack Overview

- **Agent Brain & Portable Skills:** Read [AGENTS.md](./AGENTS.md) and [CLAUDE.md](./CLAUDE.md), plus the 5 portable workflows inside `.agents/skills/` (`ideate`, `blueprint`, `build-scene`, `visual-qa`, `publish`).
- **Research & Competitor Intelligence (`cm research`):** Zero-key autocomplete + HN + Reddit demand feeds (`cm research trends`) and optional YouTube Data API v3 Shorts outlier scanner + hook transcript extraction (`cm research youtube`).
- **Voiceover (`cm tts`):** Gemini TTS (`google-genai`, strongest-first cascade with SHA-256 clip caching) + offline `--provider mock` for deterministic layout testing.
- **Sound Design (`cm sfx`, `cm audio`):** Semantic SFX cue slots (`assets/sfx/manifest.json`) backed by procedural WAV synth + FFmpeg SFX mixing, music sidechain ducking, and `-14 LUFS` mastering.
- **2D Visual Engine (`engine/` & `cm snapshot`):** Deterministic HTML5 / SVG virtual clock rendered cleanly inside mobile safe zones, audited across start/mid/end keyframes with Chromium (`cm snapshot --mode all`).
- **Video Renderer (`cm render`):** Parallel headless Chromium chunk capture piped into FFmpeg H.264 (`yuv420p`) / AAC MP4 (`30fps` preview / `60fps` final).
- **Auto-Publishing & Analytics (`cm auth`, `cm publish`, `cm analytics sync`):** YouTube OAuth 2.0 PKCE upload (`private`/`unlisted`/`public`), Instagram Reels Graph API + pure-stdlib S3/R2 SigV4 staging, and analytics feedback into `channels/catalog.json`.

## Quickstart

```powershell
# 1. Research demand & outliers
.\.venv\Scripts\cm.exe research trends --sources youtube,google,hn,reddit --limit 10
.\.venv\Scripts\cm.exe research youtube "how rate limiters work" --limit 15 --sort outlier

# 2. Validate, synthesize voice+SFX, snapshot QA, and render an example Short
.\.venv\Scripts\cm.exe validate examples/rate_limiter.blueprint.json
.\.venv\Scripts\cm.exe new rate-limiter --from-blueprint examples/rate_limiter.blueprint.json
.\.venv\Scripts\cm.exe tts rate-limiter --provider auto
.\.venv\Scripts\cm.exe snapshot rate-limiter --mode all
.\.venv\Scripts\cm.exe render rate-limiter --quality preview --fps 30

# 3. Authenticate YouTube, dry-run or publish, and sync analytics
.\.venv\Scripts\cm.exe auth youtube
.\.venv\Scripts\cm.exe publish rate-limiter --dry-run
.\.venv\Scripts\cm.exe publish rate-limiter --platform youtube --privacy private
.\.venv\Scripts\cm.exe analytics sync
```

Reference examples:
- [password_system.blueprint.json](./examples/password_system.blueprint.json) & [password_system.scene.html](./examples/password_system.scene.html)
- [rate_limiter.blueprint.json](./examples/rate_limiter.blueprint.json) & [rate_limiter.scene.html](./examples/rate_limiter.scene.html)

## Roadmap

See [ROADMAP.md](./ROADMAP.md) for the milestone build plan.
