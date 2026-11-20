"""Tests for blueprint schema validation and retention linting."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from cm.models import (
    Blueprint,
    Catalog,
    FormatPreset,
    PublishedEntry,
    SystemStatus,
    TopicDomain,
)
from cm.state import build_starter_blueprint
from cm.validators import validate_blueprint_data, validate_blueprint_file

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_BP = REPO_ROOT / "examples" / "password_system.blueprint.json"


def _load_example_dict() -> dict:
    return json.loads(EXAMPLE_BP.read_text(encoding="utf-8"))


def test_example_blueprint_passes_with_zero_warnings() -> None:
    report = validate_blueprint_file(EXAMPLE_BP)
    assert report.passed is True
    assert report.errors == []
    assert report.warnings == []
    assert report.metrics is not None
    assert report.metrics.iteration_count == 3
    assert 45.0 <= report.metrics.estimated_duration_sec <= 85.0
    assert report.metrics.hook_duration_sec <= 4.0


def test_starter_template_fails_due_to_todo_markers() -> None:
    starter = build_starter_blueprint(
        slug="neural-net",
        title="Neural Net From Scratch",
        topic_domain=TopicDomain.AI,
        fmt=FormatPreset.SHORT_9X16,
    )
    report = validate_blueprint_data(starter, target="starter")
    codes = {issue.code for issue in report.errors}
    assert report.passed is False
    assert "TODO_PLACEHOLDER_REMAINING" in codes


def test_banned_hook_opener_fails() -> None:
    raw = _load_example_dict()
    raw["beats"][0]["narration"] = "Hey guys, today we are going to build passwords."
    bp = Blueprint.model_validate(raw)
    report = validate_blueprint_data(bp, target="test")
    assert report.passed is False
    assert any(e.code == "HOOK_BANNED_OPENER" for e in report.errors)


def test_too_few_iterations_fails() -> None:
    raw = _load_example_dict()
    # Keep hook, build, 1 fail->upgrade, and payoff (only 1 iteration)
    raw["beats"] = [
        raw["beats"][0],
        raw["beats"][1],
        raw["beats"][2],
        raw["beats"][3],
        raw["beats"][-1],
    ]
    bp = Blueprint.model_validate(raw)
    report = validate_blueprint_data(bp, target="test")
    assert any(e.code == "STRUCTURE_TOO_FEW_ITERATIONS" for e in report.errors)


def test_fail_missing_stamp_and_stress_test_fails() -> None:
    raw = _load_example_dict()
    raw["beats"][2]["failure_stamp"] = None
    raw["beats"][2]["stress_test_trigger"] = None
    raw["beats"][2]["system_status"] = SystemStatus.BUILDING.value
    bp = Blueprint.model_validate(raw)
    report = validate_blueprint_data(bp, target="test")
    codes = {e.code for e in report.errors}
    assert "FAIL_MISSING_STAMP" in codes
    assert "FAIL_MISSING_STRESS_TEST" in codes
    assert "FAIL_WRONG_STATUS" in codes


def test_upgrade_without_version_increment_fails() -> None:
    raw = _load_example_dict()
    raw["beats"][3]["version_label"] = raw["beats"][2]["version_label"]
    bp = Blueprint.model_validate(raw)
    report = validate_blueprint_data(bp, target="test")
    assert any(e.code == "UPGRADE_VERSION_NOT_INCREMENTED" for e in report.errors)


def test_static_visual_diff_fails() -> None:
    raw = _load_example_dict()
    raw["beats"][4]["visual_diff"] = "none"
    bp = Blueprint.model_validate(raw)
    report = validate_blueprint_data(bp, target="test")
    assert any(e.code == "VISUAL_DIFF_STATIC" for e in report.errors)


def test_headline_too_long_fails() -> None:
    raw = _load_example_dict()
    raw["beats"][1]["on_screen_headline"] = "This Headline Is Far Too Many Words Long For Mobile"
    bp = Blueprint.model_validate(raw)
    report = validate_blueprint_data(bp, target="test")
    assert any(e.code == "HEADLINE_TOO_LONG" for e in report.errors)


def test_duration_too_short_and_too_long_fail() -> None:
    raw_short = _load_example_dict()
    for beat in raw_short["beats"]:
        beat["estimated_duration_sec"] = 1.5
    report_short = validate_blueprint_data(Blueprint.model_validate(raw_short), target="short")
    assert any(e.code == "DURATION_OUT_OF_RANGE" for e in report_short.errors)

    raw_long = copy.deepcopy(_load_example_dict())
    for beat in raw_long["beats"]:
        beat["estimated_duration_sec"] = 12.0
    report_long = validate_blueprint_data(Blueprint.model_validate(raw_long), target="long")
    assert any(e.code == "DURATION_OUT_OF_RANGE" for e in report_long.errors)


def test_malformed_json_and_schema_error_handled_cleanly(tmp_path: Path) -> None:
    bad_json = tmp_path / "broken.json"
    bad_json.write_text("{not valid json", encoding="utf-8")
    r_json = validate_blueprint_file(bad_json)
    assert r_json.passed is False
    assert r_json.errors[0].code == "INVALID_JSON"

    bad_schema = tmp_path / "schema_err.json"
    bad_schema.write_text(json.dumps({"slug": "Not Valid Slug"}), encoding="utf-8")
    r_schema = validate_blueprint_file(bad_schema)
    assert r_schema.passed is False
    assert any(e.code == "SCHEMA_VALIDATION_ERROR" for e in r_schema.errors)


def test_warnings_for_dense_sentence_missing_sfx_and_duplicate_topic() -> None:
    raw = _load_example_dict()
    raw["beats"][1]["narration"] = (
        "One two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone "
        "twentytwo twentythree twentyfour twentyfive twentysix twentyseven twentyeight twentynine."
    )
    raw["beats"][2]["sfx"] = []
    bp = Blueprint.model_validate(raw)

    catalog = Catalog(
        published=[
            PublishedEntry(
                slug="password-system",
                title="Building a Password System From Scratch",
                topic_domain=TopicDomain.SECURITY,
                format=FormatPreset.SHORT_9X16,
                published_at=datetime.now(UTC),
                keywords=["password", "hashing", "argon2"],
            )
        ]
    )

    report = validate_blueprint_data(bp, target="warn-check", catalog=catalog)
    warn_codes = {w.code for w in report.warnings}
    assert "DENSE_SENTENCE" in warn_codes
    assert "MISSING_SFX" in warn_codes
    assert "DUPLICATE_TOPIC" in warn_codes
