"""Exhaustive tests for Playwright visual QA snapshotter, diffing, and contact sheets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from typer.testing import CliRunner

from cm.cli import app
from cm.models import (
    FormatPreset,
    PipelineStage,
    QaReport,
    StageStatus,
    TopicDomain,
)
from cm.scene import scaffold_scene
from cm.snapshot import (
    SnapshotMode,
    compute_image_stddev,
    compute_stage_diff_pct,
    run_snapshot,
)
from cm.state import init_run, load_state

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_BP = REPO_ROOT / "examples" / "password_system.blueprint.json"
EXAMPLE_SCENE = REPO_ROOT / "examples" / "password_system.scene.html"


def _make_custom_scene(bp_dict: dict, render_js: str) -> str:
    bp_json = json.dumps(bp_dict, indent=2)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <link rel="stylesheet" href="/engine/tokens.css" />
</head>
<body>
  <div id="cm-root"></div>
  <script id="cm-blueprint-data" type="application/json">{bp_json}</script>
  <script src="/engine/runtime.js"></script>
  <script src="/engine/primitives.js"></script>
  <script>
    (function () {{
      const P = window.CM.primitives;
      function renderStage(ctx) {{
        {render_js}
      }}
      window.CM.bootScene({{ renderStage: renderStage }});
    }})();
  </script>
</body>
</html>
"""


def test_image_stddev_and_stage_diff_helpers() -> None:
    solid = Image.new("RGB", (200, 200), (10, 20, 30))
    assert compute_image_stddev(solid) == 0.0

    patterned = Image.new("RGB", (200, 200), (10, 20, 30))
    draw = ImageDraw.Draw(patterned)
    draw.rectangle([40, 40, 140, 140], fill=(240, 240, 240))
    assert compute_image_stddev(patterned) > 20.0

    assert compute_stage_diff_pct(solid, solid) == 0.0
    assert compute_stage_diff_pct(solid, patterned) > 5.0

    # Cropping outside the changed region yields 0% stage diff
    img_top_change = Image.new("RGB", (200, 200), (10, 20, 30))
    ImageDraw.Draw(img_top_change).rectangle([0, 0, 199, 40], fill=(255, 255, 255))
    assert compute_stage_diff_pct(solid, img_top_change, crop_box=(10, 60, 190, 190)) == 0.0


def test_snapshot_example_scene_all_and_mid_modes_clean_stale_frames(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "qa_out"

    # Mode ALL -> 27 frames (9 beats x 3)
    report_all = run_snapshot(
        str(EXAMPLE_SCENE),
        mode=SnapshotMode.ALL,
        out_dir=out_dir,
        safe_guides=True,
    )
    assert report_all.passed is True
    assert report_all.error_count == 0
    assert report_all.frame_count == 27
    assert len(report_all.beat_diffs) == 8
    assert all(bd.passed and bd.diff_pct > 0.5 for bd in report_all.beat_diffs)

    frames = list((out_dir / "frames").glob("*.png"))
    assert len(frames) == 27
    with Image.open(frames[0]) as f0:
        assert f0.size == (1080, 1920)

    sheet_path = Path(report_all.contact_sheet_path)
    assert sheet_path.is_file()
    with Image.open(sheet_path) as sheet:
        assert sheet.size[0] > 1000 and sheet.size[1] > 1000

    persisted = QaReport.model_validate_json(
        (out_dir / "report.json").read_text(encoding="utf-8")
    )
    assert persisted.passed is True

    # Mode MID into same out_dir cleans stale PNGs down to 9 frames
    report_mid = run_snapshot(
        str(EXAMPLE_SCENE),
        mode=SnapshotMode.MID,
        out_dir=out_dir,
        safe_guides=False,
    )
    assert report_mid.passed is True
    assert report_mid.frame_count == 9
    assert len(list((out_dir / "frames").glob("*.png"))) == 9


def test_snapshot_slug_updates_state_and_supports_long_16x9(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    # Short 9:16 via CLI
    assert runner.invoke(app, ["new", "snap-short", "--from-blueprint", str(EXAMPLE_BP)]).exit_code == 0
    assert runner.invoke(app, ["scene", "init", "snap-short"]).exit_code == 0

    res_snap = runner.invoke(app, ["snapshot", "snap-short", "--mode", "mid"])
    assert res_snap.exit_code == 0, res_snap.output
    assert "[PASS]" in res_snap.output
    res_snap.output.encode("cp1252")

    state = load_state("snap-short")
    assert state.stages[PipelineStage.SCENE].status == StageStatus.PASSED
    assert state.stages[PipelineStage.QA].status == StageStatus.PASSED
    assert state.current_stage == PipelineStage.RENDER
    assert (tmp_path / "runs" / "snap-short" / "qa" / "contact_sheet.png").is_file()
    assert (tmp_path / "runs" / "snap-short" / "qa" / "report.json").is_file()

    # Long 16:9 snapshot -> 1920x1080 frames & 2-col contact sheet
    raw_long = json.loads(EXAMPLE_BP.read_text(encoding="utf-8"))
    raw_long["format"] = FormatPreset.LONG_16X9.value
    long_bp_path = tmp_path / "long_bp.json"
    long_bp_path.write_text(json.dumps(raw_long), encoding="utf-8")
    init_run("snap-long", from_blueprint=long_bp_path, topic_domain=TopicDomain.CS)
    scaffold_scene("snap-long")

    res_long = runner.invoke(app, ["snapshot", "snap-long", "--mode", "mid", "--json"])
    assert res_long.exit_code == 0, res_long.output
    rep_long = QaReport.model_validate_json(res_long.output)
    assert rep_long.passed is True
    assert (rep_long.width, rep_long.height) == (1920, 1080)
    with Image.open(tmp_path / "runs" / "snap-long" / "qa" / "frames" / "001_b01_hook_mid.png") as f_long:
        assert f_long.size == (1920, 1080)


def test_snapshot_detects_static_beat_visual_even_when_hud_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))
    raw_bp = json.loads(EXAMPLE_BP.read_text(encoding="utf-8"))
    raw_bp["beats"] = raw_bp["beats"][:4]
    raw_bp["slug"] = "static-stage"
    init_run("static-stage", from_blueprint=EXAMPLE_BP)

    scene_path = tmp_path / "runs" / "static-stage" / "scene.html"
    scene_path.write_text(
        _make_custom_scene(
            raw_bp,
            """
            P.clear(ctx.svg);
            P.node(ctx.svg, { x: 200, y: 200, w: 450, h: 140, title: "Static Node", subtitle: "never moves", accent: "cyan" });
            """,
        ),
        encoding="utf-8",
    )

    res = runner.invoke(app, ["snapshot", "static-stage", "--mode", "mid", "--json"])
    assert res.exit_code == 1
    rep = QaReport.model_validate_json(res.output)
    assert rep.passed is False
    assert any(v.code == "STATIC_BEAT_VISUAL" for v in rep.violations)
    assert Path(rep.contact_sheet_path).is_file()
    assert load_state("static-stage").stages[PipelineStage.QA].status == StageStatus.FAILED


def test_snapshot_detects_layout_runtime_console_page_and_blank_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))
    raw_bp = json.loads(EXAMPLE_BP.read_text(encoding="utf-8"))
    raw_bp["beats"] = raw_bp["beats"][:4]

    # 1. Layout overflow + font too small + runtime exception + console error + pageerror
    raw_bp["slug"] = "broken-layout"
    init_run("broken-layout", from_blueprint=EXAMPLE_BP)
    (tmp_path / "runs" / "broken-layout" / "scene.html").write_text(
        _make_custom_scene(
            raw_bp,
            """
            P.clear(ctx.svg);
            P.node(ctx.svg, { x: -80, y: 200, w: 300, h: 120, title: "Offscreen " + ctx.beatIndex, subtitle: "bad", accent: "crimson" });
            if (ctx.beatIndex === 0) {
                console.error("simulated console error");
                setTimeout(function () { throw new Error("async page error"); }, 0);
            }
            if (ctx.beatIndex === 1) {
                throw new Error("boom in renderStage");
            }
            """,
        ),
        encoding="utf-8",
    )

    res_broken = runner.invoke(app, ["snapshot", "broken-layout", "--mode", "mid"])
    assert res_broken.exit_code == 1
    assert "[FAIL]" in res_broken.output
    res_broken.output.encode("cp1252")

    rep_broken = QaReport.model_validate_json(
        (tmp_path / "runs" / "broken-layout" / "qa" / "report.json").read_text(encoding="utf-8")
    )
    codes = {v.code for v in rep_broken.violations}
    assert "SAFE_ZONE_OVERFLOW" in codes
    assert "RUNTIME_ERROR" in codes
    assert "CONSOLE_ERROR" in codes
    assert "PAGE_ERROR" in codes

    # 2. Blank uniform frame detection
    raw_bp["slug"] = "blank-frame"
    init_run("blank-frame", from_blueprint=EXAMPLE_BP)
    (tmp_path / "runs" / "blank-frame" / "scene.html").write_text(
        _make_custom_scene(
            raw_bp,
            """
            P.clear(ctx.svg);
            P.node(ctx.svg, { x: 200, y: 200, w: 400, h: 120, title: "Hidden " + ctx.beatIndex, subtitle: "covered", accent: "cyan" });
            const canvas = document.getElementById('cm-canvas');
            let cover = document.getElementById('blank-cover');
            if (!cover) {
                cover = document.createElement('div');
                cover.id = 'blank-cover';
                cover.style.cssText = 'position:absolute;inset:0;background:#070b14;z-index:999;';
                canvas.appendChild(cover);
            }
            """,
        ),
        encoding="utf-8",
    )

    rep_blank = run_snapshot("blank-frame", mode=SnapshotMode.MID, min_beat_diff_pct=0.0)
    assert rep_blank.passed is False
    assert any(v.code == "BLANK_FRAME" for v in rep_blank.violations)


def test_snapshot_missing_harness_zero_keyframes_and_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    # Plain HTML missing window.__HARNESS__
    plain_html = tmp_path / "plain.html"
    plain_html.write_text("<html><body><h1>Not a harness scene</h1></body></html>", encoding="utf-8")
    res_plain = runner.invoke(app, ["snapshot", str(plain_html), "--json"])
    assert res_plain.exit_code == 1
    assert "missing window.__HARNESS__" in json.loads(res_plain.output)["error"]

    # Scene with runtime.js loaded but never booted (0 keyframes)
    unbooted_html = tmp_path / "unbooted.html"
    unbooted_html.write_text(
        '<!DOCTYPE html><html><head><script src="/engine/runtime.js"></script></head><body></body></html>',
        encoding="utf-8",
    )
    res_unbooted = runner.invoke(app, ["snapshot", str(unbooted_html)])
    assert res_unbooted.exit_code == 1
    assert "0 keyframes" in res_unbooted.output

    # Missing slug & cm schema qa
    assert runner.invoke(app, ["snapshot", "ghost-slug"]).exit_code == 1
    res_schema = runner.invoke(app, ["schema", "qa"])
    assert res_schema.exit_code == 0
    schema_obj = json.loads(res_schema.output)
    assert "beat_diffs" in schema_obj["properties"]
