"""Unified agent-friendly CLI for Content Machine 3.0."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cm.catalog import add_idea, find_idea, load_catalog, set_idea_status
from cm.models import (
    PIPELINE_ORDER,
    Blueprint,
    Catalog,
    FormatPreset,
    IdeaStatus,
    PipelineStage,
    RunState,
    StageRecord,
    StageStatus,
    TopicDomain,
)
from cm.paths import run_dir
from cm.state import compute_next_action, init_run, list_runs, load_state, update_stage
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
app.add_typer(ideas_app, name="ideas")

console = Console()

STATUS_ICONS: dict[StageStatus, str] = {
    StageStatus.PASSED: "[green][x][/green]",
    StageStatus.IN_PROGRESS: "[cyan][>][/cyan]",
    StageStatus.FAILED: "[red][!][/red]",
    StageStatus.PENDING: "[dim][ ][/dim]",
    StageStatus.SKIPPED: "[yellow][-][/yellow]",
}


class SchemaModel(str, Enum):
    """Exportable JSON Schema model names."""

    BLUEPRINT = "blueprint"
    STATE = "state"
    CATALOG = "catalog"


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
    fmt: FormatPreset = typer.Option(FormatPreset.SHORT_9X16, "--format", help="Video format preset"),
    domain: TopicDomain = typer.Option(TopicDomain.CS, "--domain", help="Topic domain"),
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
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
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
        runs = list_runs()
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
                ]
            }
            typer.echo(json.dumps(payload, indent=2))
            return

        if not runs:
            console.print("[dim]No runs found in runs/. Start one with: cm new <slug>[/dim]")
            return

        table = Table(title="Content Machine 3.0 Runs")
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
        return

    try:
        state = load_state(slug)
    except FileNotFoundError as exc:
        _fail(str(exc), as_json)

    next_action = compute_next_action(state)
    if as_json:
        payload = state.model_dump(mode="json")
        payload["next_action"] = next_action
        typer.echo(json.dumps(payload, indent=2))
        return

    table = Table(title=f"Run: {state.slug} ({state.title})")
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
    console.print(Panel(next_action, title="Next Action", border_style="cyan"))


@app.command()
def validate(
    target: str = typer.Argument(..., help="Run slug or path to a blueprint JSON file"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Validate a blueprint against Pydantic contracts and retention rules."""
    candidate = Path(target)
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

    catalog = load_catalog()
    report = validate_blueprint_file(bp_path, catalog=catalog)

    if slug_mode:
        notes = (
            f"Validated cleanly ({report.metrics.estimated_duration_sec:.1f}s, "
            f"{report.metrics.iteration_count} iterations)"
            if report.passed and report.metrics is not None
            else f"{len(report.errors)} validation error(s)"
        )
        update_stage(
            slug=target,
            stage=PipelineStage.BLUEPRINT,
            status=StageStatus.PASSED if report.passed else StageStatus.FAILED,
            notes=notes,
            error_count=len(report.errors),
        )

    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        if not report.passed:
            raise typer.Exit(code=1)
        return

    badge = "[green][PASS][/green]" if report.passed else "[red][FAIL][/red]"
    console.print(
        f"{badge} {report.target} — {len(report.errors)} error(s), {len(report.warnings)} warning(s)"
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
        help="Schema to export: blueprint, state, or catalog",
    ),
) -> None:
    """Print the JSON Schema for a Content Machine contract model."""
    model_map = {
        SchemaModel.BLUEPRINT: Blueprint,
        SchemaModel.STATE: RunState,
        SchemaModel.CATALOG: Catalog,
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
    if len(failure) < 3:
        _fail("At least 3 --failure steps are required for a Build->Fail->Upgrade chain.", as_json)

    try:
        entry, warnings = add_idea(
            title=title,
            topic_domain=domain,
            hook=hook,
            failure_chain=failure,
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
    catalog = load_catalog()
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

    table = Table(title="Channel Idea Backlog")
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
    catalog = load_catalog()
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


if __name__ == "__main__":
    app()
