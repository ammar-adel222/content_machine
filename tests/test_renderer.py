"""Exhaustive tests for parallel Playwright-to-FFmpeg renderer and ffprobe validation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cm.cli import app
from cm.models import (
    FormatPreset,
    PipelineStage,
    RenderReport,
    StageStatus,
    TopicDomain,
)
from cm.renderer import (
    FrameFormat,
    RenderQuality,
    _mux_chunks,
    partition_frame_ranges,
    probe_media,
    render_video,
    resolve_render_target,
)
from cm.scene import scaffold_scene
from cm.state import init_run, load_state

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_BP = REPO_ROOT / "examples" / "password_system.blueprint.json"
EXAMPLE_SCENE = REPO_ROOT / "examples" / "password_system.scene.html"


def _minimal_harness_html(
    fmt: str = "long_16x9",
    width: int = 1920,
    height: int = 1080,
    total_duration: float = 0.1,
    include_harness: bool = True,
) -> str:
    harness_js = (
        f"""
        window.__HARNESS__ = {{
            getState: function () {{
                return {{ format: {json.dumps(fmt)}, width: {width}, height: {height}, totalDuration: {total_duration} }};
            }},
            seek: function (t) {{
                document.getElementById("time-readout").textContent = t.toFixed(3);
            }}
        }};
        """
        if include_harness
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <style>
    html, body {{ margin: 0; width: 100%; height: 100%; background: #0a1020; color: #e2e8f0; display: flex; align-items: center; justify-content: center; font-family: sans-serif; font-size: 64px; }}
  </style>
</head>
<body>
  <div id="time-readout">0.000</div>
  <script>{harness_js}</script>
</body>
</html>
"""


def test_partition_frame_ranges_and_validation() -> None:
    assert partition_frame_ranges(10, 3) == [(0, 4), (4, 7), (7, 10)]
    assert partition_frame_ranges(3, 8) == [(0, 1), (1, 2), (2, 3)]
    assert partition_frame_ranges(1, 4) == [(0, 1)]

    with pytest.raises(ValueError, match="total_frames"):
        partition_frame_ranges(0, 2)
    with pytest.raises(ValueError, match="workers"):
        partition_frame_ranges(5, 0)


def test_full_slug_render_with_audio_and_state_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    res_new = runner.invoke(
        app, ["new", "render-short", "--from-blueprint", str(EXAMPLE_BP)]
    )
    assert res_new.exit_code == 0, res_new.output

    res_tts = runner.invoke(
        app, ["tts", "render-short", "--provider", "mock"]
    )
    assert res_tts.exit_code == 0, res_tts.output

    res_scene = runner.invoke(app, ["scene", "init", "render-short"])
    assert res_scene.exit_code == 0, res_scene.output

    res_render = runner.invoke(
        app,
        [
            "render",
            "render-short",
            "--workers",
            "2",
            "--fps",
            "6",
            "--max-duration",
            "2.0",
            "--json",
        ],
    )
    assert res_render.exit_code == 0, res_render.output

    report = RenderReport.model_validate_json(res_render.output)
    assert report.slug == "render-short"
    assert report.format == FormatPreset.SHORT_9X16
    assert report.width == 1080
    assert report.height == 1920
    assert report.fps == 6
    assert report.total_frames == 12
    assert report.workers == 2
    assert report.video_codec == "h264"
    assert report.pix_fmt == "yuv420p"
    assert report.audio_codec == "aac"
    assert report.audio_source is not None
    assert Path(report.audio_source).name == "master.wav"
    assert 1.8 <= report.duration_sec <= 2.2
    assert report.file_size_bytes > 5000

    final_mp4 = tmp_path / "runs" / "render-short" / "final.mp4"
    report_json = tmp_path / "runs" / "render-short" / "render_report.json"
    assert final_mp4.is_file()
    assert report_json.is_file()
    persisted = RenderReport.model_validate_json(report_json.read_text(encoding="utf-8"))
    assert persisted.output_path == str(final_mp4.resolve())

    media = probe_media(final_mp4)
    assert media["video_codec"] == "h264"
    assert media["pix_fmt"] == "yuv420p"
    assert (media["width"], media["height"]) == (1080, 1920)
    assert media["audio_codec"] == "aac"
    assert 1.8 <= float(media["duration_sec"]) <= 2.2

    state = load_state("render-short")
    assert state.stages[PipelineStage.RENDER].status == StageStatus.PASSED
    assert state.current_stage == PipelineStage.PUBLISH


def test_long_16x9_png_final_quality_and_silent_audio_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    raw_long = json.loads(EXAMPLE_BP.read_text(encoding="utf-8"))
    raw_long["format"] = FormatPreset.LONG_16X9.value
    long_bp = tmp_path / "long_bp.json"
    long_bp.write_text(json.dumps(raw_long), encoding="utf-8")
    init_run("render-long", from_blueprint=long_bp, topic_domain=TopicDomain.CS)
    scaffold_scene("render-long")

    report = render_video(
        "render-long",
        quality=RenderQuality.FINAL,
        fps=5,
        workers=1,
        frame_format=FrameFormat.PNG,
        max_duration=1.0,
    )
    assert report.format == FormatPreset.LONG_16X9
    assert (report.width, report.height) == (1920, 1080)
    assert report.video_codec == "h264"
    assert report.pix_fmt == "yuv420p"
    assert report.audio_codec == "aac"
    assert report.audio_source is None
    assert report.frame_format == "png"
    assert report.quality == "final"

    # Also test default fps selection for preview/final using a tiny 0.05s scene
    tiny_html = tmp_path / "tiny_16x9.scene.html"
    tiny_html.write_text(
        _minimal_harness_html(fmt="long_16x9", width=640, height=360, total_duration=0.05),
        encoding="utf-8",
    )
    tiny_report = render_video(
        str(tiny_html),
        quality=RenderQuality.FINAL,
        fps=None,
        workers=None,
        out_path=tmp_path / "tiny_final.mp4",
    )
    assert tiny_report.fps == 60
    assert tiny_report.total_frames == 3
    assert (tiny_report.width, tiny_report.height) == (640, 360)


def test_standalone_html_cli_human_mode_and_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))
    out_mp4 = tmp_path / "standalone_out.mp4"

    res_cli = runner.invoke(
        app,
        [
            "render",
            str(EXAMPLE_SCENE),
            "--fps",
            "6",
            "--workers",
            "2",
            "--max-duration",
            "1.0",
            "--out",
            str(out_mp4),
        ],
    )
    assert res_cli.exit_code == 0, res_cli.output
    assert "Render Complete" in res_cli.output
    res_cli.output.encode("cp1252")
    assert out_mp4.is_file()
    assert (tmp_path / "render_report.json").is_file()

    res_schema = runner.invoke(app, ["schema", "render"])
    assert res_schema.exit_code == 0, res_schema.output
    schema_obj = json.loads(res_schema.output)
    assert schema_obj["title"] == "RenderReport"
    assert "video_codec" in schema_obj["properties"]


def test_resolve_render_target_audio_precedence_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))
    init_run("audio-prio", from_blueprint=EXAMPLE_BP)
    scaffold_scene("audio-prio")

    audio_dir = tmp_path / "runs" / "audio-prio" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    voice_wav = audio_dir / "voice_master.wav"
    voice_wav.write_bytes(b"RIFF")

    _, _, _, auto_voice, _, is_slug = resolve_render_target("audio-prio")
    assert is_slug is True
    assert auto_voice == voice_wav.resolve()

    master_wav = audio_dir / "master.wav"
    master_wav.write_bytes(b"RIFF")
    _, _, _, auto_master, _, _ = resolve_render_target("audio-prio")
    assert auto_master == master_wav.resolve()

    explicit_wav = tmp_path / "custom.wav"
    explicit_wav.write_bytes(b"RIFF")
    _, _, _, custom_resolved, _, _ = resolve_render_target(
        "audio-prio", audio_path=explicit_wav
    )
    assert custom_resolved == explicit_wav.resolve()

    with pytest.raises(FileNotFoundError, match="Audio file not found"):
        resolve_render_target("audio-prio", audio_path=tmp_path / "missing.wav")


def test_renderer_error_paths_and_probe_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    # Missing slug via CLI
    res_missing = runner.invoke(app, ["render", "no-such-slug", "--json"])
    assert res_missing.exit_code == 1
    assert "error" in json.loads(res_missing.output)

    # Human error output
    res_missing_human = runner.invoke(app, ["render", "no-such-slug"])
    assert res_missing_human.exit_code == 1
    assert "[FAIL]" in res_missing_human.output

    # Missing __HARNESS__
    no_harness = tmp_path / "no_harness.scene.html"
    no_harness.write_text(_minimal_harness_html(include_harness=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="__HARNESS__"):
        render_video(str(no_harness), workers=1)

    # Zero duration scene & parameter validation
    zero_dur = tmp_path / "zero_dur.scene.html"
    zero_dur.write_text(_minimal_harness_html(total_duration=0.0), encoding="utf-8")
    with pytest.raises(RuntimeError, match="totalDuration"):
        render_video(str(zero_dur), workers=1)

    with pytest.raises(ValueError, match="max_duration"):
        render_video(str(zero_dur), workers=1, max_duration=0.0)
    with pytest.raises(ValueError, match="fps"):
        render_video(str(zero_dur), workers=1, fps=0)
    with pytest.raises(ValueError, match="workers"):
        render_video(str(zero_dur), workers=0)

    # probe_media errors: missing file, invalid file, audio-only file without video stream
    with pytest.raises(FileNotFoundError, match="Media file not found"):
        probe_media(tmp_path / "ghost.mp4")

    corrupt = tmp_path / "bad.mp4"
    corrupt.write_text("not a media file", encoding="utf-8")
    with pytest.raises(RuntimeError, match="ffprobe failed"):
        probe_media(corrupt)

    audio_only = tmp_path / "tone.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.2",
            str(audio_only),
        ],
        capture_output=True,
        check=True,
    )
    with pytest.raises(RuntimeError, match="No video stream found"):
        probe_media(audio_only)

    # Chunk FFmpeg failure & mux FFmpeg failure cleanup
    bad_chunk = tmp_path / "bad_chunk.mp4"
    bad_chunk.write_text("corrupt", encoding="utf-8")
    partial_tmp = tmp_path / f".failed_out.{__import__('os').getpid()}.tmp.mp4"
    partial_tmp.write_bytes(b"stale")
    with pytest.raises(RuntimeError, match="FFmpeg mux failed"):
        _mux_chunks([bad_chunk], None, tmp_path / "failed_out.mp4", tmp_path)
    assert not partial_tmp.exists()

    real_popen = subprocess.Popen

    def _fail_popen(cmd: object, *args: object, **kwargs: object) -> object:
        class _FakeProc:
            def __init__(self) -> None:
                self.stdin = SimpleNamespace(
                    write=lambda _b: 0, flush=lambda: None, close=lambda: None
                )

            def wait(self) -> int:
                return 2

        if isinstance(cmd, (list, tuple)) and bool(cmd) and str(cmd[0]) == "ffmpeg":
            return _FakeProc()
        return real_popen(cmd, *args, **kwargs)  # type: ignore[call-overload]

    tiny_ok = tmp_path / "tiny_ok.scene.html"
    tiny_ok.write_text(_minimal_harness_html(total_duration=0.05), encoding="utf-8")
    monkeypatch.setattr(subprocess, "Popen", _fail_popen)
    with pytest.raises(RuntimeError, match="FFmpeg chunk encode failed"):
        render_video(str(tiny_ok), fps=20, workers=1, out_path=tmp_path / "unused.mp4")
