"""Unified agent-friendly CLI for Content Machine 3.0."""

from __future__ import annotations

import json
import threading
import urllib.request
import webbrowser
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cm.audio import mix_run_audio
from cm.catalog import add_idea, find_idea, load_catalog, set_idea_status
from cm.models import (
    PIPELINE_ORDER,
    AudioTimeline,
    Blueprint,
    Catalog,
    FormatPreset,
    IdeaStatus,
    PipelineStage,
    QaReport,
    RunState,
    StageRecord,
    StageStatus,
    TopicDomain,
)
from cm.paths import resolve_engine_dir, run_dir
from cm.scene import create_preview_server, resolve_preview_target, scaffold_scene
from cm.sfx import init_sfx_pack, list_sfx_slots
from cm.snapshot import SnapshotMode, run_snapshot
from cm.state import (
    compute_next_action,
    init_run,
    list_runs_checked,
    load_state,
    update_stage,
)
from cm.tts import TtsProvider, synthesize_run_tts
from cm.validators import validate_blueprint_file

app = typer.Typer(
    name="cm",
    help="Content Machine 3.0 - Agent-native 2D CS/AI content harness",
    no_args_is_help=True,
)
ideas_app = typer.Typer(
    name="ideas",
    help="Manage the idea backlog and channel deduplication",
    no_args_is_help=True,
)
scene_app = typer.Typer(
    name="scene",
    help="Scaffold and inspect 2D HTML scene files",
    no_args_is_help=True,
)
sfx_app = typer.Typer(
    name="sfx",
    help="Manage semantic sound-effect packs and procedural synth cues",
    no_args_is_help=True,
)
audio_app = typer.Typer(
    name="audio",
    help="Mix voiceover, SFX cues, and background music to -14 LUFS",
    no_args_is_help=True,
)
app.add_typer(ideas_app, name="ideas")
app.add_typer(scene_app, name="scene")
app.add_typer(sfx_app, name="sfx")
app.add_typer(audio_app, name="audio")

console = Console()

STATUS_ICONS: dict[StageStatus, str] = {
    StageStatus.PASSED: "[green][x][/green]",
    StageStatus.IN_PROGRESS: "[cyan][>][/cyan]",
    StageStatus.FAILED: "[red][!][/red]",
    StageStatus.PENDING: "[dim][ ][/dim]",
    StageStatus.SKIPPED: "[yellow][-][/yellow]",
}


class SchemaModel(StrEnum):
    """Exportable JSON Schema model names."""

    BLUEPRINT = "blueprint"
    STATE = "state"
    CATALOG = "catalog"
    QA = "qa"
    TIMELINE = "timeline"


def _fail(message: str, as_json: bool, code: int = 1) -> NoReturn:
    if as_json:
        typer.echo(json.dumps({"error": message}, indent=2))
    else:
        console.print(f"[red][FAIL][/red] {message}")
    raise typer.Exit(code=code)


@app.command()
def new(
    slug: str = typer.Argument(..., help="Lowercase kebab-case run identifier (e.g. password-system)"),
    title: str | None = typer.Option(None, "--title", help="Video title"),
    fmt: FormatPreset | None = typer.Option(
        None, "--format", help="Video format preset (defaults to source blueprint or short_9x16)"
    ),
    domain: TopicDomain | None = typer.Option(
        None, "--domain", help="Topic domain (defaults to source blueprint or cs)"
    ),
    from_blueprint: Path | None = typer.Option(
        None, "--from-blueprint", help="Copy an existing blueprint JSON file into the run"
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing run workspace"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Initialize a new video run workspace under runs/<slug>/."""
    try:
        state, rdir = init_run(
            slug=slug,
            title=title,
            topic_domain=domain,
            fmt=fmt,
            from_blueprint=from_blueprint,
            force=force,
        )
    except (ValueError, FileExistsError, FileNotFoundError, UnicodeDecodeError) as exc:
        _fail(str(exc), as_json)

    next_action = compute_next_action(state)
    if as_json:
        payload = {
            "run_dir": str(rdir),
            "blueprint_path": str(rdir / "blueprint.json"),
            "state": state.model_dump(mode="json"),
            "next_action": next_action,
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(f"[green][OK][/green] Initialized run [bold]{state.slug}[/bold] at {rdir}")
    console.print(f"Blueprint: {rdir / 'blueprint.json'}")
    console.print(f"Next: {next_action}")


@app.command()
def status(
    slug: str | None = typer.Argument(None, help="Optional run slug to inspect"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Show status of all runs or detailed checkpoint state for a single run."""
    if slug is None:
        runs, corrupt_errors = list_runs_checked()
        if as_json:
            payload = {
                "runs": [
                    {
                        "slug": r.slug,
                        "title": r.title,
                        "topic_domain": r.topic_domain.value,
                        "format": r.format.value,
                        "current_stage": r.current_stage.value,
                        "updated_at": r.updated_at.isoformat(),
                        "next_action": compute_next_action(r),
                    }
                    for r in runs
                ],
                "errors": corrupt_errors,
            }
            typer.echo(json.dumps(payload, indent=2))
            return

        if not runs and not corrupt_errors:
            console.print("[dim]No runs found in runs/. Start one with: cm new <slug>[/dim]")
            return

        if runs:
            table = Table(title="Content Machine 3.0 Runs", box=box.ASCII)
            table.add_column("Slug", style="bold")
            table.add_column("Title")
            table.add_column("Format")
            table.add_column("Stage")
            table.add_column("Next Action")
            for r in runs:
                table.add_row(
                    r.slug,
                    r.title,
                    r.format.value,
                    r.current_stage.value,
                    compute_next_action(r),
                )
            console.print(table)

        for err in corrupt_errors:
            console.print(f"[yellow][WARN][/yellow] {err}")
        return

    try:
        state = load_state(slug)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc), as_json)

    next_action = compute_next_action(state)
    if as_json:
        payload = state.model_dump(mode="json")
        payload["next_action"] = next_action
        typer.echo(json.dumps(payload, indent=2))
        return

    table = Table(title=f"Run: {state.slug} ({state.title})", box=box.ASCII)
    table.add_column("State", width=6)
    table.add_column("Stage", style="bold")
    table.add_column("Status")
    table.add_column("Artifact")
    table.add_column("Notes")
    for stage in PIPELINE_ORDER:
        rec = state.stages.get(stage, StageRecord())
        icon = STATUS_ICONS[rec.status]
        table.add_row(
            icon,
            stage.value,
            rec.status.value,
            rec.artifact or "-",
            rec.notes or "",
        )
    console.print(table)
    console.print(Panel(next_action, title="Next Action", border_style="cyan", box=box.ASCII))


@app.command()
def validate(
    target: str = typer.Argument(..., help="Run slug or path to a blueprint JSON file"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Validate a blueprint against Pydantic contracts and retention rules."""
    candidate = Path(target)
    if candidate.is_dir():
        _fail(f"Expected a blueprint JSON file or run slug, got directory: {target}", as_json)

    is_file_target = (
        candidate.is_file()
        or target.lower().endswith(".json")
        or "/" in target
        or "\\" in target
    )

    slug_mode = not is_file_target
    if slug_mode:
        state_file = run_dir(target) / "state.json"
        if not state_file.is_file():
            _fail(f"Run '{target}' not found (missing {state_file})", as_json)
        bp_path = run_dir(target) / "blueprint.json"
    else:
        bp_path = candidate

    try:
        catalog = load_catalog()
    except ValueError as exc:
        _fail(str(exc), as_json)

    report = validate_blueprint_file(
        bp_path,
        catalog=catalog,
        expected_slug=target if slug_mode else None,
    )

    if slug_mode:
        notes = (
            f"Validated cleanly ({report.metrics.estimated_duration_sec:.1f}s, "
            f"{report.metrics.iteration_count} iterations)"
            if report.passed and report.metrics is not None
            else f"{len(report.errors)} validation error(s)"
        )
        try:
            update_stage(
                slug=target,
                stage=PipelineStage.BLUEPRINT,
                status=StageStatus.PASSED if report.passed else StageStatus.FAILED,
                notes=notes,
                error_count=len(report.errors),
            )
        except ValueError as exc:
            _fail(str(exc), as_json)

    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        if not report.passed:
            raise typer.Exit(code=1)
        return

    badge = "[green][PASS][/green]" if report.passed else "[red][FAIL][/red]"
    console.print(
        f"{badge} {report.target} - {len(report.errors)} error(s), {len(report.warnings)} warning(s)"
    )

    if report.metrics is not None:
        m = report.metrics
        console.print(
            f"Metrics: {m.beat_count} beats | {m.total_words} words | "
            f"~{m.estimated_duration_sec:.2f}s (hook {m.hook_duration_sec:.2f}s) | "
            f"{m.iteration_count} fail->upgrade iterations | versions: {' -> '.join(m.version_progression)}"
        )

    for err in report.errors:
        loc = f"[{err.beat_id}] " if err.beat_id else ""
        console.print(f"[red]  ERROR {err.code}[/red] {loc}{err.message}")
        console.print(f"    Fix: {err.suggestion}")

    for warn in report.warnings:
        loc = f"[{warn.beat_id}] " if warn.beat_id else ""
        console.print(f"[yellow]  WARN  {warn.code}[/yellow] {loc}{warn.message}")
        console.print(f"    Tip: {warn.suggestion}")

    if not report.passed:
        raise typer.Exit(code=1)


@app.command()
def schema(
    model: SchemaModel = typer.Argument(
        SchemaModel.BLUEPRINT,
        help="Schema to export: blueprint, state, catalog, qa, or timeline",
    ),
) -> None:
    """Print the JSON Schema for a Content Machine contract model."""
    model_map = {
        SchemaModel.BLUEPRINT: Blueprint,
        SchemaModel.STATE: RunState,
        SchemaModel.CATALOG: Catalog,
        SchemaModel.QA: QaReport,
        SchemaModel.TIMELINE: AudioTimeline,
    }
    typer.echo(json.dumps(model_map[model].model_json_schema(), indent=2))


@ideas_app.command("add")
def ideas_add(
    title: str = typer.Option(..., "--title", help="Video title"),
    domain: TopicDomain = typer.Option(..., "--domain", help="Topic domain"),
    hook: str = typer.Option(..., "--hook", help="Opening curiosity hook (<=4s)"),
    failure: list[str] = typer.Option(
        ...,
        "--failure",
        help="Failure->upgrade step (repeat at least 3 times)",
    ),
    slug: str | None = typer.Option(None, "--slug", help="Optional custom kebab-case slug"),
    fmt: FormatPreset = typer.Option(FormatPreset.SHORT_9X16, "--format", help="Video format preset"),
    score: int = typer.Option(8, "--score", min=1, max=10, help="Idea strength score (1-10)"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Add a CS/AI Build->Fail->Upgrade concept to channels/catalog.json."""
    cleaned_failures = [step.strip() for step in failure if step.strip()]
    if len(cleaned_failures) < 3 or len(cleaned_failures) != len(failure):
        _fail("At least 3 non-empty --failure steps are required for a Build->Fail->Upgrade chain.", as_json)

    try:
        entry, warnings = add_idea(
            title=title,
            topic_domain=domain,
            hook=hook,
            failure_chain=cleaned_failures,
            slug=slug,
            fmt=fmt,
            score=score,
        )
    except ValueError as exc:
        _fail(str(exc), as_json)

    if as_json:
        payload = {
            "idea": entry.model_dump(mode="json"),
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(f"[green][OK][/green] Added {entry.id} ({entry.slug}): {entry.title}")
    for w in warnings:
        console.print(f"[yellow][WARN][/yellow] {w}")


@ideas_app.command("list")
def ideas_list(
    status: IdeaStatus | None = typer.Option(None, "--status", help="Filter ideas by status"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """List ideas stored in channels/catalog.json."""
    try:
        catalog = load_catalog()
    except ValueError as exc:
        _fail(str(exc), as_json)

    items = catalog.ideas
    if status is not None:
        items = [i for i in items if i.status == status]

    if as_json:
        typer.echo(
            json.dumps({"ideas": [i.model_dump(mode="json") for i in items]}, indent=2)
        )
        return

    if not items:
        console.print("[dim]No matching ideas in channels/catalog.json.[/dim]")
        return

    table = Table(title="Channel Idea Backlog", box=box.ASCII)
    table.add_column("ID", style="bold")
    table.add_column("Slug")
    table.add_column("Domain")
    table.add_column("Score")
    table.add_column("Status")
    table.add_column("Title")
    for item in items:
        table.add_row(
            item.id,
            item.slug,
            item.topic_domain.value,
            str(item.score),
            item.status.value,
            item.title,
        )
    console.print(table)


@ideas_app.command("pick")
def ideas_pick(
    ref: str = typer.Argument(..., help="Idea ID (e.g. idea-001) or slug"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing run workspace"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Create a run workspace from an idea backlog entry and mark it in_progress."""
    try:
        catalog = load_catalog()
    except ValueError as exc:
        _fail(str(exc), as_json)

    idea = find_idea(catalog, ref)
    if idea is None:
        _fail(f"Idea '{ref}' not found in channels/catalog.json", as_json)

    try:
        state, rdir = init_run(
            slug=idea.slug,
            title=idea.title,
            topic_domain=idea.topic_domain,
            fmt=idea.format,
            idea=idea,
            force=force,
        )
    except (ValueError, FileExistsError) as exc:
        _fail(str(exc), as_json)

    updated_idea = set_idea_status(idea.id, IdeaStatus.IN_PROGRESS)
    next_action = compute_next_action(state)

    if as_json:
        payload = {
            "idea": updated_idea.model_dump(mode="json"),
            "run_dir": str(rdir),
            "state": state.model_dump(mode="json"),
            "next_action": next_action,
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(
        f"[green][OK][/green] Picked {updated_idea.id} -> initialized run '{state.slug}' at {rdir}"
    )
    console.print(f"Next: {next_action}")


@scene_app.command("init")
def scene_init(
    slug: str = typer.Argument(..., help="Run slug to scaffold scene.html for"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing scene.html"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Scaffold runs/<slug>/scene.html from runs/<slug>/blueprint.json."""
    try:
        scene_path = scaffold_scene(slug=slug, force=force)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        _fail(str(exc), as_json)

    next_action = f"Preview scene in browser: cm preview {slug}"
    if as_json:
        payload = {
            "slug": slug,
            "scene_path": str(scene_path),
            "next_action": next_action,
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(f"[green][OK][/green] Scaffolded {scene_path}")
    console.print(f"Next: {next_action}")


@app.command()
def preview(
    target: str = typer.Argument(..., help="Run slug or path to a scene.html file"),
    port: int = typer.Option(8765, "--port", min=0, max=65535, help="Local HTTP port (0 = ephemeral)"),
    no_open: bool = typer.Option(False, "--no-open", help="Do not launch default web browser"),
    once: bool = typer.Option(
        False,
        "--once",
        help="Verify HTTP serving and exit immediately without blocking",
    ),
) -> None:
    """Serve an interactive ?preview=1 scrubber for a 2D scene."""
    try:
        serve_root, rel_path = resolve_preview_target(target)
        server = create_preview_server(
            serve_root=serve_root,
            port=port,
            engine_fallback=resolve_engine_dir(),
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        _fail(str(exc), as_json=False)

    actual_port = server.server_address[1]
    base_url = f"http://127.0.0.1:{actual_port}/{rel_path.as_posix()}"
    preview_url = f"{base_url}?preview=1"
    console.print(f"[green][OK][/green] Preview URL: {preview_url}")

    if once:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(base_url, timeout=5) as resp:
                resp.read(1)
        except OSError as exc:  # pragma: no cover
            _fail(f"Preview self-check failed: {exc}", as_json=False)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        return

    if not no_open:  # pragma: no cover
        webbrowser.open(preview_url)
    console.print("Press Ctrl+C to stop preview server.")  # pragma: no cover
    try:  # pragma: no cover
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:  # pragma: no cover
        server.server_close()


@app.command()
def snapshot(
    target: str = typer.Argument(..., help="Run slug or path to a scene.html file"),
    mode: SnapshotMode = typer.Option(
        SnapshotMode.ALL,
        "--mode",
        help="Keyframe capture mode: all (start/mid/end) or mid",
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Optional custom QA output directory"
    ),
    min_diff: float = typer.Option(
        0.5,
        "--min-diff",
        min=0.0,
        max=100.0,
        help="Minimum % stage pixel diff between consecutive beats",
    ),
    safe_guides: bool = typer.Option(
        True,
        "--safe-guides/--no-safe-guides",
        help="Draw safe-zone guides on contact_sheet.png",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Capture headless Chromium keyframes, audit layout/diffs, and build contact sheet."""
    try:
        report = run_snapshot(
            target=target,
            mode=mode,
            out_dir=out,
            min_beat_diff_pct=min_diff,
            safe_guides=safe_guides,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        _fail(str(exc), as_json)

    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        if not report.passed:
            raise typer.Exit(code=1)
        return

    badge = "[green][PASS][/green]" if report.passed else "[red][FAIL][/red]"
    console.print(
        f"{badge} {report.slug} ({report.mode}) - {report.frame_count} frame(s), "
        f"{report.error_count} violation(s)"
    )
    console.print(f"Contact sheet: {report.contact_sheet_path}")

    if report.beat_diffs:
        diff_table = Table(title="Beat Stage Visual Diffs", box=box.ASCII)
        diff_table.add_column("Transition")
        diff_table.add_column("Diff %")
        diff_table.add_column("Status")
        for bd in report.beat_diffs:
            diff_table.add_row(
                f"{bd.from_beat_id} -> {bd.to_beat_id}",
                f"{bd.diff_pct:.2f}%",
                "PASS" if bd.passed else "FAIL",
            )
        console.print(diff_table)

    for viol in report.violations:
        loc = f"[{viol.beat_id}:{viol.label}] " if viol.beat_id else ""
        console.print(f"[red]  ERROR {viol.code}[/red] {loc}{viol.message}")

    if not report.passed:
        raise typer.Exit(code=1)


@sfx_app.command("init")
def sfx_init(
    force: bool = typer.Option(False, "--force", help="Regenerate all procedural WAV cues"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Initialize assets/sfx/manifest.json and procedural starter WAV files."""
    try:
        summary = init_sfx_pack(force=force)
    except ValueError as exc:
        _fail(str(exc), as_json)

    if as_json:
        typer.echo(json.dumps(summary, indent=2))
        return

    gen = summary["generated"]
    pres = summary["preserved"]
    console.print(
        f"[green][OK][/green] SFX pack ready at {summary['sfx_dir']} "
        f"(generated {len(gen)}, preserved {len(pres)})"  # type: ignore[arg-type]
    )


@sfx_app.command("list")
def sfx_list(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """List semantic SFX slots and check backing WAV availability."""
    try:
        slots = list_sfx_slots()
    except ValueError as exc:
        _fail(str(exc), as_json)

    if as_json:
        typer.echo(json.dumps({"slots": slots}, indent=2))
        return

    table = Table(title="Semantic SFX Cue Slots", box=box.ASCII)
    table.add_column("Cue", style="bold")
    table.add_column("Files")
    table.add_column("Status")
    table.add_column("Duration")
    for slot in slots:
        dur_str = f"{slot['duration_sec']:.3f}s" if slot["duration_sec"] is not None else "-"
        table.add_row(
            str(slot["cue"]),
            ", ".join(slot["files"]),  # type: ignore[arg-type]
            "READY" if slot["exists"] else "MISSING",
            dur_str,
        )
    console.print(table)


@audio_app.command("mix")
def audio_mix(
    slug: str = typer.Argument(..., help="Run slug with existing audio/timeline.json"),
    music: Path | None = typer.Option(None, "--music", help="Optional background music bed path"),
    music_gain_db: float = typer.Option(-22.0, "--music-gain-db", max=0.0, min=-45.0),
    target_lufs: float = typer.Option(-14.0, "--target-lufs", max=-6.0, min=-24.0),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Mix voiceover, beat SFX cues, and ducked music bed to -14 LUFS."""
    try:
        timeline = mix_run_audio(
            slug=slug,
            music_path=music,
            music_gain_db=music_gain_db,
            target_lufs=target_lufs,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        _fail(str(exc), as_json)

    if as_json:
        typer.echo(timeline.model_dump_json(indent=2))
        return

    console.print(
        f"[green][OK][/green] Mastered {slug}/audio/{timeline.master_audio_path} "
        f"({timeline.total_duration_sec:.2f}s, {len(timeline.sfx_events)} SFX, "
        f"{timeline.measured_lufs:.2f} LUFS)"
    )


@app.command()
def tts(
    slug: str = typer.Argument(..., help="Run slug to synthesize per-beat voiceover"),
    provider: TtsProvider = typer.Option(
        TtsProvider.AUTO,
        "--provider",
        help="TTS backend: auto (uses GEMINI_API_KEY), gemini, or mock",
    ),
    model: str | None = typer.Option(None, "--model", help="Override Gemini TTS model"),
    voice: str | None = typer.Option(None, "--voice", help="Override prebuilt voice name"),
    force: bool = typer.Option(False, "--force", help="Ignore SHA-256 clip cache"),
    no_mix: bool = typer.Option(False, "--no-mix", help="Skip automatic SFX + LUFS mastering"),
    music: Path | None = typer.Option(None, "--music", help="Optional background music bed path"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Synthesize per-beat TTS audio, build timeline.json, and optionally master mix."""
    try:
        timeline = synthesize_run_tts(
            slug=slug,
            provider=provider,
            model=model,
            voice_name=voice,
            force=force,
        )
        if no_mix:
            update_stage(
                slug=slug,
                stage=PipelineStage.AUDIO,
                status=StageStatus.IN_PROGRESS,
                notes=f"{timeline.provider} voice assembled ({len(timeline.beats)} beats; unmixed)",
                advance_on_pass=False,
            )
        else:
            timeline = mix_run_audio(slug=slug, music_path=music)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        _fail(str(exc), as_json)

    if as_json:
        typer.echo(timeline.model_dump_json(indent=2))
        return

    mix_info = (
        f"mastered {timeline.measured_lufs:.2f} LUFS"
        if timeline.measured_lufs is not None
        else "voice only (--no-mix)"
    )
    console.print(
        f"[green][OK][/green] TTS ({timeline.provider}) for '{slug}': "
        f"{len(timeline.beats)} beats, {timeline.total_duration_sec:.2f}s ({mix_info})"
    )


if __name__ == "__main__":
    app()
