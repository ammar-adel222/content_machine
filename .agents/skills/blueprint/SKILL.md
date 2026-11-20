---
name: blueprint
description: Author, validate, and audio-synthesize a Content Machine 3.0 blueprint.json following retention, pacing, SFX, and version-progression rules.
---

# Skill: Blueprint Authoring & Audio Synthesis

Use this workflow after initializing a run (`cm ideas pick <slug>` or `cm new <slug>`).

## Step 1: Inspect the Contract Schema

Run `cm schema blueprint` (or reference `examples/password_system.blueprint.json` / `examples/rate_limiter.blueprint.json`) so every field matches Pydantic contracts.

Valid `topic_domain` values: `cs`, `ai`, `systems`, `security`, `networking`, `databases`.
Valid `format` values: `short_9x16` (`30–95s`, recommended `45–85s`, `>= 3` iterations) or `long_16x9` (`150–900s`, recommended `240–600s`, `>= 5` iterations).
Valid `sfx` cues: `pop_build`, `click_ui`, `whoosh`, `tick_data`, `riser_stress`, `pulse_load`, `impact_fail`, `glitch_error`, `alarm_breach`, `stamp_hit`, `chime_fix`, `lock_upgrade`.

## Step 2: Enforce Retention & Validator Rules

Every item below is enforced by `cm validate <slug>` (`cm/validators.py`):

1. **Hook (`beats[0].kind == "hook"`):**
   - Must land cleanly within **`<= 4.0s`** (`<= 10 words` at `165` WPM; > `4.0s` warns, > `6.0s` fails).
   - **Never** open with `"Hey guys"`, `"What's up"`, `"Welcome back"`, `"In this video"`, `"Today we're going to"`, `"Let's talk about"`, or `"Have you ever wondered what X is"`. Open with stakes, paradox, or an immediate failing premise.
2. **Initial Build & Evolution Loop:**
   - `beats[1].kind` must be `"build"` (`v1.0`).
   - Every `"fail"` beat MUST have:
     - `failure_stamp`: crisp uppercase `1–28` chars (aim `<= 18` chars so stamp SVG pops on mobile).
     - Preceding `"stress_test"` beat **OR** non-empty `stress_test_trigger`.
     - `system_status`: `"compromised"` or `"degraded"`.
     - Subsequent `"upgrade"` or `"payoff"` beat incrementing `version_label` (e.g., `v1.0 -> v2.0 -> v3.0 -> v4.0`).
   - Final beat must have `kind == "payoff"`.
3. **Mobile Readability & Pacing:**
   - `on_screen_headline`: `<= 6` words AND `<= 42` chars (aim `<= 28` chars for clean single-line `72px` fit).
   - Beat duration (`short_9x16`): keep each beat `<= 9.0s` (`> 9.0s` warns, `> 13.0s` fails); keep sentences punchy (`<= 28` words per sentence).
   - `visual_diff`: every beat after beat 0 must specify a **distinct, non-empty** visual mutation (`visual_state` and `visual_diff` must both change).
   - `voice`: set `voice_name` (e.g. `"Charon"`) and concise `style_prompt` inside `blueprint.json` — never in `.env`.
   - Remove every `"TODO"` marker.

## Step 3: Validate Until `0` Errors & `0` Warnings

```powershell
cm validate <slug>
```
Do **not** proceed to TTS or scene coding until `cm validate <slug>` reports `[PASS]` with `0 error(s)`. Aim for `0 warning(s)`.

## Step 4: Synthesize Voiceover & Mastered Audio

Run TTS before finalizing visual animations so `scene.html` inherits exact spoken beat durations:
```powershell
cm tts <slug> --provider auto
```
- Uses `GEMINI_API_KEY` from `.env` with automatic strongest-first cascade (`gemini-3.8-flash-tts -> ...`), caches every beat by SHA-256 hash, mixes semantic SFX cues, normalizes to `-14 LUFS`, and writes `runs/<slug>/audio/timeline.json` + `master.wav`.
- Offline layout prototyping without an API key: `cm tts <slug> --provider mock` (remember to re-run `--provider auto` before final render!).
- If you edit any beat's `narration` or `pause_after_ms` later, re-run `cm validate <slug>` and `cm tts <slug> --provider auto`.
