# CLAUDE.md — Content Machine 3.0

Read [AGENTS.md](./AGENTS.md) first — it is the canonical repository guide and rulebook for **Content Machine 3.0** across Claude Code, Codex, and Antigravity.

## Quick Execution Loop

1. Activate / invoke the repo virtual environment CLI (`cm`):
   - **Research & Pick:** `cm research trends` -> `cm research youtube "<topic>"` -> `cm ideas add` -> `cm ideas pick <slug>`
   - **Script & Audio:** edit `runs/<slug>/blueprint.json` -> `cm validate <slug>` -> `cm tts <slug>`
   - **2D Scene & QA:** `cm scene init <slug>` -> code deterministic `renderStage(ctx)` in `runs/<slug>/scene.html` -> `cm snapshot <slug> --mode all` -> inspect `runs/<slug>/qa/contact_sheet.png`
   - **Render:** `cm render <slug> --quality final`
2. Always run `cm snapshot <slug> --mode all` and inspect `contact_sheet.png` before declaring a video done.
3. Keep all harness code edits compliant with `ruff check cm tests` and `pytest -v --cov=cm --cov-branch --cov-fail-under=95`.

## Skill Directory

- `.agents/skills/ideate/SKILL.md`
- `.agents/skills/blueprint/SKILL.md`
- `.agents/skills/build-scene/SKILL.md`
- `.agents/skills/visual-qa/SKILL.md`
- `.agents/skills/publish/SKILL.md`
