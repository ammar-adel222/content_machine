"""Verification tests for portable agent skills and the rate-limiter flagship prototype."""

from __future__ import annotations

import json
import re
from pathlib import Path

from cm.renderer import render_video
from cm.snapshot import SnapshotMode, run_snapshot
from cm.validators import validate_blueprint_file

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_SKILLS = ("ideate", "blueprint", "build-scene", "visual-qa", "publish")
RATE_LIMITER_BP = REPO_ROOT / "examples" / "rate_limiter.blueprint.json"
RATE_LIMITER_SCENE = REPO_ROOT / "examples" / "rate_limiter.scene.html"


def _parse_frontmatter(skill_md: Path) -> dict[str, str]:
    text = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    assert match is not None, f"Missing YAML frontmatter in {skill_md}"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            fields[key.strip()] = val.strip()
    return fields


def test_agent_entrypoints_and_portable_skills() -> None:
    agents_md = REPO_ROOT / "AGENTS.md"
    claude_md = REPO_ROOT / "CLAUDE.md"
    assert agents_md.is_file()
    assert claude_md.is_file()

    agents_text = agents_md.read_text(encoding="utf-8")
    claude_text = claude_md.read_text(encoding="utf-8")
    assert "AGENTS.md" in claude_text

    for skill_name in EXPECTED_SKILLS:
        assert f".agents/skills/{skill_name}/SKILL.md" in agents_text
        assert f".agents/skills/{skill_name}/SKILL.md" in claude_text

        skill_path = REPO_ROOT / ".agents" / "skills" / skill_name / "SKILL.md"
        assert skill_path.is_file()
        meta = _parse_frontmatter(skill_path)
        assert meta.get("name") == skill_name
        assert meta.get("description")
        body = skill_path.read_text(encoding="utf-8")
        assert "cm " in body


def test_rate_limiter_prototype_blueprint_scene_qa_and_render(tmp_path: Path) -> None:
    assert RATE_LIMITER_BP.is_file()
    assert RATE_LIMITER_SCENE.is_file()

    # Blueprint validates cleanly with 0 errors and 0 warnings
    val_report = validate_blueprint_file(RATE_LIMITER_BP)
    assert val_report.passed is True
    assert val_report.errors == []
    assert val_report.warnings == []
    assert val_report.metrics is not None
    assert val_report.metrics.beat_count == 9
    assert val_report.metrics.iteration_count == 3

    # Embedded blueprint inside scene HTML stays synchronized with rate_limiter.blueprint.json
    scene_html = RATE_LIMITER_SCENE.read_text(encoding="utf-8")
    embedded = re.search(
        r'<script id="cm-blueprint-data" type="application/json">\s*(.*?)\s*</script>',
        scene_html,
        re.DOTALL,
    )
    assert embedded is not None
    assert json.loads(embedded.group(1)) == json.loads(
        RATE_LIMITER_BP.read_text(encoding="utf-8")
    )

    # All 27 Chromium keyframes pass DOM layout/safe-zone/diff audit
    qa_report = run_snapshot(
        str(RATE_LIMITER_SCENE),
        mode=SnapshotMode.ALL,
        out_dir=tmp_path / "qa",
    )
    assert qa_report.passed is True
    assert qa_report.error_count == 0
    assert qa_report.frame_count == 27
    assert len(qa_report.beat_diffs) == 8
    assert all(bd.passed and bd.diff_pct > 0.5 for bd in qa_report.beat_diffs)

    # Quick parallel H.264/AAC MP4 render smoke check
    mp4_out = tmp_path / "rate_limiter_smoke.mp4"
    render_rep = render_video(
        str(RATE_LIMITER_SCENE),
        fps=6,
        workers=2,
        max_duration=1.5,
        out_path=mp4_out,
    )
    assert mp4_out.is_file()
    assert render_rep.video_codec == "h264"
    assert render_rep.pix_fmt == "yuv420p"
    assert (render_rep.width, render_rep.height) == (1080, 1920)
    assert render_rep.audio_codec == "aac"
