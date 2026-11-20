"""Parallel Playwright-to-FFmpeg H.264/AAC video renderer for Content Machine."""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import tempfile
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from playwright.async_api import async_playwright

from cm.models import (
    FORMAT_SPECS,
    FormatPreset,
    PipelineStage,
    RenderReport,
    StageStatus,
)
from cm.paths import atomic_write_text, resolve_engine_dir, run_dir
from cm.scene import create_preview_server, resolve_preview_target
from cm.state import update_stage


class RenderQuality(StrEnum):
    """Encoding quality presets balancing speed vs visual fidelity."""

    PREVIEW = "preview"
    FINAL = "final"


class FrameFormat(StrEnum):
    """Screenshot transport format piped from Chromium to FFmpeg."""

    JPEG = "jpeg"
    PNG = "png"


_QUALITY_SETTINGS: dict[RenderQuality, tuple[str, str]] = {
    RenderQuality.PREVIEW: ("23", "veryfast"),
    RenderQuality.FINAL: ("18", "medium"),
}


def default_worker_count() -> int:
    """Return a conservative default Chromium worker count for the host CPU."""
    return max(1, min(4, os.cpu_count() or 2))


def partition_frame_ranges(total_frames: int, workers: int) -> list[tuple[int, int]]:
    """Split [0, total_frames) into contiguous non-empty [start, end) ranges."""
    if total_frames < 1:
        raise ValueError("total_frames must be >= 1")
    if workers < 1:
        raise ValueError("workers must be >= 1")

    effective = min(workers, total_frames)
    base, remainder = divmod(total_frames, effective)
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for idx in range(effective):
        span = base + (1 if idx < remainder else 0)
        ranges.append((cursor, cursor + span))
        cursor += span
    return ranges


def probe_media(media_path: Path) -> dict[str, object]:
    """Inspect a rendered media container using ffprobe and return stream facts."""
    if not media_path.is_file():
        raise FileNotFoundError(f"Media file not found: {media_path}")

    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {media_path}: {proc.stderr.strip()}")

    payload = json.loads(proc.stdout)
    streams = payload.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise RuntimeError(f"No video stream found in {media_path}")

    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = payload.get("format", {})
    duration_raw = fmt.get("duration") or video_stream.get("duration") or 0.0
    size_raw = fmt.get("size") or media_path.stat().st_size

    return {
        "video_codec": str(video_stream.get("codec_name", "")),
        "pix_fmt": str(video_stream.get("pix_fmt", "")),
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "audio_codec": str(audio_stream["codec_name"]) if audio_stream else None,
        "duration_sec": float(duration_raw),
        "file_size_bytes": int(size_raw),
    }


def resolve_render_target(
    target: str,
    out_path: Path | None = None,
    audio_path: Path | None = None,
    root: Path | None = None,
) -> tuple[Path, Path, Path, Path | None, str, bool]:
    """Resolve scene server root, HTML path, MP4 output path, and audio source."""
    serve_root, rel_html_path = resolve_preview_target(target, root=root)
    candidate_run = run_dir(target, root=root)
    is_slug_mode = not target.lower().endswith((".html", ".htm")) and candidate_run.is_dir()

    if is_slug_mode:
        slug_label = target
        default_out = candidate_run / "final.mp4"
    else:
        slug_label = rel_html_path.stem.removesuffix(".scene")
        default_out = (serve_root / rel_html_path).with_suffix(".mp4")

    output_mp4 = (out_path or default_out).resolve()

    resolved_audio: Path | None = None
    if audio_path is not None:
        if not audio_path.is_file():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        resolved_audio = audio_path.resolve()
    elif is_slug_mode:
        for candidate_name in ("master.wav", "voice_master.wav"):
            candidate_audio = candidate_run / "audio" / candidate_name
            if candidate_audio.is_file():
                resolved_audio = candidate_audio.resolve()
                break

    return serve_root, rel_html_path, output_mp4, resolved_audio, slug_label, is_slug_mode


def _write_pipe(pipe: BinaryIO, data: bytes) -> None:
    """Write a frame buffer synchronously inside a worker thread."""
    pipe.write(data)
    pipe.flush()


async def _capture_chunk(
    context: object,
    scene_url: str,
    frame_range: tuple[int, int],
    fps: int,
    quality: RenderQuality,
    frame_format: FrameFormat,
    chunk_path: Path,
) -> Path:
    """Seek a page over [start, end) and pipe PNG/JPEG frames to an H.264 chunk."""
    start_frame, end_frame = frame_range
    crf, preset = _QUALITY_SETTINGS[quality]
    img_codec = "mjpeg" if frame_format == FrameFormat.JPEG else "png"
    screenshot_type = "jpeg" if frame_format == FrameFormat.JPEG else "png"

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "image2pipe",
        "-vcodec",
        img_codec,
        "-framerate",
        str(fps),
        "-i",
        "pipe:0",
        "-c:v",
        "libx264",
        "-vf",
        "scale=out_range=tv,format=yuv420p",
        "-color_range",
        "tv",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        preset,
        "-crf",
        crf,
        "-g",
        str(fps),
        "-movflags",
        "+faststart",
        str(chunk_path),
    ]

    page = await context.new_page()  # type: ignore[attr-defined]
    log_path = chunk_path.with_suffix(".ffmpeg.log")
    rc = 1
    try:
        await page.goto(scene_url, wait_until="networkidle")
        await page.evaluate("() => (document.fonts ? document.fonts.ready : true)")

        with log_path.open("wb") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
            )
            assert proc.stdin is not None
            try:
                for frame_idx in range(start_frame, end_frame):
                    t_sec = frame_idx / fps
                    await page.evaluate("(t) => window.__HARNESS__.seek(t)", t_sec)
                    shot_kwargs: dict[str, object] = {"type": screenshot_type}
                    if frame_format == FrameFormat.JPEG:
                        shot_kwargs["quality"] = 92
                    frame_bytes = await page.screenshot(**shot_kwargs)
                    await asyncio.to_thread(_write_pipe, proc.stdin, frame_bytes)
            finally:
                proc.stdin.close()
                rc = await asyncio.to_thread(proc.wait)
    finally:
        await page.close()

    if rc != 0:
        err_text = log_path.read_text(encoding="utf-8", errors="replace").strip()
        raise RuntimeError(f"FFmpeg chunk encode failed (exit {rc}): {err_text[-800:]}")

    return chunk_path


async def _render_chunks_async(
    scene_url: str,
    quality: RenderQuality,
    fps: int | None,
    workers: int,
    frame_format: FrameFormat,
    max_duration: float | None,
    work_dir: Path,
) -> tuple[FormatPreset, int, int, int, int, list[Path]]:
    """Launch headless Chromium, inspect harness metadata, and render chunks in parallel."""
    if max_duration is not None and max_duration <= 0:
        raise ValueError("max_duration must be > 0")
    if fps is not None and fps <= 0:
        raise ValueError("fps must be > 0")
    if workers < 1:
        raise ValueError("workers must be >= 1")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            probe_ctx = await browser.new_context()
            try:
                probe_page = await probe_ctx.new_page()
                await probe_page.goto(scene_url, wait_until="networkidle")
                harness_state = await probe_page.evaluate(
                    "() => (window.__HARNESS__ ? window.__HARNESS__.getState() : null)"
                )
            finally:
                await probe_ctx.close()

            if not isinstance(harness_state, dict):
                raise RuntimeError(f"Scene does not expose window.__HARNESS__: {scene_url}")

            fmt_preset = FormatPreset(str(harness_state.get("format", FormatPreset.SHORT_9X16)))
            spec = FORMAT_SPECS[fmt_preset]
            width = int(harness_state.get("width") or spec.width)
            height = int(harness_state.get("height") or spec.height)
            total_duration = float(harness_state.get("totalDuration") or 0.0)
            if max_duration is not None:
                total_duration = min(total_duration, max_duration)
            if total_duration <= 0:
                raise RuntimeError("Scene totalDuration must be > 0")

            resolved_fps = fps if fps is not None else (
                spec.fps_final if quality == RenderQuality.FINAL else spec.fps_preview
            )
            total_frames = max(1, math.ceil(total_duration * resolved_fps))
            ranges = partition_frame_ranges(total_frames, workers)

            render_ctx = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            try:
                tasks = [
                    _capture_chunk(
                        render_ctx,
                        scene_url,
                        frame_range,
                        resolved_fps,
                        quality,
                        frame_format,
                        work_dir / f"chunk_{idx:03d}.mp4",
                    )
                    for idx, frame_range in enumerate(ranges)
                ]
                chunk_paths = list(await asyncio.gather(*tasks))
            finally:
                await render_ctx.close()
        finally:
            await browser.close()

    return fmt_preset, width, height, resolved_fps, total_frames, chunk_paths


def _mux_chunks(
    chunk_paths: list[Path],
    resolved_audio: Path | None,
    output_mp4: Path,
    work_dir: Path,
) -> None:
    """Concatenate encoded MP4 chunks and mux AAC audio atomically into output_mp4."""
    concat_file = work_dir / "concat.txt"
    concat_lines = [f"file '{chunk.name}'" for chunk in chunk_paths]
    concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp_mp4 = output_mp4.parent / f".{output_mp4.stem}.{os.getpid()}.tmp.mp4"

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
    ]
    if resolved_audio is not None:
        cmd.extend(["-i", str(resolved_audio), "-af", "apad"])
    else:
        cmd.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])

    cmd.extend(
        [
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            "-movflags",
            "+faststart",
            str(tmp_mp4),
        ]
    )

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg mux failed (exit {proc.returncode}): {proc.stderr[-800:]}")
        os.replace(tmp_mp4, output_mp4)
    except Exception:
        if tmp_mp4.exists():
            tmp_mp4.unlink()
        raise


def render_video(
    target: str,
    quality: RenderQuality = RenderQuality.PREVIEW,
    fps: int | None = None,
    workers: int | None = None,
    frame_format: FrameFormat = FrameFormat.JPEG,
    out_path: Path | None = None,
    audio_path: Path | None = None,
    max_duration: float | None = None,
    root: Path | None = None,
) -> RenderReport:
    """Render a scene HTML or run slug to H.264/AAC MP4 and record render_report.json."""
    started_at = time.perf_counter()
    worker_count = default_worker_count() if workers is None else workers

    serve_root, rel_html_path, output_mp4, resolved_audio, slug_label, is_slug_mode = (
        resolve_render_target(target, out_path=out_path, audio_path=audio_path, root=root)
    )
    engine_fallback = resolve_engine_dir(root=root)

    server = create_preview_server(
        serve_root,
        host="127.0.0.1",
        port=0,
        engine_fallback=engine_fallback,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scene_url = f"http://127.0.0.1:{port}/{rel_html_path.as_posix()}"

    try:
        with tempfile.TemporaryDirectory(prefix="cm_render_") as tmp_dir:
            work_dir = Path(tmp_dir)
            fmt_preset, width, height, resolved_fps, total_frames, chunk_paths = asyncio.run(
                _render_chunks_async(
                    scene_url=scene_url,
                    quality=quality,
                    fps=fps,
                    workers=worker_count,
                    frame_format=frame_format,
                    max_duration=max_duration,
                    work_dir=work_dir,
                )
            )
            _mux_chunks(chunk_paths, resolved_audio, output_mp4, work_dir)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    media_info = probe_media(output_mp4)
    report = RenderReport(
        slug=slug_label,
        scene_path=str((serve_root / rel_html_path).resolve()),
        output_path=str(output_mp4),
        format=fmt_preset,
        quality=quality.value,  # type: ignore[arg-type]
        frame_format=frame_format.value,  # type: ignore[arg-type]
        width=int(media_info["width"]),
        height=int(media_info["height"]),
        fps=resolved_fps,
        total_frames=total_frames,
        workers=min(worker_count, total_frames),
        video_codec=str(media_info["video_codec"]),
        pix_fmt=str(media_info["pix_fmt"]),
        audio_codec=media_info["audio_codec"],  # type: ignore[arg-type]
        audio_source=str(resolved_audio) if resolved_audio is not None else None,
        duration_sec=round(float(media_info["duration_sec"]), 3),
        file_size_bytes=int(media_info["file_size_bytes"]),
        elapsed_sec=round(time.perf_counter() - started_at, 3),
    )

    report_path = output_mp4.parent / "render_report.json"
    atomic_write_text(report_path, report.model_dump_json(indent=2) + "\n")

    if is_slug_mode:
        update_stage(
            slug_label,
            PipelineStage.RENDER,
            StageStatus.PASSED,
            notes=(
                f"Rendered {report.width}x{report.height}@{report.fps}fps "
                f"({report.total_frames} frames, {report.duration_sec:.2f}s) -> {output_mp4.name}"
            ),
            error_count=0,
            advance_on_pass=True,
            root=root,
        )

    return report
