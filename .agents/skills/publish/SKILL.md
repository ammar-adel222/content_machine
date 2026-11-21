---
name: publish
description: Verify render quality, package Short/Reels metadata aligned with channel research, update channel memory, and prepare release publishing.
---

# Skill: Release Packaging & Publishing Checklists

Use this workflow once `cm render <slug>` produces `runs/<slug>/final.mp4` and updates `state.json` to `current_stage: publish`.

## Step 1: Verify All Pipeline Stages & Artifact Specs

Run status check and inspect `render_report.json`:
```powershell
cm status <slug>
```
Confirm before publishing:
- `blueprint`: `passed` (`0` errors)
- `audio`: `passed` (`-14 LUFS` target met inside `audio/timeline.json`, real Gemini TTS voiceover — **never** publish `provider == "mock"`)
- `scene`: `passed`
- `qa`: `passed` (`0` visual violations on `--mode all`)
- `render`: `passed` (`h264`, `yuv420p`, `aac`, `1080x1920` for `short_9x16`, `<= 90s` duration for Shorts/Reels)

## Step 2: Check Channel Memory & Craft Metadata

1. Inspect recent research insights and competitor patterns (`cm research show`) and catalog deduplication (`cm ideas list`).
2. Prepare publishing metadata tailored to the platform:
   - **Title (`<= 60` chars):** Clear curiosity hook matching winning patterns from `channels/research.json` (e.g., `how_to`, `why`, `contrast_vs`), avoiding clickbait mismatch.
   - **Description / Caption:** 1–2 concise technical summary sentences + 3–5 relevant topic hashtags (always include `#shorts` for YouTube Shorts).
   - **Topic / Idea Closure:** Record any new performance or format insight learned during production via `cm research insight "..."`.

## Step 3: Publish & Close Feedback Loop

- Exported deliverable sits at `runs/<slug>/final.mp4` with verified manifest `runs/<slug>/render_report.json`.
1. Link YouTube OAuth once (if `secrets/youtube_token.json` is not present):
   ```powershell
   cm auth youtube
   ```
2. Verify metadata, hashtags, and MP4 stream readiness in dry-run mode (never modifies `catalog.json` or uploads):
   ```powershell
   cm publish <slug> --dry-run
   ```
3. Publish to YouTube Shorts and/or Instagram Reels (receipt saved to `runs/<slug>/publish_receipt.json` and `channels/catalog.json`):
   ```powershell
   cm publish <slug> --platform youtube --privacy private
   # Optional Instagram Reels (requires IG_USER_ID, IG_ACCESS_TOKEN, and IG_STAGING_PROVIDER or --video-url):
   cm publish <slug> --platform youtube --platform instagram
   ```
4. After 24–72h live views accumulate, sync views, likes, and comments back into `channels/catalog.json` (`cm schema analytics`) and feed winning patterns back into `ideate`:
   ```powershell
   cm analytics sync
   ```
