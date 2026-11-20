"""Playwright visual QA snapshotter, beat-to-beat stage diffing, and contact sheet builder."""

from __future__ import annotations

import math
import threading
from enum import StrEnum
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat
from playwright.sync_api import sync_playwright

from cm.models import (
    FORMAT_SPECS,
    BeatDiffCheck,
    FormatPreset,
    KeyframeAudit,
    PipelineStage,
    QaReport,
    QaViolation,
    StageStatus,
)
from cm.paths import atomic_write_text, resolve_engine_dir, run_dir
from cm.scene import create_preview_server, resolve_preview_target
from cm.state import load_state, update_stage


class SnapshotMode(StrEnum):
    """Keyframe capture granularity for cm snapshot."""

    ALL = "all"
    MID = "mid"


def compute_image_stddev(image: Image.Image) -> float:
    """Compute maximum RGB channel standard deviation to detect blank frames."""
    stat = ImageStat.Stat(image.convert("RGB"))
    return round(max(stat.stddev), 3)


def compute_stage_diff_pct(
    img_a: Image.Image,
    img_b: Image.Image,
    crop_box: tuple[int, int, int, int] | None = None,
) -> float:
    """Compute percentage pixel difference across the diagram stage between two frames."""
    a = img_a.convert("RGB")
    b = img_b.convert("RGB")

    if crop_box is not None:
        max_w = min(a.width, b.width)
        max_h = min(a.height, b.height)
        x1 = max(0, min(crop_box[0], max_w - 1))
        y1 = max(0, min(crop_box[1], max_h - 1))
        x2 = max(x1 + 1, min(crop_box[2], max_w))
        y2 = max(y1 + 1, min(crop_box[3], max_h))
        a = a.crop((x1, y1, x2, y2))
        b = b.crop((x1, y1, x2, y2))

    w, h = a.size
    scale = min(1.0, 320.0 / max(w, h))
    target_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    a_sm = a.resize(target_size, Image.Resampling.BILINEAR)
    b_sm = b.resize(target_size, Image.Resampling.BILINEAR)

    diff = ImageChops.difference(a_sm, b_sm)
    means = ImageStat.Stat(diff).mean
    return round((sum(means) / (3.0 * 255.0)) * 100.0, 3)


def _draw_dashed_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    dash: int = 8,
    gap: int = 6,
) -> None:
    x1, y1, x2, y2 = box
    step = dash + gap
    for x in range(x1, x2, step):
        end_x = min(x + dash, x2)
        draw.line([(x, y1), (end_x, y1)], fill=color, width=1)
        draw.line([(x, y2), (end_x, y2)], fill=color, width=1)
    for y in range(y1, y2, step):
        end_y = min(y + dash, y2)
        draw.line([(x1, y), (x1, end_y)], fill=color, width=1)
        draw.line([(x2, y), (x2, end_y)], fill=color, width=1)


def build_contact_sheet(
    entries: list[tuple[KeyframeAudit, Path, float | None, bool]],
    output_path: Path,
    fmt: FormatPreset,
    draw_safe_guides: bool = True,
) -> Path:
    """Assemble a grid contact sheet PNG with per-beat PASS/FAIL borders and diff stats."""
    if fmt == FormatPreset.SHORT_9X16:
        cols, tile_w, tile_h = 3, 360, 640
    else:
        cols, tile_w, tile_h = 2, 640, 360

    pad = 24
    header_h = 44
    rows = max(1, math.ceil(len(entries) / cols))
    sheet_w = pad + cols * (tile_w + pad)
    sheet_h = pad + rows * (header_h + tile_h + pad)

    canvas = Image.new("RGB", (sheet_w, sheet_h), (7, 11, 20))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    spec = FORMAT_SPECS[fmt]

    for idx, (kf, png_path, diff_pct, beat_passed) in enumerate(entries):
        row_idx = idx // cols
        col_idx = idx % cols
        x0 = pad + col_idx * (tile_w + pad)
        y0 = pad + row_idx * (header_h + tile_h + pad)
        tile_y = y0 + header_h

        border_color = (34, 197, 94) if beat_passed else (239, 68, 68)
        status_str = "PASS" if beat_passed else "FAIL"
        diff_str = f" | d={diff_pct:.1f}%" if diff_pct is not None else ""

        draw.text((x0, y0 + 4), f"#{idx + 1:02d} {kf.beat_id} | t={kf.t:.1f}s", fill=(248, 250, 252), font=font)
        draw.text((x0, y0 + 22), f"{status_str}{diff_str}", fill=border_color, font=font)

        with Image.open(png_path) as frame_img:
            resized = frame_img.convert("RGB").resize((tile_w, tile_h), Image.Resampling.LANCZOS)
            canvas.paste(resized, (x0, tile_y))

        if draw_safe_guides:
            sx = tile_w / spec.width
            sy = tile_h / spec.height
            gx1 = x0 + round(spec.safe_zone.left * sx)
            gy1 = tile_y + round(spec.safe_zone.top * sy)
            gx2 = x0 + round((spec.width - spec.safe_zone.right) * sx)
            gy2 = tile_y + round((spec.height - spec.safe_zone.bottom) * sy)
            _draw_dashed_rect(draw, (gx1, gy1, gx2, gy2), color=(56, 189, 248))

        draw.rectangle(
            [x0 - 3, tile_y - 3, x0 + tile_w + 2, tile_y + tile_h + 2],
            outline=border_color,
            width=4,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG")
    return output_path


def resolve_snapshot_target(
    target: str,
    out_dir: Path | None = None,
    root: Path | None = None,
) -> tuple[Path, Path, Path, str, bool]:
    """Resolve snapshot target into (serve_root, rel_html, qa_dir, slug_label, is_slug)."""
    serve_root, rel_html_path = resolve_preview_target(target, root)
    candidate = Path(target)
    looks_like_path = (
        target.lower().endswith((".html", ".htm"))
        or "/" in target
        or "\\" in target
        or candidate.exists()
    )
    is_slug_mode = not looks_like_path

    if is_slug_mode:
        qa_dir = (out_dir or (run_dir(target, root) / "qa")).resolve()
        slug_label = target
    else:
        html_path = (serve_root / rel_html_path).resolve()
        qa_dir = (out_dir or (html_path.parent / f"{html_path.stem}_qa")).resolve()
        slug_label = html_path.stem

    return serve_root, rel_html_path, qa_dir, slug_label, is_slug_mode


def run_snapshot(
    target: str,
    mode: SnapshotMode = SnapshotMode.ALL,
    out_dir: Path | None = None,
    min_beat_diff_pct: float = 0.5,
    safe_guides: bool = True,
    root: Path | None = None,
) -> QaReport:
    """Capture headless Chromium keyframes, audit layout/diffs, and emit QaReport."""
    serve_root, rel_html_path, qa_dir, slug_label, is_slug_mode = resolve_snapshot_target(
        target, out_dir=out_dir, root=root
    )

    frames_dir = qa_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for stale_png in frames_dir.glob("*.png"):
        stale_png.unlink()

    server = create_preview_server(
        serve_root=serve_root,
        port=0,
        engine_fallback=resolve_engine_dir(root),
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    keyframe_audits: list[KeyframeAudit] = []
    beat_order: list[str] = []
    rep_keyframe_by_beat: dict[str, tuple[KeyframeAudit, Path, Image.Image]] = {}
    stage_crop_box: tuple[int, int, int, int] | None = None
    fmt = FormatPreset.SHORT_9X16
    width = 1080
    height = 1920

    try:
        url = f"http://127.0.0.1:{port}/{rel_html_path.as_posix()}"
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                pending_console: list[str] = []
                pending_page_errs: list[str] = []
                page.on(
                    "console",
                    lambda msg: pending_console.append(msg.text)
                    if msg.type == "error"
                    else None,
                )
                page.on("pageerror", lambda exc: pending_page_errs.append(str(exc)))

                page.goto(url, wait_until="load")

                if not page.evaluate("() => Boolean(window.__HARNESS__)"):
                    raise RuntimeError(
                        f"Scene missing window.__HARNESS__ runtime: {rel_html_path.as_posix()}"
                    )

                harness_state = page.evaluate("() => window.__HARNESS__.getState()")
                fmt = FormatPreset(harness_state["format"])
                width = int(harness_state["width"])
                height = int(harness_state["height"])
                if (width, height) != (1080, 1920):
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("() => window.__HARNESS__.seek(0)")

                keyframes: list[dict[str, object]] = page.evaluate(
                    "() => window.__HARNESS__.getKeyframes()"
                )
                if not keyframes:
                    raise RuntimeError(
                        f"Scene produced 0 keyframes (check blueprint initialization): {rel_html_path.as_posix()}"
                    )

                if mode == SnapshotMode.MID:
                    keyframes = [kf for kf in keyframes if kf["label"] == "mid"]

                stage_rect = page.evaluate(
                    """() => {
                        const el = document.querySelector('.cm-stage-wrap');
                        if (!el) return null;
                        const r = el.getBoundingClientRect();
                        return [Math.floor(r.left), Math.floor(r.top), Math.ceil(r.right), Math.ceil(r.bottom)];
                    }"""
                )
                if stage_rect is not None:
                    stage_crop_box = (
                        int(stage_rect[0]),
                        int(stage_rect[1]),
                        int(stage_rect[2]),
                        int(stage_rect[3]),
                    )

                for idx, kf in enumerate(keyframes):
                    beat_id = str(kf["beatId"])
                    beat_index = int(kf["beatIndex"])  # type: ignore[arg-type]
                    label = str(kf["label"])
                    t_val = float(kf["t"])  # type: ignore[arg-type]

                    page.evaluate("(t) => window.__HARNESS__.seek(t)", t_val)
                    audit = page.evaluate("() => window.__HARNESS__.auditLayout()")

                    frame_filename = f"{idx + 1:03d}_{beat_id}_{label}.png"
                    frame_path = frames_dir / frame_filename
                    page.screenshot(path=str(frame_path), type="png")

                    with Image.open(frame_path) as img:
                        img.load()
                        stddev = compute_image_stddev(img)
                        img_copy = img.copy()

                    frame_violations: list[QaViolation] = [
                        QaViolation(
                            code=str(v["code"]),
                            beat_id=beat_id,
                            label=label,
                            t=t_val,
                            element=str(v.get("element", "")),
                            message=str(v["message"]),
                        )
                        for v in audit.get("violations", [])
                    ]

                    for msg in pending_console:
                        frame_violations.append(
                            QaViolation(
                                code="CONSOLE_ERROR",
                                beat_id=beat_id,
                                label=label,
                                t=t_val,
                                element="console",
                                message=msg,
                            )
                        )
                    pending_console.clear()

                    for msg in pending_page_errs:
                        frame_violations.append(
                            QaViolation(
                                code="PAGE_ERROR",
                                beat_id=beat_id,
                                label=label,
                                t=t_val,
                                element="page",
                                message=msg,
                            )
                        )
                    pending_page_errs.clear()

                    if stddev < 2.0:
                        frame_violations.append(
                            QaViolation(
                                code="BLANK_FRAME",
                                beat_id=beat_id,
                                label=label,
                                t=t_val,
                                element="cm-canvas",
                                message=f"Frame appears uniform/blank (pixel stddev {stddev:.3f} < 2.0).",
                            )
                        )

                    kf_audit = KeyframeAudit(
                        beat_id=beat_id,
                        beat_index=beat_index,
                        label=label,
                        t=t_val,
                        frame_path=f"frames/{frame_filename}",
                        passed=len(frame_violations) == 0,
                        stage_element_count=int(audit.get("stageElementCount", 0)),
                        pixel_stddev=stddev,
                        violations=frame_violations,
                    )
                    keyframe_audits.append(kf_audit)

                    if beat_id not in beat_order:
                        beat_order.append(beat_id)
                    if beat_id not in rep_keyframe_by_beat or label == "mid":
                        rep_keyframe_by_beat[beat_id] = (kf_audit, frame_path, img_copy)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    beat_diffs: list[BeatDiffCheck] = []
    diff_from_prev: dict[str, float | None] = {beat_order[0]: None}
    static_violations: list[QaViolation] = []
    failed_beats: set[str] = {
        kf.beat_id for kf in keyframe_audits if not kf.passed
    }

    for prev_id, curr_id in zip(beat_order[:-1], beat_order[1:], strict=True):
        prev_img = rep_keyframe_by_beat[prev_id][2]
        curr_img = rep_keyframe_by_beat[curr_id][2]
        diff_pct = compute_stage_diff_pct(prev_img, curr_img, stage_crop_box)
        diff_ok = diff_pct >= min_beat_diff_pct
        diff_from_prev[curr_id] = diff_pct
        beat_diffs.append(
            BeatDiffCheck(
                from_beat_id=prev_id,
                to_beat_id=curr_id,
                diff_pct=diff_pct,
                passed=diff_ok,
            )
        )
        if not diff_ok:
            failed_beats.add(curr_id)
            static_violations.append(
                QaViolation(
                    code="STATIC_BEAT_VISUAL",
                    beat_id=curr_id,
                    label="mid",
                    t=rep_keyframe_by_beat[curr_id][0].t,
                    element="cm-stage-wrap",
                    message=(
                        f"Stage visual changed only {diff_pct:.3f}% from '{prev_id}' "
                        f"to '{curr_id}' (min {min_beat_diff_pct:.2f}%)."
                    ),
                )
            )

    sheet_entries = [
        (
            rep_keyframe_by_beat[bid][0],
            rep_keyframe_by_beat[bid][1],
            diff_from_prev[bid],
            bid not in failed_beats,
        )
        for bid in beat_order
    ]
    contact_sheet_path = qa_dir / "contact_sheet.png"
    build_contact_sheet(
        entries=sheet_entries,
        output_path=contact_sheet_path,
        fmt=fmt,
        draw_safe_guides=safe_guides,
    )

    all_violations: list[QaViolation] = []
    for kf in keyframe_audits:
        all_violations.extend(kf.violations)
    all_violations.extend(static_violations)

    report = QaReport(
        slug=slug_label,
        scene_path=rel_html_path.as_posix(),
        format=fmt,
        width=width,
        height=height,
        mode=mode.value,  # type: ignore[arg-type]
        passed=len(all_violations) == 0,
        contact_sheet_path=str(contact_sheet_path),
        frame_count=len(keyframe_audits),
        error_count=len(all_violations),
        beat_diffs=beat_diffs,
        keyframes=keyframe_audits,
        violations=all_violations,
    )

    report_path = qa_dir / "report.json"
    atomic_write_text(report_path, report.model_dump_json(indent=2) + "\n")

    if is_slug_mode:
        state = load_state(target, root)
        scene_rec = state.stages.get(PipelineStage.SCENE)
        if report.passed and (scene_rec is None or scene_rec.status != StageStatus.PASSED):
            update_stage(
                slug=target,
                stage=PipelineStage.SCENE,
                status=StageStatus.PASSED,
                notes="Verified by cm snapshot",
                advance_on_pass=False,
                root=root,
            )
        notes = (
            f"QA passed ({report.frame_count} keyframes, 0 violations)"
            if report.passed
            else f"QA failed with {report.error_count} violation(s)"
        )
        update_stage(
            slug=target,
            stage=PipelineStage.QA,
            status=StageStatus.PASSED if report.passed else StageStatus.FAILED,
            notes=notes,
            error_count=report.error_count,
            root=root,
        )

    return report
