---
name: visual-qa
description: Run Playwright visual QA keyframe snapshots, inspect contact sheets visually, resolve layout/static/safe-zone faults, and render H.264/AAC MP4 output.
---

# Skill: Visual QA & MP4 Rendering

Never render a final video blind. Always run Chromium keyframe QA and visually inspect the contact sheet first.

## Step 1: Capture All Beat Keyframes & Audit DOM

Run `all` mode (start, mid, and end frames of every beat = `3 x num_beats` frames):
```powershell
cm snapshot <slug> --mode all --safe-guides
```
Look for:
- `[PASS]` with `0 violation(s)`
- No `RUNTIME_ERROR`, `CONSOLE_ERROR`, or `PAGE_ERROR`
- No `SAFE_ZONE_OVERFLOW`, `TEXT_TOO_SMALL`, `ELEMENT_CLIPPED`, or `BLANK_FRAME`
- Every adjacent beat transition shows **`PASS`** (`diff_pct >= 0.5%` inside the SVG stage)

Need machine-readable JSON? Pass `--json` (see `cm schema qa`).

## Step 2: Inspect the Contact Sheet Visually

Open and inspect `runs/<slug>/qa/contact_sheet.png` (as well as any individual frame in `runs/<slug>/qa/frames/` that looks tight). Check with human eyes:
- Are node titles, subtitles, badges, and pipe labels sharp, centered, and non-overlapping?
- Do animated packets stay cleanly between nodes rather than colliding with labels?
- Does the crimson failure stamp read clearly without hiding critical numbers?
- Does the caption band stay completely clear of stage diagrams and phone safe zones?

## Step 3: Iterate Until Zero Violations

If `cm snapshot` fails OR your visual inspection catches crowding, clipping, or weak animation contrast:
1. Edit `runs/<slug>/scene.html` (adjust `x`, `y`, `w`, `h`, or `ease`/animation curves).
2. Re-run `cm snapshot <slug> --mode all`.
3. Re-inspect `runs/<slug>/qa/contact_sheet.png`.
Never skip fixing visual faults.

## Step 4: Render Preview & Final MP4

1. Fast preview check (`30fps`):
   ```powershell
   cm render <slug> --quality preview --fps 30
   ```
2. Production master (`60fps`, CRF 18, H.264 `yuv420p`, AAC `192k` 48kHz stereo from `runs/<slug>/audio/master.wav`):
   ```powershell
   cm render <slug> --quality final --fps 60
   ```
3. Verify `runs/<slug>/final.mp4` and `runs/<slug>/render_report.json` (`h264`, `yuv420p`, `aac`, `1080x1920`, target duration matches `audio/timeline.json`), then review `final.mp4` directly before publishing.
