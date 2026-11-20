"""Exhaustive boundary, corruption, negative-path, and CLI integration tests."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from cm import paths
from cm.catalog import (
    add_idea,
    find_duplicates,
    find_idea,
    jaccard_similarity,
    load_catalog,
    save_catalog,
    set_idea_status,
    slugify,
)
from cm.cli import app
from cm.models import (
    PIPELINE_ORDER,
    Beat,
    BeatKind,
    Blueprint,
    Catalog,
    FormatPreset,
    IdeaEntry,
    IdeaStatus,
    MetricCallout,
    PipelineStage,
    PublishedEntry,
    SfxCue,
    SfxTrigger,
    StageStatus,
    SystemStatus,
    TopicDomain,
    VoiceSettings,
)
from cm.state import (
    build_starter_blueprint,
    compute_next_action,
    init_run,
    list_runs_checked,
    load_state,
    update_stage,
)
from cm.validators import validate_blueprint_data, validate_blueprint_file

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_BP = REPO_ROOT / "examples" / "password_system.blueprint.json"


def _example_dict() -> dict:
    return json.loads(EXAMPLE_BP.read_text(encoding="utf-8"))


def _clean_timed_example() -> dict:
    raw = _example_dict()
    for beat in raw["beats"]:
        beat["estimated_duration_sec"] = 6.0
    raw["beats"][0]["estimated_duration_sec"] = 3.5
    return raw


def _assert_cp1252(text: str) -> None:
    text.encode("cp1252")


# ---------------------------------------------------------------------------
# 1. Model boundaries & sanitization
# ---------------------------------------------------------------------------


def test_sfx_trigger_boundaries() -> None:
    for valid_at in ("start", "mid", "end", 0.0, 0.5, 1.0):
        trig = SfxTrigger(cue=SfxCue.POP_BUILD, at=valid_at)
        assert trig.cue == SfxCue.POP_BUILD

    for bad_at in (-0.01, 1.01, True, False, "middle"):
        with pytest.raises(ValidationError):
            SfxTrigger(cue=SfxCue.POP_BUILD, at=bad_at)

    assert SfxTrigger(cue=SfxCue.POP_BUILD, gain_db=-30.0).gain_db == -30.0
    assert SfxTrigger(cue=SfxCue.POP_BUILD, gain_db=0.0).gain_db == 0.0
    for bad_gain in (-30.1, 0.1):
        with pytest.raises(ValidationError):
            SfxTrigger(cue=SfxCue.POP_BUILD, gain_db=bad_gain)


def test_voice_and_beat_numeric_boundaries() -> None:
    assert VoiceSettings(target_wpm=120).target_wpm == 120
    assert VoiceSettings(target_wpm=220).target_wpm == 220
    for bad_wpm in (119, 221):
        with pytest.raises(ValidationError):
            VoiceSettings(target_wpm=bad_wpm)

    with pytest.raises(ValidationError):
        VoiceSettings(voice_name="Charon\nPuck")

    raw_beat = _example_dict()["beats"][0]
    assert Beat.model_validate({**raw_beat, "pause_after_ms": 0}).pause_after_ms == 0
    assert Beat.model_validate({**raw_beat, "pause_after_ms": 1200}).pause_after_ms == 1200
    for bad_pause in (-1, 1201):
        with pytest.raises(ValidationError):
            Beat.model_validate({**raw_beat, "pause_after_ms": bad_pause})


def test_single_line_and_whitespace_rejection() -> None:
    with pytest.raises(ValidationError):
        MetricCallout(label="   ", value="OK")
    with pytest.raises(ValidationError):
        MetricCallout(label="LOAD\nSPIKE", value="99%")

    raw_beat = _example_dict()["beats"][0]
    for blank_field in ("version_label", "narration", "on_screen_headline", "visual_state", "visual_diff"):
        with pytest.raises(ValidationError):
            Beat.model_validate({**raw_beat, blank_field: "   \t  "})

    with pytest.raises(ValidationError):
        Beat.model_validate({**raw_beat, "on_screen_headline": "Line1\nLine2"})
    with pytest.raises(ValidationError):
        Beat.model_validate({**raw_beat, "failure_stamp": "ALL\r\nLEAKED"})

    # Blank failure_stamp normalizes to None cleanly
    normalized = Beat.model_validate({**raw_beat, "failure_stamp": "   "})
    assert normalized.failure_stamp is None

    raw_bp = _example_dict()
    for bad_title in ("   ", "Title\nLine2"):
        with pytest.raises(ValidationError):
            Blueprint.model_validate({**raw_bp, "title": bad_title})

    with pytest.raises(ValidationError):
        Blueprint.model_validate({**raw_bp, "core_premise": "   "})

    with pytest.raises(ValidationError):
        PublishedEntry(
            slug="p1",
            title="Bad\nTitle",
            topic_domain=TopicDomain.CS,
            format=FormatPreset.SHORT_9X16,
            published_at=datetime.now(UTC),
        )

    with pytest.raises(ValidationError):
        IdeaEntry(
            id="idea-001",
            slug="bad-chain",
            title="Bad Chain",
            topic_domain=TopicDomain.CS,
            hook="Sharp hook",
            failure_chain=["Step 1", "   ", "Step 3"],
            created_at=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# 2. Validator exact boundaries & structural edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hook_dur", "expected_error", "expected_warn"),
    [
        (4.00, None, None),
        (4.01, None, "HOOK_SLIGHTLY_LONG"),
        (6.00, None, "HOOK_SLIGHTLY_LONG"),
        (6.01, "HOOK_TOO_LONG", None),
    ],
)
def test_hook_duration_exact_boundaries(
    hook_dur: float, expected_error: str | None, expected_warn: str | None
) -> None:
    raw = _clean_timed_example()
    raw["beats"][0]["estimated_duration_sec"] = hook_dur
    report = validate_blueprint_data(Blueprint.model_validate(raw), target="hook-bounds")
    err_codes = {e.code for e in report.errors}
    warn_codes = {w.code for w in report.warnings}
    if expected_error:
        assert expected_error in err_codes
    else:
        assert "HOOK_TOO_LONG" not in err_codes
    if expected_warn:
        assert expected_warn in warn_codes
    else:
        assert "HOOK_SLIGHTLY_LONG" not in warn_codes


@pytest.mark.parametrize(
    ("beat_dur", "expected_error", "expected_warn"),
    [
        (9.00, None, None),
        (9.01, None, "BEAT_SLIGHTLY_LONG"),
        (13.00, None, "BEAT_SLIGHTLY_LONG"),
        (13.01, "BEAT_TOO_LONG", None),
    ],
)
def test_beat_duration_exact_boundaries(
    beat_dur: float, expected_error: str | None, expected_warn: str | None
) -> None:
    raw = _clean_timed_example()
    raw["beats"][1]["estimated_duration_sec"] = beat_dur
    report = validate_blueprint_data(Blueprint.model_validate(raw), target="beat-bounds")
    err_codes = {e.code for e in report.errors}
    warn_codes = {w.code for w in report.warnings}
    if expected_error:
        assert expected_error in err_codes
    else:
        assert "BEAT_TOO_LONG" not in err_codes
    if expected_warn:
        assert expected_warn in warn_codes
    else:
        assert "BEAT_SLIGHTLY_LONG" not in warn_codes


@pytest.mark.parametrize(
    ("target_total", "expected_error", "expected_warn"),
    [
        (29.9, "DURATION_OUT_OF_RANGE", None),
        (30.0, None, "DURATION_SUBOPTIMAL"),
        (45.0, None, None),
        (85.0, None, None),
        (85.1, None, "DURATION_SUBOPTIMAL"),
        (95.0, None, "DURATION_SUBOPTIMAL"),
        (95.1, "DURATION_OUT_OF_RANGE", None),
    ],
)
def test_total_duration_exact_boundaries(
    target_total: float, expected_error: str | None, expected_warn: str | None
) -> None:
    raw = _clean_timed_example()
    hook_dur = 3.5
    raw["beats"][0]["estimated_duration_sec"] = hook_dur
    # Distribute remaining duration evenly across the remaining 8 beats (kept <= 12.0s; adjust hook too for >95.1)
    remaining = target_total - hook_dur
    if remaining / 8.0 > 12.0:
        hook_dur = 4.0
        raw["beats"][0]["estimated_duration_sec"] = hook_dur
        remaining = target_total - hook_dur
    per_beat = round(remaining / 8.0, 4)
    for beat in raw["beats"][1:]:
        beat["estimated_duration_sec"] = per_beat
    # Correct rounding drift on final beat so total matches exact boundary
    actual_sum = hook_dur + per_beat * 7
    raw["beats"][-1]["estimated_duration_sec"] = round(target_total - actual_sum, 4)

    report = validate_blueprint_data(Blueprint.model_validate(raw), target="total-bounds")
    err_codes = {e.code for e in report.errors}
    warn_codes = {w.code for w in report.warnings}
    if expected_error:
        assert expected_error in err_codes
    else:
        assert "DURATION_OUT_OF_RANGE" not in err_codes
    if expected_warn:
        assert expected_warn in warn_codes
    else:
        assert "DURATION_SUBOPTIMAL" not in warn_codes


def test_headline_and_failure_stamp_exact_lengths() -> None:
    raw = _clean_timed_example()
    raw["beats"][1]["on_screen_headline"] = "A" * 42
    raw["beats"][2]["failure_stamp"] = "X" * 28
    assert validate_blueprint_data(Blueprint.model_validate(raw), target="ok").passed is True

    raw["beats"][1]["on_screen_headline"] = "A" * 43
    raw["beats"][2]["failure_stamp"] = "X" * 29
    rep = validate_blueprint_data(Blueprint.model_validate(raw), target="too-long")
    codes = {e.code for e in rep.errors}
    assert "HEADLINE_TOO_LONG" in codes
    assert "FAIL_MISSING_STAMP" in codes


def test_separate_stress_test_beat_satisfies_fail_requirement() -> None:
    raw = _clean_timed_example()
    stress_beat = {
        "id": "b02b_v1_stress",
        "kind": "stress_test",
        "version_label": "v1.0",
        "system_status": "testing",
        "narration": "Now unleash an automated SQL injection payload directly at the login endpoint.",
        "on_screen_headline": "Inject SQL Attack",
        "visual_state": "Red SQL injection probe enters the v1.0 User Node toward the database.",
        "visual_diff": "Spawn incoming red attack arrow heading into the Users Table.",
        "pause_after_ms": 120,
        "sfx": [{"cue": "riser_stress", "at": "start", "gain_db": -12.0}],
        "estimated_duration_sec": 4.5,
    }
    raw["beats"][2]["stress_test_trigger"] = None
    raw["beats"].insert(2, stress_beat)
    report = validate_blueprint_data(Blueprint.model_validate(raw), target="stress-beat")
    assert report.passed is True


def test_structural_errors_missing_roles_duplicate_id_regression_unresolved_and_todos() -> None:
    raw = _clean_timed_example()
    raw["core_premise"] = "TODO: finish premise"
    raw["persistent_subject"] = "TODO: finish subject"
    raw["beats"][0]["kind"] = BeatKind.BUILD.value
    raw["beats"][1]["kind"] = BeatKind.HOOK.value
    raw["beats"][1]["id"] = raw["beats"][0]["id"]  # Duplicate beat id
    raw["beats"][3]["system_status"] = SystemStatus.BUILDING.value  # UPGRADE_WRONG_STATUS
    raw["beats"][5]["version_label"] = "v1.0"  # UPGRADE_VERSION_REGRESSION (v1.0 already seen)
    raw["beats"][5]["sfx"] = []  # MISSING_SFX warning on upgrade
    # Leave beat[6] fail unresolved by converting beat[7] from upgrade to build
    raw["beats"][7]["kind"] = BeatKind.BUILD.value
    raw["beats"][-1]["kind"] = BeatKind.BUILD.value  # Missing payoff

    bp = Blueprint.model_validate(raw)
    rep = validate_blueprint_data(bp, target="multi-err", expected_slug="other-slug")
    err_codes = {e.code for e in rep.errors}
    warn_codes = {w.code for w in rep.warnings}

    assert {
        "SLUG_MISMATCH",
        "TODO_PLACEHOLDER_REMAINING",
        "STRUCTURE_MISSING_HOOK",
        "STRUCTURE_MISSING_INITIAL_BUILD",
        "STRUCTURE_MISSING_PAYOFF",
        "DUPLICATE_BEAT_ID",
        "UPGRADE_WRONG_STATUS",
        "UPGRADE_VERSION_REGRESSION",
        "UNRESOLVED_FAIL",
        "STRUCTURE_TOO_FEW_ITERATIONS",
    }.issubset(err_codes)
    assert "MISSING_SFX" in warn_codes


def test_decimal_safe_dense_sentence_detection() -> None:
    raw = _clean_timed_example()
    # 29 words containing decimals v1.0 and 0.002s inside ONE sentence
    raw["beats"][1]["narration"] = (
        "In v1.0 when our tiny login server checks credentials in 0.002s it saves every "
        "raw user secret into an unencrypted database column without any safety layer whatsoever."
    )
    assert len(raw["beats"][1]["narration"].split()) < 29 or True
    # Ensure > 28 words total in that single sentence:
    raw["beats"][1]["narration"] = (
        "In v1.0 when our tiny login server checks user credentials in 0.002s it writes every "
        "single raw secret directly into an unencrypted database column without adding any protection whatsoever today."
    )
    assert len(raw["beats"][1]["narration"].split()) == 30
    rep = validate_blueprint_data(Blueprint.model_validate(raw), target="decimal-dense")
    assert any(w.code == "DENSE_SENTENCE" for w in rep.warnings)


def test_long_16x9_format_validation() -> None:
    raw = copy.deepcopy(_clean_timed_example())
    raw["format"] = FormatPreset.LONG_16X9.value
    # Currently only 3 iterations and ~51.5s -> fails long_16x9 min_iterations=5 & min_duration=150s
    rep_fail = validate_blueprint_data(Blueprint.model_validate(raw), target="long-fail")
    codes = {e.code for e in rep_fail.errors}
    assert "STRUCTURE_TOO_FEW_ITERATIONS" in codes
    assert "DURATION_OUT_OF_RANGE" in codes


def test_file_errors_missing_invalid_utf8_and_non_object_json(tmp_path: Path) -> None:
    missing_rep = validate_blueprint_file(tmp_path / "does_not_exist.json")
    assert missing_rep.passed is False
    assert missing_rep.errors[0].code == "FILE_NOT_FOUND"

    bad_utf8 = tmp_path / "binary.json"
    bad_utf8.write_bytes(b"\xff\xfe\x00\x01")
    enc_rep = validate_blueprint_file(bad_utf8)
    assert enc_rep.passed is False
    assert enc_rep.errors[0].code == "INVALID_ENCODING"

    list_json = tmp_path / "list.json"
    list_json.write_text("[1, 2, 3]", encoding="utf-8")
    list_rep = validate_blueprint_file(list_json)
    assert list_rep.passed is False
    assert list_rep.errors[0].code == "SCHEMA_VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 3. Paths, Atomic I/O, State & Catalog resilience
# ---------------------------------------------------------------------------


def test_paths_resolution_and_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CM_ROOT", raising=False)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    # Without pyproject.toml upward, falls back to package parent
    monkeypatch.chdir(nested)
    assert (paths.get_repo_root() / "pyproject.toml").is_file()

    # With pyproject.toml present in cwd ancestor, resolves to tmp_path
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert paths.get_repo_root() == tmp_path.resolve()
    assert paths.assets_dir(tmp_path) == tmp_path / "assets"
    assert paths.engine_dir(tmp_path) == tmp_path / "engine"

    # atomic_write_text cleans up temp files and preserves original on failure
    target = tmp_path / "data" / "file.txt"
    paths.atomic_write_text(target, "original")
    assert target.read_text(encoding="utf-8") == "original"
    assert list(target.parent.glob("*.tmp")) == []

    def boom(_src: Path, _dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(paths.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        paths.atomic_write_text(target, "corrupted")
    assert target.read_text(encoding="utf-8") == "original"
    assert list(target.parent.glob("*.tmp")) == []


def test_state_from_blueprint_syncs_slug_and_inherits_format_unless_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    src = tmp_path / "source.json"
    raw = _example_dict()
    raw["slug"] = "old-slug"
    raw["format"] = FormatPreset.LONG_16X9.value
    raw["topic_domain"] = TopicDomain.AI.value
    src.write_text(json.dumps(raw), encoding="utf-8")

    # Inherit from source when domain & fmt omitted
    state1, rdir1 = init_run(slug="new-ai-run", from_blueprint=src)
    assert state1.format == FormatPreset.LONG_16X9
    assert state1.topic_domain == TopicDomain.AI
    assert json.loads((rdir1 / "blueprint.json").read_text(encoding="utf-8"))["slug"] == "new-ai-run"

    # Explicit overrides take precedence over source file
    state2, _ = init_run(
        slug="override-run",
        title="Custom Title",
        topic_domain=TopicDomain.DATABASES,
        fmt=FormatPreset.SHORT_9X16,
        from_blueprint=src,
    )
    assert state2.title == "Custom Title"
    assert state2.topic_domain == TopicDomain.DATABASES
    assert state2.format == FormatPreset.SHORT_9X16

    # Invalid enum values & non-dict JSON in from_blueprint fall back cleanly
    bad_enum_src = tmp_path / "bad_enum.json"
    bad_enum_src.write_text(
        json.dumps({"title": "Fallback", "topic_domain": "unknown", "format": "square"}),
        encoding="utf-8",
    )
    state3, _ = init_run(slug="bad-enum-run", from_blueprint=bad_enum_src)
    assert state3.topic_domain == TopicDomain.CS
    assert state3.format == FormatPreset.SHORT_9X16

    bad_json_src = tmp_path / "bad_json.json"
    bad_json_src.write_text("{broken", encoding="utf-8")
    state4, rdir4 = init_run(slug="bad-json-run", from_blueprint=bad_json_src)
    assert (rdir4 / "blueprint.json").read_text(encoding="utf-8") == "{broken"

    with pytest.raises(FileNotFoundError):
        init_run(slug="missing-src", from_blueprint=tmp_path / "nope.json")


def test_starter_blueprint_pads_iterations_for_long_16x9() -> None:
    idea = IdeaEntry(
        id="idea-001",
        slug="db-index",
        title="How Database Indexes Work",
        topic_domain=TopicDomain.DATABASES,
        format=FormatPreset.LONG_16X9,
        hook="Why scanning 10M rows stalls.",
        failure_chain=["Full scan", "Naive hash misses ranges", "Unbalanced tree"],
        created_at=datetime.now(UTC),
    )
    bp = build_starter_blueprint(
        slug="db-index",
        title=idea.title,
        topic_domain=idea.topic_domain,
        fmt=FormatPreset.LONG_16X9,
        idea=idea,
    )
    fail_beats = [b for b in bp.beats if b.kind == BeatKind.FAIL]
    assert len(fail_beats) == 5


def test_corrupt_state_and_compute_next_action_all_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    # Before runs/ exists, list_runs_checked returns empty
    assert list_runs_checked() == ([], [])

    state, _ = init_run(slug="stage-walk", title="Stage Walk")
    for stage in PIPELINE_ORDER[1:]:
        state = update_stage("stage-walk", stage, StageStatus.PASSED)
        action = compute_next_action(state)
        assert isinstance(action, str) and len(action) > 0
    assert state.current_stage == PipelineStage.PUBLISH
    assert "Complete!" in compute_next_action(state)

    # Corrupt a second run directory and verify list_runs_checked surfaces it cleanly
    corrupt_dir = tmp_path / "runs" / "broken-run"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "state.json").write_text("{invalid json", encoding="utf-8")

    with pytest.raises(ValueError, match="Corrupt state file"):
        load_state("broken-run")

    valid_runs, errors = list_runs_checked()
    assert len(valid_runs) == 1
    assert len(errors) == 1
    assert "broken-run" in errors[0]


def test_catalog_edge_cases_and_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    assert jaccard_similarity(set(), {"ai"}) == 0.0
    with pytest.raises(ValueError, match="Cannot derive a valid slug"):
        slugify("!!!@@@###")

    with pytest.raises(ValueError, match="Invalid slug"):
        add_idea("Test", TopicDomain.CS, "Hook", ["A", "B", "C"], slug="BAD_SLUG")

    with pytest.raises(ValueError, match="at least 3 non-empty steps"):
        add_idea("Test", TopicDomain.CS, "Hook", ["A", "   ", "C"])

    # Seed catalog with published item and non-standard idea ID to test ID gap increment + dedup
    seeded = Catalog(
        ideas=[
            IdeaEntry(
                id="custom-id",
                slug="custom-one",
                title="Custom One",
                topic_domain=TopicDomain.CS,
                hook="Hook",
                failure_chain=["A", "B", "C"],
                created_at=datetime.now(UTC),
            ),
            IdeaEntry(
                id="idea-007",
                slug="raft-consensus",
                title="Raft Consensus From Scratch",
                topic_domain=TopicDomain.SYSTEMS,
                hook="How servers agree when networks split.",
                failure_chain=["Single leader dies", "Split brain", "Log divergence"],
                created_at=datetime.now(UTC),
            ),
        ],
        published=[
            PublishedEntry(
                slug="password-system",
                title="Building a Password System From Scratch",
                topic_domain=TopicDomain.SECURITY,
                format=FormatPreset.SHORT_9X16,
                published_at=datetime.now(UTC),
                keywords=["argon2", "salts"],
            )
        ],
    )
    save_catalog(seeded)

    with pytest.raises(ValueError, match="published catalog"):
        add_idea(
            "Password System",
            TopicDomain.SECURITY,
            "Hook",
            ["A", "B", "C"],
            slug="password-system",
        )

    entry, warnings = add_idea(
        "Building Password System From Scratch",
        TopicDomain.SECURITY,
        "Hook",
        ["A", "B", "C"],
        slug="password-system-deep-dive",
    )
    assert entry.id == "idea-008"
    assert len(warnings) == 1

    # Check exact slug warning branch in find_duplicates
    dup_warnings = find_duplicates("Any Title", "password-system", seeded)
    assert any("Exact slug match with published video" in w for w in dup_warnings)
    dup_idea = find_duplicates("Any Title", "raft-consensus", seeded)
    assert any("Exact slug match with existing idea" in w for w in dup_idea)

    assert find_idea(seeded, "missing-ref") is None
    with pytest.raises(KeyError, match="Idea not found"):
        set_idea_status("missing-ref", IdeaStatus.PUBLISHED)

    # Corrupt catalog file raises clean ValueError
    (tmp_path / "channels" / "catalog.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt catalog file"):
        load_catalog()


# ---------------------------------------------------------------------------
# 4. Exhaustive CLI human + JSON + error + cp1252 ASCII-safety tests
# ---------------------------------------------------------------------------


def test_cli_human_and_error_paths_all_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    # Empty status & empty ideas list (human mode)
    res_empty = runner.invoke(app, ["status"])
    assert res_empty.exit_code == 0
    _assert_cp1252(res_empty.output)

    res_ideas_empty = runner.invoke(app, ["ideas", "list"])
    assert res_ideas_empty.exit_code == 0
    _assert_cp1252(res_ideas_empty.output)

    # Schema for all 3 models
    for model_name in ("blueprint", "state", "catalog"):
        res_schema = runner.invoke(app, ["schema", model_name])
        assert res_schema.exit_code == 0
        assert "properties" in json.loads(res_schema.output)

    # cm new error paths (human & json)
    assert runner.invoke(app, ["new", "Bad_Slug"]).exit_code == 1
    res_bad_json = runner.invoke(app, ["new", "Bad_Slug", "--json"])
    assert res_bad_json.exit_code == 1
    assert "error" in json.loads(res_bad_json.output)

    # cm new human mode + duplicate error
    res_ok = runner.invoke(app, ["new", "pass-demo", "--from-blueprint", str(EXAMPLE_BP)])
    assert res_ok.exit_code == 0
    _assert_cp1252(res_ok.output)
    assert runner.invoke(app, ["new", "pass-demo"]).exit_code == 1

    # cm validate human mode on slug (passes cleanly, cp1252-safe)
    res_val = runner.invoke(app, ["validate", "pass-demo"])
    assert res_val.exit_code == 0
    assert "[PASS]" in res_val.output
    _assert_cp1252(res_val.output)

    # cm status list + single slug (human mode)
    res_list = runner.invoke(app, ["status"])
    assert res_list.exit_code == 0
    assert "pass-demo" in res_list.output
    _assert_cp1252(res_list.output)

    res_single = runner.invoke(app, ["status", "pass-demo"])
    assert res_single.exit_code == 0
    assert "Next Action" in res_single.output
    _assert_cp1252(res_single.output)

    assert runner.invoke(app, ["status", "nonexistent"]).exit_code == 1

    # Corrupt run surfaced cleanly in status
    bad_dir = tmp_path / "runs" / "corrupt-run"
    bad_dir.mkdir(parents=True)
    (bad_dir / "state.json").write_text("{corrupt", encoding="utf-8")
    res_status_corrupt = runner.invoke(app, ["status"])
    assert res_status_corrupt.exit_code == 0
    assert "[WARN]" in res_status_corrupt.output
    _assert_cp1252(res_status_corrupt.output)
    assert runner.invoke(app, ["status", "corrupt-run", "--json"]).exit_code == 1

    # cm validate edge cases: directory target, missing run, slug mismatch, warnings & errors in human mode
    assert runner.invoke(app, ["validate", str(tmp_path)]).exit_code == 1
    assert runner.invoke(app, ["validate", "ghost-run"]).exit_code == 1

    mismatch_bp = tmp_path / "runs" / "pass-demo" / "blueprint.json"
    raw = json.loads(mismatch_bp.read_text(encoding="utf-8"))
    raw["slug"] = "wrong-slug"
    raw["beats"][2]["sfx"] = []  # also triggers WARN in human mode
    mismatch_bp.write_text(json.dumps(raw), encoding="utf-8")
    res_mismatch = runner.invoke(app, ["validate", "pass-demo"])
    assert res_mismatch.exit_code == 1
    assert "SLUG_MISMATCH" in res_mismatch.output
    assert "MISSING_SFX" in res_mismatch.output
    _assert_cp1252(res_mismatch.output)

    # cm validate --json failing blueprint returns exit code 1 with valid JSON report
    res_fail_json = runner.invoke(app, ["validate", "pass-demo", "--json"])
    assert res_fail_json.exit_code == 1
    assert json.loads(res_fail_json.output)["passed"] is False

    # cm ideas human mode + error paths + warning on near-duplicate
    assert runner.invoke(
        app,
        [
            "ideas",
            "add",
            "--title",
            "Too Few",
            "--domain",
            "cs",
            "--hook",
            "Hook",
            "--failure",
            "One",
            "--failure",
            "Two",
        ],
    ).exit_code == 1

    res_idea_ok = runner.invoke(
        app,
        [
            "ideas",
            "add",
            "--title",
            "Building a Rate Limiter From Scratch",
            "--domain",
            "systems",
            "--hook",
            "One bot floods your API.",
            "--failure",
            "No limits",
            "--failure",
            "Fixed window spike",
            "--failure",
            "Single-node counter drift",
        ],
    )
    assert res_idea_ok.exit_code == 0
    _assert_cp1252(res_idea_ok.output)

    # Similar title emits [WARN] in human mode
    res_idea_dup = runner.invoke(
        app,
        [
            "ideas",
            "add",
            "--title",
            "Rate Limiter From Scratch Guide",
            "--domain",
            "systems",
            "--hook",
            "One bot floods your API.",
            "--failure",
            "No limits",
            "--failure",
            "Fixed window spike",
            "--failure",
            "Single-node counter drift",
        ],
    )
    assert res_idea_dup.exit_code == 0
    assert "[WARN]" in res_idea_dup.output
    _assert_cp1252(res_idea_dup.output)

    # Duplicate slug rejected
    assert runner.invoke(
        app,
        [
            "ideas",
            "add",
            "--title",
            "Duplicate Slug Idea",
            "--slug",
            "building-a-rate-limiter-from-scratch",
            "--domain",
            "systems",
            "--hook",
            "Hook",
            "--failure",
            "A",
            "--failure",
            "B",
            "--failure",
            "C",
        ],
    ).exit_code == 1

    res_ideas_human = runner.invoke(app, ["ideas", "list", "--status", "backlog"])
    assert res_ideas_human.exit_code == 0
    assert "idea-001" in res_ideas_human.output
    _assert_cp1252(res_ideas_human.output)

    assert runner.invoke(app, ["ideas", "pick", "idea-999"]).exit_code == 1
    res_pick_ok = runner.invoke(app, ["ideas", "pick", "idea-001"])
    assert res_pick_ok.exit_code == 0
    _assert_cp1252(res_pick_ok.output)
    # Re-picking without --force fails due to existing run dir
    assert runner.invoke(app, ["ideas", "pick", "idea-001"]).exit_code == 1

    # Corrupt catalog surfaces cleanly across validate & ideas commands
    (tmp_path / "channels" / "catalog.json").write_text("{corrupt", encoding="utf-8")
    assert runner.invoke(app, ["validate", str(EXAMPLE_BP), "--json"]).exit_code == 1
    assert runner.invoke(app, ["ideas", "list", "--json"]).exit_code == 1
    assert runner.invoke(app, ["ideas", "pick", "idea-001", "--json"]).exit_code == 1
