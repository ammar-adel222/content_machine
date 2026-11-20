---
name: ideate
description: Research winning Shorts formats, mine zero-key trend curiosity feeds, deduplicate against channel memory, and register new Build->Fail->Upgrade video ideas.
---

# Skill: Ideate & Format Intelligence

Use this workflow before authoring any new blueprint so every video targets real curiosity demand and avoids repeating covered topics.

## Step 1: Mine Demand & Inspect Format Playbook

1. Run zero-key curiosity & demand feeds:
   ```powershell
   cm research trends --sources youtube,google,hn,reddit --limit 10 --topic "<domain-or-keyword>"
   ```
   Or scan general AI/CS curiosity without a narrow topic:
   ```powershell
   cm research trends --limit 10
   ```
2. *(Optional when `YOUTUBE_API_KEY` is configured in `.env`)* Scan similar YouTube Shorts (`<= 90s`) to learn what duration, title pattern, and opening hook earn outsized views vs channel subscribers:
   ```powershell
   cm research youtube "<topic query>" --limit 15 --sort outlier --transcripts 5
   ```
3. Review the accumulated channel playbook (`channels/research.json`):
   ```powershell
   cm research show --top 10
   ```
   If you notice a recurring winning format or hook pattern, record it permanently:
   ```powershell
   cm research insight "Open immediately with a failing benchmark under 3.0s"
   ```

## Step 2: Check Existing Backlog & Deduplicate

Inspect all queued/in-progress/published topics before committing to a concept:
```powershell
cm ideas list
```
Never pick an idea whose core mechanism overlaps (`>= 60%` token similarity or identical slug) with an existing entry in `channels/catalog.json`.

## Step 3: Apply the 3-Failure Test

A topic qualifies ONLY if you can map **one persistent 2D architecture diagram** across at least **3 progressive breakages** (`Build -> Stress-Test -> Fail -> Upgrade`):
- Can a curious non-expert grasp the v1.0 flaw in 5 seconds visually?
- Does each upgrade cleanly solve the previous failure yet expose a *new* real engineering bottleneck (latency, race condition, memory explosion, distributed split, cost)?
- Keep the final architecture simple enough to read cleanly inside `856x840` (`short_9x16`).

## Step 4: Register & Activate the Idea

```powershell
cm ideas add \
  --title "Building a Rate Limiter From Scratch" \
  --domain systems \
  --hook "Why does a 10-line traffic blocker collapse under a 61-second burst?" \
  --failure "v1.0 Fixed 60s Window -> Burst Stress-Test -> BOUNDARY BURST 2x Spike -> v2.0 Sliding Log" \
  --failure "v2.0 Sliding Log -> High-RPS Load -> OUT OF MEMORY -> v3.0 Token Bucket" \
  --failure "v3.0 Token Bucket -> Distributed Cluster -> 10X BYPASS Race -> v4.0 Atomic Redis Buckets" \
  --score 9

cm ideas pick <slug>
```
Verify `runs/<slug>/state.json` and starter `runs/<slug>/blueprint.json` were created before moving to the `blueprint` skill.
