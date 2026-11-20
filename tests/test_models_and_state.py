"""Tests for Pydantic models and the file-backed run state machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from cm.models import (
    FORMAT_SPECS,
    FormatPreset,
    PipelineStage,
    StageStatus,
    TopicDomain,
)
from cm.state import (
    compute_next_action,
    init_run,
    list_runs,
    load_state,
    update_stage,
)


def test_format_specs_have_expected_dimensions() -> None:
    short = FORMAT_SPECS[FormatPreset.SHORT_9X16]
    long = FORMAT_SPECS[FormatPreset.LONG_16X9]
    assert (short.width, short.height) == (1080, 1920)
    assert (long.width, long.height) == (1920, 1080)
    assert short.min_iterations == 3
    assert long.min_iterations == 5


def test_init_run_creates_workspace_and_starter_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    state, rdir = init_run(
        slug="rate-limiter",
        title="Building a Rate Limiter From Scratch",
        topic_domain=TopicDomain.SYSTEMS,
        fmt=FormatPreset.SHORT_9X16,
    )

    assert state.slug == "rate-limiter"
    assert state.current_stage == PipelineStage.BLUEPRINT
    assert state.stages[PipelineStage.IDEATE].status == StageStatus.PASSED
    assert state.stages[PipelineStage.BLUEPRINT].status == StageStatus.IN_PROGRESS
    assert (rdir / "state.json").is_file()
    assert (rdir / "blueprint.json").is_file()
    assert (rdir / "audio").is_dir()
    assert (rdir / "qa").is_dir()
    assert (rdir / "render").is_dir()


def test_init_run_duplicate_guard_and_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    init_run(slug="cache-engine", title="Cache Engine")
    with pytest.raises(FileExistsError):
        init_run(slug="cache-engine", title="Cache Engine")

    state, _ = init_run(slug="cache-engine", title="Cache Engine v2", force=True)
    assert state.title == "Cache Engine v2"


def test_init_run_rejects_invalid_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    with pytest.raises(ValueError):
        init_run(slug="Bad_Slug!")


def test_update_stage_progression_and_next_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    state, _ = init_run(slug="vector-search", title="Vector Search")
    assert "cm validate vector-search" in compute_next_action(state)

    failed = update_stage(
        "vector-search",
        PipelineStage.BLUEPRINT,
        StageStatus.FAILED,
        notes="2 errors",
        error_count=2,
    )
    assert failed.current_stage == PipelineStage.BLUEPRINT
    assert failed.stages[PipelineStage.BLUEPRINT].error_count == 2
    assert "cm validate vector-search" in compute_next_action(failed)

    passed = update_stage(
        "vector-search",
        PipelineStage.BLUEPRINT,
        StageStatus.PASSED,
        notes="OK",
        error_count=0,
    )
    assert passed.current_stage == PipelineStage.AUDIO
    assert "cm tts vector-search" in compute_next_action(passed)

    reloaded = load_state("vector-search")
    assert reloaded.current_stage == PipelineStage.AUDIO
    assert len(list_runs()) == 1
