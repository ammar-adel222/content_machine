# Content Machine 3.0

An agent-native content production harness built for coding agents (**Antigravity**, **Claude Code**, **Codex**) rather than brittle LLM-wrapper pipelines.

## Core Format

**2D AI/CS "Build From Scratch → Stress-Test → Break → Upgrade"**

- Starting with **`9:16` short-form** (45–90s for YouTube Shorts & Instagram Reels) so hooks, pacing, and visual quality can be iterated rapidly.
- Structured with layout presets to expand cleanly to **`16:9` long-form** later.

## Stack Overview

- **Agent Brain & Workflows:** Coding agents handle research, scripting, scene coding, and visual QA via `AGENTS.md`, `CLAUDE.md`, and portable `.agents/skills/`.
- **Voiceover:** Gemini TTS (`google-genai`) synthesized per beat with content-hash caching.
- **Sound Design:** Semantic SFX cue slots (`assets/sfx/manifest.json`) backed by a built-in procedural synth starter pack + drop-in CC0/royalty-free WAV overrides, mixed and auto-ducked in FFmpeg to `-14 LUFS`.
- **2D Visual Engine:** Deterministic HTML5 / SVG / Canvas virtual clock rendered frame-by-frame via headless Chromium (Playwright) and encoded to H.264/AAC MP4 with FFmpeg.
- **Publishing & Memory:** YouTube Data API v3 + Instagram Graph API (Reels), with topic deduplication and publish receipts stored in `channels/catalog.json`.

## Roadmap

See [ROADMAP.md](./ROADMAP.md) for the milestone build plan.
