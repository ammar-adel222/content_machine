"""Typed Pydantic v2 contracts for blueprints, run state, catalog, and validation."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _clean_non_empty(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Field cannot be blank")
    return cleaned


def _clean_single_line(value: str) -> str:
    cleaned = _clean_non_empty(value)
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("Field must be a single line without newlines")
    return cleaned


class FormatPreset(StrEnum):
    """Supported video aspect ratios and pacing targets."""

    SHORT_9X16 = "short_9x16"
    LONG_16X9 = "long_16x9"


class SafeZone(BaseModel):
    """Platform UI safe margins in pixels."""

    top: int
    bottom: int
    left: int
    right: int


class FormatSpec(BaseModel):
    """Resolution, frame rate, pacing, and safe-zone constraints per format."""

    preset: FormatPreset
    width: int
    height: int
    fps_preview: int
    fps_final: int
    hard_min_duration_sec: float
    recommended_min_duration_sec: float
    recommended_max_duration_sec: float
    hard_max_duration_sec: float
    min_iterations: int
    max_beat_warn_sec: float
    max_beat_error_sec: float
    safe_zone: SafeZone


FORMAT_SPECS: dict[FormatPreset, FormatSpec] = {
    FormatPreset.SHORT_9X16: FormatSpec(
        preset=FormatPreset.SHORT_9X16,
        width=1080,
        height=1920,
        fps_preview=30,
        fps_final=60,
        hard_min_duration_sec=30.0,
        recommended_min_duration_sec=45.0,
        recommended_max_duration_sec=85.0,
        hard_max_duration_sec=95.0,
        min_iterations=3,
        max_beat_warn_sec=9.0,
        max_beat_error_sec=13.0,
        safe_zone=SafeZone(top=160, bottom=360, left=64, right=160),
    ),
    FormatPreset.LONG_16X9: FormatSpec(
        preset=FormatPreset.LONG_16X9,
        width=1920,
        height=1080,
        fps_preview=30,
        fps_final=60,
        hard_min_duration_sec=150.0,
        recommended_min_duration_sec=240.0,
        recommended_max_duration_sec=600.0,
        hard_max_duration_sec=900.0,
        min_iterations=5,
        max_beat_warn_sec=18.0,
        max_beat_error_sec=28.0,
        safe_zone=SafeZone(top=64, bottom=96, left=80, right=80),
    ),
}


class TopicDomain(StrEnum):
    """Core CS and AI subject categories."""

    CS = "cs"
    AI = "ai"
    SYSTEMS = "systems"
    SECURITY = "security"
    NETWORKING = "networking"
    DATABASES = "databases"


class SfxCue(StrEnum):
    """Semantic sound-effect slots resolved by assets/sfx/manifest.json."""

    POP_BUILD = "pop_build"
    CLICK_UI = "click_ui"
    WHOOSH = "whoosh"
    TICK_DATA = "tick_data"
    RISER_STRESS = "riser_stress"
    PULSE_LOAD = "pulse_load"
    IMPACT_FAIL = "impact_fail"
    GLITCH_ERROR = "glitch_error"
    ALARM_BREACH = "alarm_breach"
    STAMP_HIT = "stamp_hit"
    CHIME_FIX = "chime_fix"
    LOCK_UPGRADE = "lock_upgrade"


class SfxTrigger(BaseModel):
    """A sound-effect cue anchored within a beat."""

    cue: SfxCue
    at: Literal["start", "mid", "end"] | float = "start"
    gain_db: float = Field(default=-12.0, ge=-30.0, le=0.0)

    @field_validator("at", mode="before")
    @classmethod
    def reject_bool_at(cls, value: object) -> object:
        """Reject booleans before Pydantic coerces True/False to float 1.0/0.0."""
        if isinstance(value, bool):
            raise ValueError("'at' cannot be a boolean")
        return value

    @field_validator("at")
    @classmethod
    def validate_at(cls, value: str | float) -> str | float:
        """Ensure fractional offsets lie within [0.0, 1.0]."""
        if isinstance(value, float) and not 0.0 <= value <= 1.0:
            raise ValueError("Numeric 'at' offset must be between 0.0 and 1.0")
        return value


class BeatKind(StrEnum):
    """Narrative role of a beat in the Build -> Stress-Test -> Fail -> Upgrade loop."""

    HOOK = "hook"
    BUILD = "build"
    STRESS_TEST = "stress_test"
    FAIL = "fail"
    UPGRADE = "upgrade"
    PAYOFF = "payoff"


class SystemStatus(StrEnum):
    """HUD status indicator displayed on the persistent system diagram."""

    INITIALIZING = "initializing"
    BUILDING = "building"
    TESTING = "testing"
    COMPROMISED = "compromised"
    DEGRADED = "degraded"
    UPGRADED = "upgraded"
    RESILIENT = "resilient"


class MetricCallout(BaseModel):
    """Optional live metric badge rendered inside the 2D HUD."""

    label: str = Field(min_length=1, max_length=24)
    value: str = Field(min_length=1, max_length=24)
    severity: Literal["neutral", "warn", "danger", "good"] = "neutral"

    @field_validator("label", "value")
    @classmethod
    def validate_single_line(cls, value: str) -> str:
        """Strip whitespace and disallow blank or multiline metric strings."""
        return _clean_single_line(value)


class VoiceSettings(BaseModel):
    """Gemini TTS voice parameters and pacing rate."""

    voice_name: str = Field(default="Charon", min_length=1)
    style_prompt: str | None = None
    target_wpm: int = Field(default=165, ge=120, le=220)

    @field_validator("voice_name")
    @classmethod
    def validate_voice_name(cls, value: str) -> str:
        """Ensure voice_name is non-blank and single-line."""
        return _clean_single_line(value)


class Beat(BaseModel):
    """Single timed beat inside a Build -> Stress-Test -> Fail -> Upgrade script."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    kind: BeatKind
    version_label: str = Field(min_length=1)
    system_status: SystemStatus
    narration: str
    on_screen_headline: str
    visual_state: str
    visual_diff: str
    stress_test_trigger: str | None = None
    failure_stamp: str | None = None
    metric_callout: MetricCallout | None = None
    pause_after_ms: int = Field(default=180, ge=0, le=1200)
    sfx: list[SfxTrigger] = Field(default_factory=list)
    estimated_duration_sec: float | None = Field(default=None, gt=0.0)

    @field_validator("version_label", "on_screen_headline")
    @classmethod
    def validate_single_line_text(cls, value: str) -> str:
        """Strip whitespace and enforce non-empty single-line HUD fields."""
        return _clean_single_line(value)

    @field_validator("narration", "visual_state", "visual_diff")
    @classmethod
    def require_non_empty_text(cls, value: str) -> str:
        """Strip whitespace and reject blank strings."""
        return _clean_non_empty(value)

    @field_validator("failure_stamp")
    @classmethod
    def validate_failure_stamp(cls, value: str | None) -> str | None:
        """Normalize blank failure stamps to None and disallow multiline stamps."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if "\n" in cleaned or "\r" in cleaned:
            raise ValueError("failure_stamp must be a single line without newlines")
        return cleaned


class Blueprint(BaseModel):
    """Complete pre-production contract for a single video."""

    schema_version: Literal["1.0"] = "1.0"
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=100)
    topic_domain: TopicDomain
    format: FormatPreset = FormatPreset.SHORT_9X16
    core_premise: str = Field(min_length=1)
    persistent_subject: str = Field(min_length=1)
    target_audience: str = "curious non-technical viewers"
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    beats: list[Beat] = Field(min_length=4)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Ensure title is single-line and non-blank."""
        return _clean_single_line(value)

    @field_validator("core_premise", "persistent_subject", "target_audience")
    @classmethod
    def validate_prose_fields(cls, value: str) -> str:
        """Ensure descriptive blueprint fields are non-blank."""
        return _clean_non_empty(value)


class PipelineStage(StrEnum):
    """Ordered production stages for a single run."""

    IDEATE = "ideate"
    BLUEPRINT = "blueprint"
    AUDIO = "audio"
    SCENE = "scene"
    QA = "qa"
    RENDER = "render"
    PUBLISH = "publish"


PIPELINE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.IDEATE,
    PipelineStage.BLUEPRINT,
    PipelineStage.AUDIO,
    PipelineStage.SCENE,
    PipelineStage.QA,
    PipelineStage.RENDER,
    PipelineStage.PUBLISH,
)


class StageStatus(StrEnum):
    """Execution status of an individual pipeline stage."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class StageRecord(BaseModel):
    """Checkpoint record for one stage inside runs/<slug>/state.json."""

    status: StageStatus = StageStatus.PENDING
    updated_at: datetime | None = None
    artifact: str | None = None
    notes: str | None = None
    error_count: int = 0


class RunState(BaseModel):
    """File-backed state machine checkpoint persisted in runs/<slug>/state.json."""

    schema_version: Literal["1.0"] = "1.0"
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str
    topic_domain: TopicDomain
    format: FormatPreset
    idea_id: str | None = None
    created_at: datetime
    updated_at: datetime
    current_stage: PipelineStage = PipelineStage.BLUEPRINT
    stages: dict[PipelineStage, StageRecord]

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Ensure run title is single-line and non-blank."""
        return _clean_single_line(value)


class IdeaStatus(StrEnum):
    """Lifecycle status of an idea in channels/catalog.json."""

    BACKLOG = "backlog"
    IN_PROGRESS = "in_progress"
    PUBLISHED = "published"
    REJECTED = "rejected"


class IdeaEntry(BaseModel):
    """Idea backlog entry with an explicit multi-step failure chain."""

    id: str
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=100)
    topic_domain: TopicDomain
    format: FormatPreset = FormatPreset.SHORT_9X16
    hook: str = Field(min_length=1)
    failure_chain: list[str] = Field(min_length=3)
    score: int = Field(default=7, ge=1, le=10)
    status: IdeaStatus = IdeaStatus.BACKLOG
    created_at: datetime

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Ensure idea title is single-line and non-blank."""
        return _clean_single_line(value)

    @field_validator("hook")
    @classmethod
    def validate_hook(cls, value: str) -> str:
        """Ensure hook is non-blank."""
        return _clean_non_empty(value)

    @field_validator("failure_chain")
    @classmethod
    def validate_failure_chain(cls, value: list[str]) -> list[str]:
        """Strip each failure step and reject blank items."""
        return [_clean_non_empty(step) for step in value]


class PlatformMetrics(BaseModel):
    """Per-platform engagement stats pulled by cm analytics sync."""

    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    synced_at: datetime


class PublishedEntry(BaseModel):
    """Receipt of a published video stored for history and deduplication."""

    slug: str
    title: str
    topic_domain: TopicDomain
    format: FormatPreset
    published_at: datetime
    platforms: dict[str, dict[str, str]] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list)
    analytics: dict[str, PlatformMetrics] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Ensure published title is single-line and non-blank."""
        return _clean_single_line(value)


class Catalog(BaseModel):
    """Channel-level idea backlog and published archive."""

    schema_version: Literal["1.0"] = "1.0"
    channel_name: str = "Content Machine 3.0"
    default_format: FormatPreset = FormatPreset.SHORT_9X16
    ideas: list[IdeaEntry] = Field(default_factory=list)
    published: list[PublishedEntry] = Field(default_factory=list)


class LintIssue(BaseModel):
    """Single actionable error or warning emitted by the blueprint validator."""

    code: str
    severity: Literal["error", "warning"]
    beat_id: str | None = None
    message: str
    suggestion: str


class BlueprintMetrics(BaseModel):
    """Summary pacing and structural metrics calculated from a blueprint."""

    beat_count: int
    total_words: int
    estimated_duration_sec: float
    hook_duration_sec: float
    iteration_count: int
    version_progression: list[str]
    beats_with_sfx: int


class ValidationReport(BaseModel):
    """Structured output returned by cm validate."""

    passed: bool
    target: str
    errors: list[LintIssue]
    warnings: list[LintIssue]
    metrics: BlueprintMetrics | None = None


class QaViolation(BaseModel):
    """Single visual, layout, or runtime violation detected by cm snapshot."""

    code: str
    beat_id: str | None = None
    label: str | None = None
    t: float | None = None
    element: str = ""
    message: str


class KeyframeAudit(BaseModel):
    """Capture and DOM layout audit summary for a single keyframe."""

    beat_id: str
    beat_index: int
    label: str
    t: float
    frame_path: str
    passed: bool
    stage_element_count: int
    pixel_stddev: float
    violations: list[QaViolation] = Field(default_factory=list)


class BeatDiffCheck(BaseModel):
    """Stage-cropped visual difference check between consecutive beats."""

    from_beat_id: str
    to_beat_id: str
    diff_pct: float
    passed: bool


class QaReport(BaseModel):
    """Structured visual QA report persisted to runs/<slug>/qa/report.json."""

    schema_version: Literal["1.0"] = "1.0"
    slug: str
    scene_path: str
    format: FormatPreset
    width: int
    height: int
    mode: Literal["all", "mid"]
    passed: bool
    contact_sheet_path: str
    frame_count: int
    error_count: int
    beat_diffs: list[BeatDiffCheck] = Field(default_factory=list)
    keyframes: list[KeyframeAudit] = Field(default_factory=list)
    violations: list[QaViolation] = Field(default_factory=list)


class WordTiming(BaseModel):
    """Per-word timestamp used for synced kinetic captions."""

    word: str
    start_sec: float
    end_sec: float


class TimedBeatAudio(BaseModel):
    """Synthesized speech clip and timing boundaries for a single beat."""

    beat_id: str
    kind: BeatKind
    version_label: str
    narration: str
    cache_key: str
    clip_path: str
    provider: Literal["gemini", "mock"]
    model: str = ""
    start_sec: float
    speech_end_sec: float
    end_sec: float
    duration_sec: float
    pause_after_ms: int
    word_timings: list[WordTiming] = Field(default_factory=list)


class ScheduledSfx(BaseModel):
    """Resolved sound-effect placement within the mastered audio mix."""

    beat_id: str
    cue: SfxCue
    time_sec: float
    gain_db: float
    file_path: str


class AudioTimeline(BaseModel):
    """Complete audio synthesis and mastering timeline at runs/<slug>/audio/timeline.json."""

    schema_version: Literal["1.0"] = "1.0"
    slug: str
    provider: Literal["gemini", "mock"]
    model: str
    voice_name: str
    sample_rate: int = 48000
    total_duration_sec: float
    voice_track_path: str
    master_audio_path: str | None = None
    target_lufs: float = -14.0
    measured_lufs: float | None = None
    fallback_events: list[str] = Field(default_factory=list)
    beats: list[TimedBeatAudio] = Field(default_factory=list)
    sfx_events: list[ScheduledSfx] = Field(default_factory=list)


class RenderReport(BaseModel):
    """Rendered video artifact metadata at runs/<slug>/render_report.json."""

    schema_version: Literal["1.0"] = "1.0"
    slug: str
    scene_path: str
    output_path: str
    format: FormatPreset
    quality: Literal["preview", "final"]
    frame_format: Literal["jpeg", "png"]
    width: int
    height: int
    fps: int
    total_frames: int
    workers: int
    video_codec: str
    pix_fmt: str
    audio_codec: str | None = None
    audio_source: str | None = None
    duration_sec: float
    file_size_bytes: int
    elapsed_sec: float


class CompetitorVideo(BaseModel):
    """Normalized competitor YouTube video / Short record with performance ratios."""

    video_id: str
    url: str
    title: str
    channel_id: str
    channel_title: str
    published_at: datetime
    duration_sec: int = Field(ge=0)
    is_short: bool = True
    view_count: int = Field(default=0, ge=0)
    like_count: int = Field(default=0, ge=0)
    comment_count: int = Field(default=0, ge=0)
    subscriber_count: int | None = Field(default=None, ge=0)
    outlier_score: float | None = None
    views_per_day: float = 0.0
    engagement_rate: float = 0.0
    hook_transcript: str | None = None
    title_pattern: str = "statement"
    query: str = ""


class FormatPatternSummary(BaseModel):
    """Aggregated format, pacing, and hook patterns observed across researched videos."""

    sample_size: int = 0
    median_views: int = 0
    median_duration_sec: float = 0.0
    sweet_spot_duration_sec: tuple[int, int] | None = None
    top_title_patterns: list[str] = Field(default_factory=list)
    top_hook_openers: list[str] = Field(default_factory=list)
    takeaways: list[str] = Field(default_factory=list)


class TrendSignal(BaseModel):
    """Audience curiosity or demand signal from zero-key search/community feeds."""

    source: Literal[
        "youtube_autocomplete",
        "google_autocomplete",
        "hackernews",
        "reddit_eli5",
    ]
    seed: str
    title: str
    url: str | None = None
    score: int | None = None
    comments: int | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchSnapshot(BaseModel):
    """Point-in-time output from cm research youtube or cm research trends."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["youtube", "trends"]
    queries: list[str] = Field(default_factory=list)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    videos: list[CompetitorVideo] = Field(default_factory=list)
    trends: list[TrendSignal] = Field(default_factory=list)
    format_summary: FormatPatternSummary | None = None
    warnings: list[str] = Field(default_factory=list)


class ResearchPlaybook(BaseModel):
    """Persistent research intelligence & learned format playbook at channels/research.json."""

    schema_version: Literal["1.0"] = "1.0"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    videos: list[CompetitorVideo] = Field(default_factory=list)
    trends: list[TrendSignal] = Field(default_factory=list)
    learned_insights: list[str] = Field(default_factory=list)
    format_summary: FormatPatternSummary = Field(default_factory=FormatPatternSummary)


class PublishPlatform(StrEnum):
    """Supported social publishing targets."""

    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


class YouTubePrivacy(StrEnum):
    """YouTube upload visibility status values."""

    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class PlatformPublishResult(BaseModel):
    """Outcome for a single platform during cm publish."""

    platform: Literal["youtube", "instagram", "facebook"]
    status: Literal["dry_run", "published", "skipped", "failed"]
    video_id: str | None = None
    url: str | None = None
    privacy: str | None = None
    staged_url: str | None = None
    error: str | None = None


class PublishReceipt(BaseModel):
    """Release publishing receipt written to runs/<slug>/publish_receipt.json."""

    schema_version: Literal["1.0"] = "1.0"
    slug: str
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    video_path: str
    dry_run: bool = False
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    results: list[PlatformPublishResult] = Field(default_factory=list)


class AnalyticsSyncReport(BaseModel):
    """Summary returned by cm analytics sync across published catalog videos."""

    schema_version: Literal["1.0"] = "1.0"
    synced_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    videos_checked: int = 0
    youtube_updated: int = 0
    instagram_updated: int = 0
    facebook_updated: int = 0
    warnings: list[str] = Field(default_factory=list)
