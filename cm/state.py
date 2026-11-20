"""File-backed state machine for video runs under runs/<slug>/."""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from cm.models import (
    FORMAT_SPECS,
    PIPELINE_ORDER,
    Beat,
    BeatKind,
    Blueprint,
    FormatPreset,
    IdeaEntry,
    PipelineStage,
    RunState,
    SfxCue,
    SfxTrigger,
    StageRecord,
    StageStatus,
    SystemStatus,
    TopicDomain,
)
from cm.paths import atomic_write_text, run_dir, runs_dir

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def default_stages() -> dict[PipelineStage, StageRecord]:
    """Build initial stage checkpoint records for a new run."""
    now = datetime.now(UTC)
    return {
        PipelineStage.IDEATE: StageRecord(status=StageStatus.PASSED, updated_at=now),
        PipelineStage.BLUEPRINT: StageRecord(
            status=StageStatus.IN_PROGRESS,
            updated_at=now,
            artifact="blueprint.json",
        ),
        PipelineStage.AUDIO: StageRecord(artifact="audio/timeline.json"),
        PipelineStage.SCENE: StageRecord(artifact="scene.html"),
        PipelineStage.QA: StageRecord(artifact="qa/report.json"),
        PipelineStage.RENDER: StageRecord(artifact="render/final.mp4"),
        PipelineStage.PUBLISH: StageRecord(artifact="publish_receipt.json"),
    }


def build_starter_blueprint(
    slug: str,
    title: str,
    topic_domain: TopicDomain,
    fmt: FormatPreset,
    idea: IdeaEntry | None = None,
) -> Blueprint:
    """Create a scaffold blueprint containing explicit TODO markers to complete."""
    spec = FORMAT_SPECS[fmt]
    chain = (
        list(idea.failure_chain)
        if idea is not None
        else [f"Failure mode {idx}" for idx in range(1, spec.min_iterations + 1)]
    )
    if len(chain) < spec.min_iterations:
        chain.extend(
            f"Failure mode {idx}" for idx in range(len(chain) + 1, spec.min_iterations + 1)
        )

    hook_text = (
        f"TODO: Refine hook - {idea.hook}"
        if idea is not None
        else f"TODO: Write a sharp <=4s visual hook about {title} with no generic opener."
    )

    beats: list[Beat] = [
        Beat(
            id="b01_hook",
            kind=BeatKind.HOOK,
            version_label="v0.1",
            system_status=SystemStatus.INITIALIZING,
            narration=hook_text,
            on_screen_headline=f"TODO: {title[:30]}",
            visual_state="TODO: Empty blueprint grid introducing the core system node.",
            visual_diff="Reveal initial baseline system node and curiosity question.",
            pause_after_ms=150,
            sfx=[SfxTrigger(cue=SfxCue.POP_BUILD, at="start")],
        ),
        Beat(
            id="b02_v1_build",
            kind=BeatKind.BUILD,
            version_label="v1.0",
            system_status=SystemStatus.BUILDING,
            narration="TODO: Describe the naive v1.0 architecture in 1-2 short plain-English sentences.",
            on_screen_headline="TODO: v1 Naive Design",
            visual_state="TODO: Naive v1.0 diagram with basic input -> core node -> storage.",
            visual_diff="Add v1.0 client and baseline processing component.",
            pause_after_ms=180,
            sfx=[SfxTrigger(cue=SfxCue.POP_BUILD, at="start")],
        ),
    ]

    for idx, failure_desc in enumerate(chain, start=1):
        fail_version = f"v{idx}.0"
        next_version = f"v{idx + 1}.0"
        beats.append(
            Beat(
                id=f"b{len(beats) + 1:02d}_v{idx}_fail",
                kind=BeatKind.FAIL,
                version_label=fail_version,
                system_status=SystemStatus.COMPROMISED,
                narration=f"TODO: Explain break #{idx} ({failure_desc}) clearly for non-technical viewers.",
                on_screen_headline=f"TODO: v{idx} Failure",
                visual_state=f"TODO: Diagram showing {fail_version} breaking under {failure_desc}.",
                visual_diff=f"Inject stress test for '{failure_desc}' and stamp {fail_version} failure.",
                stress_test_trigger=failure_desc,
                failure_stamp=f"FAIL V{idx}",
                pause_after_ms=200,
                sfx=[SfxTrigger(cue=SfxCue.IMPACT_FAIL, at="start")],
            )
        )
        beats.append(
            Beat(
                id=f"b{len(beats) + 1:02d}_v{idx + 1}_upgrade",
                kind=BeatKind.UPGRADE,
                version_label=next_version,
                system_status=(
                    SystemStatus.RESILIENT if idx == len(chain) else SystemStatus.UPGRADED
                ),
                narration=f"TODO: Explain the {next_version} architectural upgrade that defeats break #{idx}.",
                on_screen_headline=f"TODO: {next_version} Upgrade",
                visual_state=f"TODO: Upgraded diagram {next_version} protecting against '{failure_desc}'.",
                visual_diff=f"Upgrade architecture from {fail_version} to {next_version} to resolve '{failure_desc}'.",
                pause_after_ms=180,
                sfx=[SfxTrigger(cue=SfxCue.LOCK_UPGRADE, at="start")],
            )
        )

    final_version = f"v{len(chain) + 1}.0"
    beats.append(
        Beat(
            id=f"b{len(beats) + 1:02d}_payoff",
            kind=BeatKind.PAYOFF,
            version_label=final_version,
            system_status=SystemStatus.RESILIENT,
            narration="TODO: Deliver a punchy 1-sentence payoff showing why the final design works.",
            on_screen_headline="TODO: Final Takeaway",
            visual_state=f"TODO: Full {final_version} system running cleanly with green resilient HUD.",
            visual_diff=f"Highlight complete {final_version} pipeline and final insight badge.",
            pause_after_ms=250,
            sfx=[SfxTrigger(cue=SfxCue.CHIME_FIX, at="start")],
        )
    )

    return Blueprint(
        slug=slug,
        title=title,
        topic_domain=topic_domain,
        format=fmt,
        core_premise=f"TODO: Summarize why {title} breaks when built naively and how iterative engineering fixes it.",
        persistent_subject=f"TODO: Describe the single persistent 2D diagram for {title}.",
        beats=beats,
    )


def init_run(
    slug: str,
    title: str | None = None,
    topic_domain: TopicDomain | None = None,
    fmt: FormatPreset | None = None,
    from_blueprint: Path | None = None,
    idea: IdeaEntry | None = None,
    force: bool = False,
    root: Path | None = None,
) -> tuple[RunState, Path]:
    """Create a new runs/<slug>/ workspace with state.json and blueprint.json."""
    if not SLUG_PATTERN.match(slug):
        raise ValueError(
            f"Invalid slug '{slug}': must be lowercase alphanumeric kebab-case"
        )

    rdir = run_dir(slug, root)
    if rdir.exists() and not force:
        raise FileExistsError(f"Run '{slug}' already exists at {rdir}")

    bp_text_to_write: str | None = None
    parsed_title: str | None = None
    parsed_domain: TopicDomain | None = None
    parsed_fmt: FormatPreset | None = None

    if from_blueprint is not None:
        if not from_blueprint.is_file():
            raise FileNotFoundError(f"Source blueprint not found: {from_blueprint}")
        raw_text = from_blueprint.read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, dict):
            if isinstance(parsed.get("title"), str) and parsed["title"].strip():
                parsed_title = parsed["title"].strip()
            if isinstance(parsed.get("topic_domain"), str):
                with contextlib.suppress(ValueError):
                    parsed_domain = TopicDomain(parsed["topic_domain"])
            if isinstance(parsed.get("format"), str):
                with contextlib.suppress(ValueError):
                    parsed_fmt = FormatPreset(parsed["format"])
            parsed["slug"] = slug
            bp_text_to_write = json.dumps(parsed, indent=2) + "\n"
        else:
            bp_text_to_write = raw_text

    resolved_domain = topic_domain or parsed_domain or (
        idea.topic_domain if idea is not None else TopicDomain.CS
    )
    resolved_fmt = fmt or parsed_fmt or (
        idea.format if idea is not None else FormatPreset.SHORT_9X16
    )
    resolved_title = title or parsed_title or (
        idea.title if idea is not None else slug.replace("-", " ").title()
    )

    for sub in ("audio", "qa", "render"):
        (rdir / sub).mkdir(parents=True, exist_ok=True)

    bp_path = rdir / "blueprint.json"
    if bp_text_to_write is not None:
        atomic_write_text(bp_path, bp_text_to_write)
    else:
        starter = build_starter_blueprint(
            slug=slug,
            title=resolved_title,
            topic_domain=resolved_domain,
            fmt=resolved_fmt,
            idea=idea,
        )
        atomic_write_text(bp_path, starter.model_dump_json(indent=2) + "\n")

    now = datetime.now(UTC)
    state = RunState(
        slug=slug,
        title=resolved_title,
        topic_domain=resolved_domain,
        format=resolved_fmt,
        idea_id=idea.id if idea is not None else None,
        created_at=now,
        updated_at=now,
        current_stage=PipelineStage.BLUEPRINT,
        stages=default_stages(),
    )
    save_state(state, root)
    return state, rdir


def load_state(slug: str, root: Path | None = None) -> RunState:
    """Load RunState from runs/<slug>/state.json."""
    state_file = run_dir(slug, root) / "state.json"
    if not state_file.is_file():
        raise FileNotFoundError(f"Run '{slug}' not found (missing {state_file})")
    try:
        return RunState.model_validate_json(state_file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValidationError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Corrupt state file for run '{slug}' at {state_file}: {exc}") from exc


def save_state(state: RunState, root: Path | None = None) -> Path:
    """Persist RunState atomically to runs/<slug>/state.json."""
    state.updated_at = datetime.now(UTC)
    state_file = run_dir(state.slug, root) / "state.json"
    return atomic_write_text(state_file, state.model_dump_json(indent=2) + "\n")


def update_stage(
    slug: str,
    stage: PipelineStage,
    status: StageStatus,
    notes: str | None = None,
    error_count: int = 0,
    advance_on_pass: bool = True,
    root: Path | None = None,
) -> RunState:
    """Update a stage checkpoint and advance current_stage on pass."""
    state = load_state(slug, root)
    now = datetime.now(UTC)
    record = state.stages.get(stage, StageRecord())
    record.status = status
    record.updated_at = now
    record.notes = notes
    record.error_count = error_count
    state.stages[stage] = record

    if status == StageStatus.PASSED and advance_on_pass:
        idx = PIPELINE_ORDER.index(stage)
        state.current_stage = (
            PIPELINE_ORDER[idx + 1] if idx + 1 < len(PIPELINE_ORDER) else stage
        )
    elif status in (StageStatus.FAILED, StageStatus.IN_PROGRESS):
        state.current_stage = stage

    save_state(state, root)
    return state


def list_runs_checked(root: Path | None = None) -> tuple[list[RunState], list[str]]:
    """Return valid runs sorted by updated_at descending plus any corrupt-run error strings."""
    rdir = runs_dir(root)
    if not rdir.is_dir():
        return [], []

    runs: list[RunState] = []
    errors: list[str] = []
    for child in sorted(rdir.iterdir()):
        if child.is_dir() and (child / "state.json").is_file():
            try:
                runs.append(load_state(child.name, root))
            except ValueError as exc:
                errors.append(str(exc))

    runs.sort(key=lambda item: item.updated_at, reverse=True)
    return runs, errors


def list_runs(root: Path | None = None) -> list[RunState]:
    """Return all valid runs under runs/ sorted by updated_at descending."""
    runs, _ = list_runs_checked(root)
    return runs


def compute_next_action(state: RunState, root: Path | None = None) -> str:
    """Return the exact next CLI command or action for an agent or user."""
    _ = root
    bp_rec = state.stages.get(PipelineStage.BLUEPRINT, StageRecord())
    if bp_rec.status in (StageStatus.FAILED, StageStatus.IN_PROGRESS, StageStatus.PENDING):
        return f"Edit runs/{state.slug}/blueprint.json and run: cm validate {state.slug}"

    for stage in PIPELINE_ORDER:
        rec = state.stages.get(stage, StageRecord())
        if rec.status in (StageStatus.PASSED, StageStatus.SKIPPED):
            continue
        if stage == PipelineStage.AUDIO:
            return f"Generate TTS & audio timeline: cm tts {state.slug} (Milestone 4)"
        if stage == PipelineStage.SCENE:
            return f"Build runs/{state.slug}/scene.html using engine/ primitives (Milestone 2)"
        if stage == PipelineStage.QA:
            return f"Run visual QA snapshotter: cm snapshot {state.slug} (Milestone 3)"
        if stage == PipelineStage.RENDER:
            return f"Render MP4 video: cm render {state.slug} (Milestone 5)"
        if stage == PipelineStage.PUBLISH:
            return f"Publish Short/Reel: cm publish {state.slug} --dry-run (Milestone 7)"

    return "Complete! Published and recorded in channels/catalog.json."
