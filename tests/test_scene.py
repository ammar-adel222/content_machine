"""Unit and CLI tests for 2D scene scaffolding, timeline preview, and HTTP server."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cm.cli import app
from cm.models import Blueprint, FormatPreset, PipelineStage, StageStatus, TopicDomain
from cm.paths import resolve_engine_dir
from cm.scene import (
    build_preview_timeline,
    create_preview_server,
    resolve_preview_target,
    scaffold_scene,
)
from cm.state import init_run, load_state, update_stage

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_BP = REPO_ROOT / "examples" / "password_system.blueprint.json"
EXAMPLE_SCENE = REPO_ROOT / "examples" / "password_system.scene.html"


def test_resolve_engine_dir_falls_back_to_repo_engine(tmp_path: Path) -> None:
    resolved = resolve_engine_dir(tmp_path)
    assert (resolved / "runtime.js").is_file()
    assert (resolved / "tokens.css").is_file()
    assert (resolved / "primitives.js").is_file()

    # When root actually contains engine/runtime.js, prefer it
    local_eng = tmp_path / "engine"
    local_eng.mkdir()
    (local_eng / "runtime.js").write_text("// custom", encoding="utf-8")
    assert resolve_engine_dir(tmp_path) == local_eng


def test_build_preview_timeline_matches_blueprint_beats() -> None:
    bp = Blueprint.model_validate_json(EXAMPLE_BP.read_text(encoding="utf-8"))
    tl = build_preview_timeline(bp)
    beats = tl["beats"]
    assert isinstance(beats, list)
    assert len(beats) == len(bp.beats)
    assert tl["total_duration_sec"] > 45.0
    assert beats[0]["start_sec"] == 0.0
    for prev, nxt in zip(beats[:-1], beats[1:], strict=True):
        assert prev["end_sec"] == pytest.approx(nxt["start_sec"], abs=1e-3)
        assert len(prev["words"]) >= 1

    # Explicit estimated_duration_sec & empty/punctuation word guard
    raw = json.loads(EXAMPLE_BP.read_text(encoding="utf-8"))
    raw["beats"][0]["estimated_duration_sec"] = 3.2
    bp2 = Blueprint.model_validate(raw)
    tl2 = build_preview_timeline(bp2)
    assert tl2["beats"][0]["duration_sec"] == 3.2


def test_scaffold_scene_short_long_overwrite_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    # Missing run -> FileNotFoundError
    with pytest.raises(FileNotFoundError):
        scaffold_scene("does-not-exist")

    # Initialize from valid short_9x16 example
    init_run("password-system", from_blueprint=EXAMPLE_BP)
    scene_path = scaffold_scene("password-system")
    assert scene_path.is_file()
    html = scene_path.read_text(encoding="utf-8")
    assert "tokens.css" in html
    assert "Building a Password System From Scratch" in html

    state = load_state("password-system")
    assert state.stages[PipelineStage.SCENE].status == StageStatus.IN_PROGRESS

    # Duplicate guard without --force
    with pytest.raises(FileExistsError):
        scaffold_scene("password-system")

    # Passed stage stays passed unless force=True
    update_stage("password-system", PipelineStage.SCENE, StageStatus.PASSED)
    scene_path2 = scaffold_scene("password-system", force=True)
    assert scene_path2 == scene_path
    assert load_state("password-system").stages[PipelineStage.SCENE].status == StageStatus.IN_PROGRESS

    # Portable ../../engine relative path works over both HTTP and file://
    assert "../../engine/tokens.css" in html
    assert (tmp_path / "engine" / "runtime.js").is_file()

    # Overwriting with an already-existing local engine directory preserves custom assets
    local_engine = tmp_path / "engine"
    local_engine.mkdir(exist_ok=True)
    scene_rel = scaffold_scene("password-system", force=True)
    assert "../../engine/tokens.css" in scene_rel.read_text(encoding="utf-8")

    # Long 16:9 blueprint scaffolds cleanly too
    raw_long = json.loads(EXAMPLE_BP.read_text(encoding="utf-8"))
    raw_long["format"] = FormatPreset.LONG_16X9.value
    raw_long["title"] = "Rate Limiter <10ms> & Cache"
    long_bp = tmp_path / "long.json"
    long_bp.write_text(json.dumps(raw_long), encoding="utf-8")
    init_run("long-demo", from_blueprint=long_bp, topic_domain=TopicDomain.SYSTEMS)
    long_scene = scaffold_scene("long-demo")
    assert "Rate Limiter &lt;10ms&gt; &amp; Cache" in long_scene.read_text(encoding="utf-8")

    # Missing or invalid blueprint errors
    (tmp_path / "runs" / "long-demo" / "blueprint.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid blueprint"):
        scaffold_scene("long-demo", force=True)

    (tmp_path / "runs" / "long-demo" / "blueprint.json").unlink()
    with pytest.raises(FileNotFoundError):
        scaffold_scene("long-demo", force=True)


def test_resolve_preview_target_and_http_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))
    init_run("preview-demo", from_blueprint=EXAMPLE_BP)

    # Missing scene.html error
    with pytest.raises(FileNotFoundError, match="cm scene init"):
        resolve_preview_target("preview-demo")

    scene_path = scaffold_scene("preview-demo")

    # Resolve by slug
    s_root, rel = resolve_preview_target("preview-demo")
    assert s_root == tmp_path.resolve()
    assert rel == Path("runs") / "preview-demo" / "scene.html"

    # Resolve by external .html path outside CM_ROOT
    ext_root, ext_rel = resolve_preview_target(str(EXAMPLE_SCENE))
    assert ext_root == EXAMPLE_SCENE.parent.resolve()
    assert ext_rel == Path(EXAMPLE_SCENE.name)

    # Error paths: directory, missing html, non-html
    with pytest.raises(ValueError, match="got directory"):
        resolve_preview_target(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        resolve_preview_target("missing/scene.html")
    non_html = tmp_path / "notes.txt"
    non_html.write_text("hi", encoding="utf-8")
    with pytest.raises(ValueError, match="must be an .html file"):
        resolve_preview_target(str(non_html))

    # HTTP server serves both runs/<slug>/scene.html and /engine/runtime.js fallback
    server = create_preview_server(s_root, port=0, engine_fallback=REPO_ROOT / "engine")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scene_url = f"http://127.0.0.1:{port}/{rel.as_posix()}"
        with urllib.request.urlopen(scene_url, timeout=5) as resp:
            assert resp.status == 200
            assert b"cm-blueprint-data" in resp.read()

        engine_url = f"http://127.0.0.1:{port}/engine/runtime.js"
        with urllib.request.urlopen(engine_url, timeout=5) as resp:
            assert resp.status == 200
            assert b"__HARNESS__" in resp.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert scene_path.is_file()


def test_cli_scene_init_and_preview_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    # Missing run fails in both human and JSON modes
    assert runner.invoke(app, ["scene", "init", "missing-run"]).exit_code == 1
    res_err_json = runner.invoke(app, ["scene", "init", "missing-run", "--json"])
    assert res_err_json.exit_code == 1
    assert "error" in json.loads(res_err_json.output)

    # Create run and scaffold scene (human + --force --json)
    assert runner.invoke(app, ["new", "scene-cli", "--from-blueprint", str(EXAMPLE_BP)]).exit_code == 0

    res_init = runner.invoke(app, ["scene", "init", "scene-cli"])
    assert res_init.exit_code == 0, res_init.output
    res_init.output.encode("cp1252")

    assert runner.invoke(app, ["scene", "init", "scene-cli"]).exit_code == 1
    res_force = runner.invoke(app, ["scene", "init", "scene-cli", "--force", "--json"])
    assert res_force.exit_code == 0, res_force.output
    assert Path(json.loads(res_force.output)["scene_path"]).is_file()

    # Preview via slug (--once --no-open --port 0)
    res_prev = runner.invoke(app, ["preview", "scene-cli", "--once", "--no-open", "--port", "0"])
    assert res_prev.exit_code == 0, res_prev.output
    assert "Preview URL: http://127.0.0.1:" in res_prev.output
    res_prev.output.encode("cp1252")

    # Preview missing target fails cleanly
    res_prev_err = runner.invoke(app, ["preview", "no-such-slug", "--once", "--no-open"])
    assert res_prev_err.exit_code == 1
    res_prev_err.output.encode("cp1252")
