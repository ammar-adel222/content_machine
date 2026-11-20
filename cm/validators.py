"""Pydantic schema validation and retention/pacing linting for blueprints."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from cm.catalog import jaccard_similarity, tokenize_topic
from cm.models import (
    FORMAT_SPECS,
    Beat,
    BeatKind,
    Blueprint,
    BlueprintMetrics,
    Catalog,
    LintIssue,
    SystemStatus,
    ValidationReport,
)

BANNED_OPENERS = re.compile(
    r"^\s*(?:hey guys|what'?s up|welcome back|in this video|"
    r"today we(?:'re| are) going to|let'?s talk about|"
    r"have you ever wondered what .+ is)\b",
    re.IGNORECASE,
)

TODO_PATTERN = re.compile(r"\bTODO\b")
STATIC_DIFF_VALUES = frozenset({"", "none", "n/a", "same", "unchanged", "no change"})


def estimate_beat_duration(beat: Beat, target_wpm: int) -> float:
    """Estimate beat duration in seconds from narration word count and pause."""
    if beat.estimated_duration_sec is not None:
        return round(float(beat.estimated_duration_sec), 2)
    words = len(beat.narration.split())
    speech_sec = max(1.0, words / (target_wpm / 60.0))
    return round(speech_sec + (beat.pause_after_ms / 1000.0), 2)


def validate_blueprint_data(
    blueprint: Blueprint,
    target: str,
    catalog: Catalog | None = None,
) -> ValidationReport:
    """Run structural, retention, pacing, and visual-diff checks on a Blueprint."""
    spec = FORMAT_SPECS[blueprint.format]
    errors: list[LintIssue] = []
    warnings: list[LintIssue] = []

    def add_error(code: str, message: str, suggestion: str, beat_id: str | None = None) -> None:
        errors.append(
            LintIssue(
                code=code,
                severity="error",
                beat_id=beat_id,
                message=message,
                suggestion=suggestion,
            )
        )

    def add_warning(code: str, message: str, suggestion: str, beat_id: str | None = None) -> None:
        warnings.append(
            LintIssue(
                code=code,
                severity="warning",
                beat_id=beat_id,
                message=message,
                suggestion=suggestion,
            )
        )

    if TODO_PATTERN.search(blueprint.core_premise) or TODO_PATTERN.search(
        blueprint.persistent_subject
    ):
        add_error(
            "TODO_PLACEHOLDER_REMAINING",
            "Top-level blueprint fields still contain TODO placeholders.",
            "Replace TODO text in core_premise and persistent_subject with real details.",
        )

    beats = blueprint.beats
    seen_ids: set[str] = set()
    beat_durations: list[float] = []

    if beats[0].kind != BeatKind.HOOK:
        add_error(
            "STRUCTURE_MISSING_HOOK",
            f"First beat '{beats[0].id}' has kind '{beats[0].kind.value}', expected 'hook'.",
            "Start the blueprint with a 'hook' beat.",
            beat_id=beats[0].id,
        )

    if beats[1].kind != BeatKind.BUILD:
        add_error(
            "STRUCTURE_MISSING_INITIAL_BUILD",
            f"Second beat '{beats[1].id}' has kind '{beats[1].kind.value}', expected 'build'.",
            "Follow the hook immediately with an initial naive 'build' beat (e.g. v1.0).",
            beat_id=beats[1].id,
        )

    if beats[-1].kind != BeatKind.PAYOFF:
        add_error(
            "STRUCTURE_MISSING_PAYOFF",
            f"Final beat '{beats[-1].id}' has kind '{beats[-1].kind.value}', expected 'payoff'.",
            "End the blueprint with a 'payoff' beat summarizing the final resilient design.",
            beat_id=beats[-1].id,
        )

    if BANNED_OPENERS.search(beats[0].narration):
        add_error(
            "HOOK_BANNED_OPENER",
            "Hook narration uses a banned generic opener.",
            "Open immediately with stakes, a visual paradox, or a direct question.",
            beat_id=beats[0].id,
        )

    iteration_count = 0
    pending_fail = False
    version_progression: list[str] = []

    for idx, beat in enumerate(beats):
        if beat.id in seen_ids:
            add_error(
                "DUPLICATE_BEAT_ID",
                f"Duplicate beat id '{beat.id}'.",
                "Give every beat a unique snake_case id.",
                beat_id=beat.id,
            )
        seen_ids.add(beat.id)

        if not version_progression or version_progression[-1] != beat.version_label:
            version_progression.append(beat.version_label)

        if any(
            TODO_PATTERN.search(txt)
            for txt in (
                beat.narration,
                beat.on_screen_headline,
                beat.visual_state,
                beat.visual_diff,
            )
        ):
            add_error(
                "TODO_PLACEHOLDER_REMAINING",
                f"Beat '{beat.id}' still contains TODO placeholder text.",
                "Replace all TODO markers with production copy and visual specs.",
                beat_id=beat.id,
            )

        dur = estimate_beat_duration(beat, blueprint.voice.target_wpm)
        beat_durations.append(dur)

        if idx == 0:
            if dur > 6.0:
                add_error(
                    "HOOK_TOO_LONG",
                    f"Hook duration is ~{dur:.2f}s (max 6.0s).",
                    "Trim hook narration under 15 words so the curiosity loop lands in <=4s.",
                    beat_id=beat.id,
                )
            elif dur > 4.0:
                add_warning(
                    "HOOK_SLIGHTLY_LONG",
                    f"Hook duration is ~{dur:.2f}s (recommended <= 4.0s).",
                    "Tighten the opening line to <= 10 words for stronger Short retention.",
                    beat_id=beat.id,
                )

        headline = beat.on_screen_headline.strip()
        if len(headline) > 42 or len(headline.split()) > 6:
            add_error(
                "HEADLINE_TOO_LONG",
                f"Headline '{headline}' is {len(headline)} chars / {len(headline.split())} words.",
                "Keep on_screen_headline <= 6 words and <= 42 chars for mobile legibility.",
                beat_id=beat.id,
            )

        if dur > spec.max_beat_error_sec:
            add_error(
                "BEAT_TOO_LONG",
                f"Beat '{beat.id}' is ~{dur:.2f}s (max {spec.max_beat_error_sec:.1f}s).",
                "Split into two beats or tighten narration so visual state shifts regularly.",
                beat_id=beat.id,
            )
        elif dur > spec.max_beat_warn_sec:
            add_warning(
                "BEAT_SLIGHTLY_LONG",
                f"Beat '{beat.id}' is ~{dur:.2f}s (recommended <= {spec.max_beat_warn_sec:.1f}s).",
                "Trim a clause or split the beat to keep pacing brisk.",
                beat_id=beat.id,
            )

        for sentence in re.split(r"[.!?]+", beat.narration):
            words_in_sentence = len(sentence.split())
            if words_in_sentence > 28:
                add_warning(
                    "DENSE_SENTENCE",
                    f"Beat '{beat.id}' contains a {words_in_sentence}-word sentence.",
                    "Break long sentences into punchy spoken chunks (<= 20 words).",
                    beat_id=beat.id,
                )
                break

        if idx > 0:
            prev = beats[idx - 1]
            diff_norm = beat.visual_diff.strip().lower()
            if (
                diff_norm in STATIC_DIFF_VALUES
                or diff_norm == prev.visual_diff.strip().lower()
                or beat.visual_state.strip().lower() == prev.visual_state.strip().lower()
            ):
                add_error(
                    "VISUAL_DIFF_STATIC",
                    f"Beat '{beat.id}' does not introduce a distinct visual change from '{prev.id}'.",
                    "Specify a concrete diagram mutation in visual_state and visual_diff.",
                    beat_id=beat.id,
                )

        if beat.kind == BeatKind.FAIL:
            pending_fail = True
            stamp = (beat.failure_stamp or "").strip()
            if not stamp or len(stamp) > 28:
                add_error(
                    "FAIL_MISSING_STAMP",
                    f"Fail beat '{beat.id}' must define failure_stamp (1-28 chars).",
                    "Add a crisp uppercase failure_stamp (e.g. 'ALL LEAKED').",
                    beat_id=beat.id,
                )

            prev_is_stress = idx > 0 and beats[idx - 1].kind == BeatKind.STRESS_TEST
            has_trigger = bool((beat.stress_test_trigger or "").strip())
            if not prev_is_stress and not has_trigger:
                add_error(
                    "FAIL_MISSING_STRESS_TEST",
                    f"Fail beat '{beat.id}' has no preceding stress_test beat and no stress_test_trigger.",
                    "Add a 'stress_test' beat before it or set stress_test_trigger on this beat.",
                    beat_id=beat.id,
                )

            if beat.system_status not in (SystemStatus.COMPROMISED, SystemStatus.DEGRADED):
                add_error(
                    "FAIL_WRONG_STATUS",
                    f"Fail beat '{beat.id}' has status '{beat.system_status.value}'.",
                    "Use 'compromised' or 'degraded' on fail beats.",
                    beat_id=beat.id,
                )

            if not beat.sfx:
                add_warning(
                    "MISSING_SFX",
                    f"Fail beat '{beat.id}' has no SFX cues.",
                    "Add impact_fail, alarm_breach, glitch_error, or stamp_hit.",
                    beat_id=beat.id,
                )

        elif beat.kind == BeatKind.UPGRADE:
            if pending_fail:
                iteration_count += 1
                pending_fail = False

            if beat.system_status not in (SystemStatus.UPGRADED, SystemStatus.RESILIENT):
                add_error(
                    "UPGRADE_WRONG_STATUS",
                    f"Upgrade beat '{beat.id}' has status '{beat.system_status.value}'.",
                    "Use 'upgraded' or 'resilient' on upgrade beats.",
                    beat_id=beat.id,
                )

            if idx > 0 and beat.version_label.strip().lower() == beats[idx - 1].version_label.strip().lower():
                add_error(
                    "UPGRADE_VERSION_NOT_INCREMENTED",
                    f"Upgrade beat '{beat.id}' keeps version_label '{beat.version_label}'.",
                    "Increment version_label (e.g. v1.0 -> v2.0) when upgrading.",
                    beat_id=beat.id,
                )

            if not beat.sfx:
                add_warning(
                    "MISSING_SFX",
                    f"Upgrade beat '{beat.id}' has no SFX cues.",
                    "Add lock_upgrade or chime_fix to accent the fix.",
                    beat_id=beat.id,
                )

    if iteration_count < spec.min_iterations:
        add_error(
            "STRUCTURE_TOO_FEW_ITERATIONS",
            f"Found {iteration_count} fail->upgrade iterations; format '{blueprint.format.value}' requires >= {spec.min_iterations}.",
            f"Include at least {spec.min_iterations} distinct fail -> upgrade cycles.",
        )

    total_duration = round(sum(beat_durations), 2)
    if total_duration < spec.hard_min_duration_sec or total_duration > spec.hard_max_duration_sec:
        add_error(
            "DURATION_OUT_OF_RANGE",
            f"Estimated duration {total_duration:.2f}s is outside hard bounds [{spec.hard_min_duration_sec:.0f}s, {spec.hard_max_duration_sec:.0f}s].",
            f"Adjust narration length so total runtime stays between {spec.recommended_min_duration_sec:.0f}s and {spec.recommended_max_duration_sec:.0f}s.",
        )
    elif (
        total_duration < spec.recommended_min_duration_sec
        or total_duration > spec.recommended_max_duration_sec
    ):
        add_warning(
            "DURATION_SUBOPTIMAL",
            f"Estimated duration {total_duration:.2f}s is outside recommended range [{spec.recommended_min_duration_sec:.0f}s, {spec.recommended_max_duration_sec:.0f}s].",
            "Tune pacing slightly toward the recommended duration sweet spot.",
        )

    if catalog is not None:
        bp_tokens = tokenize_topic(blueprint.title)
        for pub in catalog.published:
            pub_tokens = tokenize_topic(pub.title)
            for kw in pub.keywords:
                pub_tokens |= tokenize_topic(kw)
            sim = jaccard_similarity(bp_tokens, pub_tokens)
            if pub.slug == blueprint.slug or sim >= 0.65:
                add_warning(
                    "DUPLICATE_TOPIC",
                    f"Topic closely overlaps previously published video '{pub.slug}' ({pub.title}).",
                    "Differentiate the angle or pick an uncovered concept from channels/catalog.json.",
                )

    metrics = BlueprintMetrics(
        beat_count=len(beats),
        total_words=sum(len(b.narration.split()) for b in beats),
        estimated_duration_sec=total_duration,
        hook_duration_sec=round(beat_durations[0], 2),
        iteration_count=iteration_count,
        version_progression=version_progression,
        beats_with_sfx=sum(1 for b in beats if b.sfx),
    )

    return ValidationReport(
        passed=len(errors) == 0,
        target=target,
        errors=errors,
        warnings=warnings,
        metrics=metrics,
    )


def validate_blueprint_file(
    path: Path,
    catalog: Catalog | None = None,
) -> ValidationReport:
    """Load, schema-validate, and lint a blueprint JSON file without raising raw tracebacks."""
    target = str(path)
    if not path.is_file():
        return ValidationReport(
            passed=False,
            target=target,
            errors=[
                LintIssue(
                    code="FILE_NOT_FOUND",
                    severity="error",
                    message=f"Blueprint file does not exist: {path}",
                    suggestion="Create the run via 'cm new <slug>' or provide a valid blueprint path.",
                )
            ],
            warnings=[],
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return ValidationReport(
            passed=False,
            target=target,
            errors=[
                LintIssue(
                    code="INVALID_JSON",
                    severity="error",
                    message=f"Malformed JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
                    suggestion="Fix JSON syntax errors before re-running cm validate.",
                )
            ],
            warnings=[],
        )

    try:
        blueprint = Blueprint.model_validate(raw)
    except ValidationError as exc:
        schema_errors = [
            LintIssue(
                code="SCHEMA_VALIDATION_ERROR",
                severity="error",
                message=f"{'.'.join(str(p) for p in err['loc']) or 'root'}: {err['msg']}",
                suggestion="Run 'cm schema blueprint' to inspect required fields and enum values.",
            )
            for err in exc.errors()
        ]
        return ValidationReport(
            passed=False,
            target=target,
            errors=schema_errors,
            warnings=[],
        )

    return validate_blueprint_data(blueprint, target=target, catalog=catalog)
