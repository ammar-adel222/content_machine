"""YouTube Shorts competitor intelligence, zero-key trend feeds, and format playbook."""

from __future__ import annotations

import html
import json
import math
import os
import re
import statistics
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from cm.models import (
    CompetitorVideo,
    FormatPatternSummary,
    ResearchPlaybook,
    ResearchSnapshot,
    TrendSignal,
)
from cm.paths import atomic_write_text, research_path
from cm.tts import load_env

DEFAULT_TREND_SEEDS: tuple[str, ...] = (
    "how does ai ",
    "how does ",
    "why does ",
    "explained in 60 seconds ",
)
VALID_TREND_SOURCES: frozenset[str] = frozenset({"youtube", "google", "hn", "reddit"})
_USER_AGENT = "content-machine/3.0 (research-cli)"
_ISO_DURATION_RE = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


class YouTubeOrder(StrEnum):
    """YouTube Data API v3 search ordering modes."""

    RELEVANCE = "relevance"
    VIEW_COUNT = "viewCount"
    DATE = "date"


class YouTubeSort(StrEnum):
    """Local ranking metrics applied after computing channel outlier ratios."""

    OUTLIER = "outlier"
    VIEWS = "views"
    VELOCITY = "velocity"


def redact_url(url: str) -> str:
    """Strip sensitive key query parameters before logging or raising HTTP errors."""
    return re.sub(r"(key=)[^&]+", r"\1***", url)


def http_get_text(
    url: str,
    timeout: float = 12.0,
    headers: dict[str, str] | None = None,
) -> str:
    """Fetch UTF-8 response body via stdlib urllib with safe error redaction."""
    req_headers = {"User-Agent": _USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bytes(resp.read()).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = bytes(exc.read()).decode("utf-8", errors="replace")[:300].strip()
        except Exception:
            detail = ""
        msg = detail or str(exc.reason)
        raise RuntimeError(f"HTTP {exc.code} fetching {redact_url(url)}: {msg}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Request failed for {redact_url(url)}: {exc}") from exc


def http_get_json(
    url: str,
    timeout: float = 12.0,
    headers: dict[str, str] | None = None,
) -> object:
    """Fetch and decode a JSON payload via http_get_text."""
    raw = http_get_text(url, timeout=timeout, headers=headers)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {redact_url(url)}: {exc}") from exc


def parse_iso8601_duration(raw: str) -> int:
    """Parse an ISO-8601 YouTube duration string (e.g. 'PT58S', 'PT1M12S') to seconds."""
    match = _ISO_DURATION_RE.fullmatch(raw.strip().upper())
    if match is None or not any(match.groups()):
        return 0
    days, hours, minutes, seconds = (int(group or 0) for group in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def classify_title_pattern(title: str) -> str:
    """Classify a video title into a structural hook archetype."""
    clean = title.strip()
    low = clean.lower()
    if re.search(r"\b(myths?|wrong|lie|lies|truth about|stop doing|never)\b", low):
        return "myth_bust"
    if re.search(r"\bvs\.?\b|\bversus\b", low):
        return "contrast_vs"
    if low.startswith("why ") or " why " in low:
        return "why"
    if low.startswith(("how to ", "how ")) or "how it works" in low:
        return "how_to"
    if re.search(r"(^|\s)\d+\s+[a-z]", low):
        return "number_list"
    if clean.endswith("?"):
        return "question"
    return "statement"


def fetch_opening_transcript(
    video_id: str,
    max_sec: float = 12.0,
    max_words: int = 35,
) -> str | None:
    """Extract the opening spoken hook lines from a YouTube video's timedtext captions."""
    watch_url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    try:
        page_html = http_get_text(watch_url)
    except RuntimeError:
        return None

    marker = '"captionTracks":'
    idx = page_html.find(marker)
    if idx == -1:
        return None
    tail = page_html[idx + len(marker) :].lstrip()
    try:
        tracks, _ = json.JSONDecoder().raw_decode(tail)
    except json.JSONDecodeError:
        return None
    if not isinstance(tracks, list):
        return None

    valid_tracks = [t for t in tracks if isinstance(t, dict) and t.get("baseUrl")]
    if not valid_tracks:
        return None

    chosen = next(
        (t for t in valid_tracks if str(t.get("languageCode", "")).lower().startswith("en")),
        valid_tracks[0],
    )
    try:
        xml_text = http_get_text(str(chosen["baseUrl"]))
        xml_root = ET.fromstring(xml_text)
    except (RuntimeError, ET.ParseError):
        return None

    chunks: list[str] = []
    for elem in xml_root.iter("text"):
        try:
            start = float(elem.attrib.get("start", "0"))
        except ValueError:
            continue
        if start > max_sec:
            continue
        raw_line = html.unescape("".join(elem.itertext()))
        collapsed = " ".join(raw_line.split())
        if collapsed:
            chunks.append(collapsed)

    words = " ".join(chunks).split()
    if not words:
        return None
    return " ".join(words[:max_words])


def _performance_rank_key(video: CompetitorVideo) -> tuple[float, float, int]:
    return (
        video.outlier_score if video.outlier_score is not None else -1.0,
        video.views_per_day,
        video.view_count,
    )


def summarize_format_patterns(videos: list[CompetitorVideo]) -> FormatPatternSummary:
    """Derive duration sweet spot, leading title patterns, and hook openers from videos."""
    if not videos:
        return FormatPatternSummary()

    ranked = sorted(videos, key=_performance_rank_key, reverse=True)
    median_views = int(statistics.median(v.view_count for v in videos))
    median_dur = round(float(statistics.median(v.duration_sec for v in videos)), 1)

    top_count = max(1, min(5, math.ceil(len(ranked) * 0.35)))
    top_slice = ranked[:top_count]
    durations = [v.duration_sec for v in top_slice]
    sweet_spot = (min(durations), max(durations))

    pattern_scores: dict[str, list[float]] = {}
    for v in ranked:
        score = v.outlier_score if v.outlier_score is not None else (v.views_per_day / 1000.0)
        pattern_scores.setdefault(v.title_pattern, []).append(score)
    top_patterns = [
        name
        for name, _ in sorted(
            pattern_scores.items(),
            key=lambda item: sum(item[1]) / len(item[1]),
            reverse=True,
        )[:4]
    ]

    top_hooks = [v.hook_transcript for v in ranked if v.hook_transcript][:5]

    takeaways = [
        f"Top performers run {sweet_spot[0]}-{sweet_spot[1]}s (median {median_dur}s across {len(videos)} videos).",
        f"Leading title patterns: {', '.join(top_patterns)}.",
    ]
    best = ranked[0]
    if best.outlier_score is not None:
        takeaways.append(
            f"Best outlier scored {best.outlier_score:.2f}x channel subscribers ({best.view_count:,} views)."
        )
    else:
        takeaways.append(
            f"Fastest velocity reached {best.views_per_day:,.1f} views/day ({best.view_count:,} views)."
        )

    return FormatPatternSummary(
        sample_size=len(videos),
        median_views=median_views,
        median_duration_sec=median_dur,
        sweet_spot_duration_sec=sweet_spot,
        top_title_patterns=top_patterns,
        top_hook_openers=top_hooks,
        takeaways=takeaways,
    )


def _sort_videos(videos: list[CompetitorVideo], sort_by: YouTubeSort) -> list[CompetitorVideo]:
    if sort_by == YouTubeSort.VIEWS:
        return sorted(videos, key=lambda v: (v.view_count, v.views_per_day), reverse=True)
    if sort_by == YouTubeSort.VELOCITY:
        return sorted(videos, key=lambda v: (v.views_per_day, v.view_count), reverse=True)
    return sorted(videos, key=_performance_rank_key, reverse=True)


def _parse_int_stat(raw: object) -> int:
    try:
        return max(0, int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def research_youtube(
    query: str,
    limit: int = 15,
    max_duration_sec: int = 90,
    shorts_only: bool = True,
    days: int | None = None,
    order: YouTubeOrder = YouTubeOrder.RELEVANCE,
    sort_by: YouTubeSort = YouTubeSort.OUTLIER,
    transcript_limit: int = 5,
    save: bool = True,
    root: Path | None = None,
) -> ResearchSnapshot:
    """Search YouTube Shorts/videos via Data API v3, rank outliers, and extract hook transcripts."""
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Search query cannot be blank")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if max_duration_sec < 1:
        raise ValueError("max_duration_sec must be >= 1")
    if transcript_limit < 0:
        raise ValueError("transcript_limit must be >= 0")
    if days is not None and days < 1:
        raise ValueError("days must be >= 1")

    load_env(root)
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "YOUTUBE_API_KEY is not set in .env or environment (see .env.example)"
        )

    search_params: dict[str, str] = {
        "part": "snippet",
        "type": "video",
        "q": clean_query,
        "maxResults": str(limit),
        "order": order.value,
        "key": api_key,
    }
    if shorts_only:
        search_params["videoDuration"] = "short"
    if days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        search_params["publishedAfter"] = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    search_url = (
        "https://www.googleapis.com/youtube/v3/search?"
        + urllib.parse.urlencode(search_params)
    )
    search_data = http_get_json(search_url)
    search_items = search_data.get("items", []) if isinstance(search_data, dict) else []

    video_ids: list[str] = []
    for item in search_items:
        vid = item.get("id", {}).get("videoId") if isinstance(item, dict) else None
        if isinstance(vid, str) and vid and vid not in video_ids:
            video_ids.append(vid)

    if not video_ids:
        empty_snap = ResearchSnapshot(
            kind="youtube",
            queries=[clean_query],
            format_summary=FormatPatternSummary(),
            warnings=[f"No YouTube videos matched query '{clean_query}'."],
        )
        if save:
            merge_snapshot_into_playbook(empty_snap, root=root)
        return empty_snap

    videos_url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(
        {
            "part": "snippet,contentDetails,statistics",
            "id": ",".join(video_ids),
            "key": api_key,
        }
    )
    videos_data = http_get_json(videos_url)
    video_items = videos_data.get("items", []) if isinstance(videos_data, dict) else []

    channel_ids: list[str] = []
    for item in video_items:
        ch_id = item.get("snippet", {}).get("channelId") if isinstance(item, dict) else None
        if isinstance(ch_id, str) and ch_id and ch_id not in channel_ids:
            channel_ids.append(ch_id)

    channel_subs: dict[str, int | None] = {}
    if channel_ids:
        channels_url = "https://www.googleapis.com/youtube/v3/channels?" + urllib.parse.urlencode(
            {"part": "statistics", "id": ",".join(channel_ids), "key": api_key}
        )
        channels_data = http_get_json(channels_url)
        ch_items = channels_data.get("items", []) if isinstance(channels_data, dict) else []
        for ch_item in ch_items:
            if not isinstance(ch_item, dict):
                continue
            cid = str(ch_item.get("id", ""))
            ch_stats = ch_item.get("statistics", {})
            if not cid or not isinstance(ch_stats, dict):
                continue
            if ch_stats.get("hiddenSubscriberCount") or "subscriberCount" not in ch_stats:
                channel_subs[cid] = None
            else:
                channel_subs[cid] = _parse_int_stat(ch_stats.get("subscriberCount"))

    now = datetime.now(UTC)
    parsed_videos: list[CompetitorVideo] = []
    filtered_out = 0
    for item in video_items:
        if not isinstance(item, dict):
            continue
        vid = str(item.get("id", ""))
        snippet = item.get("snippet", {})
        details = item.get("contentDetails", {})
        stats = item.get("statistics", {})
        duration_sec = parse_iso8601_duration(str(details.get("duration", "")))
        is_short = 0 < duration_sec <= max_duration_sec
        if shorts_only and not is_short:
            filtered_out += 1
            continue

        title = str(snippet.get("title", "")).strip()
        channel_id = str(snippet.get("channelId", ""))
        channel_title = str(snippet.get("channelTitle", ""))
        pub_raw = str(snippet.get("publishedAt", ""))
        published_at = (
            datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
            if pub_raw
            else now
        )
        age_days = max(0.04, (now - published_at).total_seconds() / 86400.0)

        view_count = _parse_int_stat(stats.get("viewCount"))
        like_count = _parse_int_stat(stats.get("likeCount"))
        comment_count = _parse_int_stat(stats.get("commentCount"))
        subs = channel_subs.get(channel_id)
        outlier_score = (
            round(view_count / subs, 3) if subs is not None and subs > 0 else None
        )
        views_per_day = round(view_count / age_days, 2)
        engagement_rate = (
            round((like_count + comment_count) / view_count, 4) if view_count > 0 else 0.0
        )

        parsed_videos.append(
            CompetitorVideo(
                video_id=vid,
                url=f"https://www.youtube.com/shorts/{vid}"
                if is_short
                else f"https://www.youtube.com/watch?v={vid}",
                title=title,
                channel_id=channel_id,
                channel_title=channel_title,
                published_at=published_at,
                duration_sec=duration_sec,
                is_short=is_short,
                view_count=view_count,
                like_count=like_count,
                comment_count=comment_count,
                subscriber_count=subs,
                outlier_score=outlier_score,
                views_per_day=views_per_day,
                engagement_rate=engagement_rate,
                title_pattern=classify_title_pattern(title),
                query=clean_query,
            )
        )

    ranked_videos = _sort_videos(parsed_videos, sort_by)
    for video in ranked_videos[:transcript_limit]:
        video.hook_transcript = fetch_opening_transcript(video.video_id)

    warnings: list[str] = []
    if filtered_out > 0:
        warnings.append(
            f"Filtered out {filtered_out} video(s) outside 1-{max_duration_sec}s Shorts window."
        )

    snapshot = ResearchSnapshot(
        kind="youtube",
        queries=[clean_query],
        videos=ranked_videos,
        format_summary=summarize_format_patterns(ranked_videos),
        warnings=warnings,
    )
    if save:
        merge_snapshot_into_playbook(snapshot, root=root)
    return snapshot


def fetch_autocomplete(seed: str, youtube: bool = True) -> list[TrendSignal]:
    """Fetch zero-key search autocomplete suggestions from YouTube or Google."""
    params: dict[str, str] = {"client": "firefox", "q": seed}
    if youtube:
        params["ds"] = "yt"
    url = "https://suggestqueries.google.com/complete/search?" + urllib.parse.urlencode(params)
    payload = http_get_json(url)
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        return []

    source: str = "youtube_autocomplete" if youtube else "google_autocomplete"
    base_url = (
        "https://www.youtube.com/results?search_query="
        if youtube
        else "https://www.google.com/search?q="
    )
    signals: list[TrendSignal] = []
    for item in payload[1]:
        text = str(item).strip()
        if not text:
            continue
        signals.append(
            TrendSignal(
                source=source,  # type: ignore[arg-type]
                seed=seed.strip(),
                title=text,
                url=base_url + urllib.parse.quote_plus(text),
            )
        )
    return signals


def fetch_hackernews_top(limit: int = 10) -> list[TrendSignal]:
    """Fetch top Hacker News front-page stories via Algolia's zero-key API."""
    url = f"https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage={limit}"
    payload = http_get_json(url)
    hits = payload.get("hits", []) if isinstance(payload, dict) else []
    signals: list[TrendSignal] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title", "")).strip()
        if not title:
            continue
        story_url = str(hit.get("url") or "").strip()
        if not story_url and hit.get("objectID"):
            story_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        signals.append(
            TrendSignal(
                source="hackernews",
                seed="front_page",
                title=title,
                url=story_url or None,
                score=hit.get("points"),
                comments=hit.get("num_comments"),
            )
        )
    return signals


def fetch_reddit_eli5(limit: int = 10, topic: str | None = None) -> list[TrendSignal]:
    """Fetch high-curiosity questions from r/explainlikeimfive without an API key."""
    clean_topic = topic.strip() if topic else ""
    if clean_topic:
        url = (
            "https://www.reddit.com/r/explainlikeimfive/search.json?"
            + urllib.parse.urlencode(
                {
                    "q": clean_topic,
                    "restrict_sr": "1",
                    "sort": "top",
                    "t": "year",
                    "limit": str(limit),
                }
            )
        )
        seed_label = clean_topic
    else:
        url = f"https://www.reddit.com/r/explainlikeimfive/top.json?t=month&limit={limit}"
        seed_label = "top_month"

    payload = http_get_json(url)
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    children = data.get("children", []) if isinstance(data, dict) else []
    signals: list[TrendSignal] = []
    for child in children:
        post = child.get("data", {}) if isinstance(child, dict) else {}
        if not isinstance(post, dict):
            continue
        raw_title = str(post.get("title", "")).strip()
        clean_title = re.sub(r"^eli5\s*[:\-]?\s*", "", raw_title, flags=re.IGNORECASE).strip()
        if not clean_title:
            continue
        permalink = str(post.get("permalink", "")).strip()
        signals.append(
            TrendSignal(
                source="reddit_eli5",
                seed=seed_label,
                title=clean_title,
                url=f"https://www.reddit.com{permalink}" if permalink else None,
                score=post.get("score"),
                comments=post.get("num_comments"),
            )
        )
    return signals


def research_trends(
    seeds: list[str] | None = None,
    sources: list[str] | None = None,
    limit: int = 10,
    topic: str | None = None,
    save: bool = True,
    root: Path | None = None,
) -> ResearchSnapshot:
    """Gather zero-key audience curiosity signals from autocomplete, HN, and Reddit."""
    if limit < 1:
        raise ValueError("limit must be >= 1")

    active_sources = [
        src.strip().lower()
        for src in (sources if sources is not None else ["youtube", "google", "hn", "reddit"])
        if src.strip()
    ]
    if not active_sources:
        raise ValueError("At least one trend source must be specified")
    invalid = sorted(set(active_sources) - VALID_TREND_SOURCES)
    if invalid:
        raise ValueError(
            f"Unsupported trend source(s): {', '.join(invalid)} "
            f"(valid: {', '.join(sorted(VALID_TREND_SOURCES))})"
        )

    if seeds:
        active_seeds = [s for s in seeds if s.strip()]
    elif topic and topic.strip():
        clean_topic = topic.strip()
        active_seeds = [f"how does {clean_topic} ", f"why does {clean_topic} "]
    else:
        active_seeds = list(DEFAULT_TREND_SEEDS)

    raw_signals: list[TrendSignal] = []
    warnings: list[str] = []

    for src in active_sources:
        if src in {"youtube", "google"}:
            is_yt = src == "youtube"
            for seed in active_seeds:
                try:
                    raw_signals.extend(fetch_autocomplete(seed, youtube=is_yt)[:limit])
                except RuntimeError as exc:
                    warnings.append(f"{src} ({seed.strip()}): {exc}")
        elif src == "hn":
            try:
                raw_signals.extend(fetch_hackernews_top(limit=limit))
            except RuntimeError as exc:
                warnings.append(f"hn: {exc}")
        elif src == "reddit":
            try:
                raw_signals.extend(fetch_reddit_eli5(limit=limit, topic=topic))
            except RuntimeError as exc:
                warnings.append(f"reddit: {exc}")

    deduped: list[TrendSignal] = []
    seen: set[tuple[str, str]] = set()
    for sig in raw_signals:
        key = (sig.source, sig.title.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(sig)

    queries = [s.strip() for s in active_seeds]
    if topic and topic.strip() and topic.strip() not in queries:
        queries.append(topic.strip())

    snapshot = ResearchSnapshot(
        kind="trends",
        queries=queries,
        trends=deduped,
        warnings=warnings,
    )
    if save:
        merge_snapshot_into_playbook(snapshot, root=root)
    return snapshot


def load_research_playbook(root: Path | None = None) -> ResearchPlaybook:
    """Load channels/research.json, initializing an empty playbook if missing."""
    path = research_path(root)
    if not path.is_file():
        playbook = ResearchPlaybook()
        save_research_playbook(playbook, root=root)
        return playbook
    try:
        return ResearchPlaybook.model_validate_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise ValueError(f"Corrupt research playbook at {path}: {exc}") from exc


def save_research_playbook(playbook: ResearchPlaybook, root: Path | None = None) -> Path:
    """Persist channels/research.json atomically."""
    return atomic_write_text(research_path(root), playbook.model_dump_json(indent=2) + "\n")


def merge_snapshot_into_playbook(
    snapshot: ResearchSnapshot,
    root: Path | None = None,
) -> ResearchPlaybook:
    """Merge a competitor/trend snapshot into channels/research.json and recompute summary."""
    playbook = load_research_playbook(root=root)

    if snapshot.videos:
        by_id = {v.video_id: v for v in playbook.videos}
        for video in snapshot.videos:
            by_id[video.video_id] = video
        merged_videos = sorted(by_id.values(), key=_performance_rank_key, reverse=True)[:200]
        playbook.videos = merged_videos
        playbook.format_summary = summarize_format_patterns(merged_videos)

    if snapshot.trends:
        merged_trends: list[TrendSignal] = []
        seen: set[tuple[str, str]] = set()
        for sig in [*snapshot.trends, *playbook.trends]:
            key = (sig.source, sig.title.lower())
            if key in seen:
                continue
            seen.add(key)
            merged_trends.append(sig)
        playbook.trends = merged_trends[:200]

    playbook.updated_at = datetime.now(UTC)
    save_research_playbook(playbook, root=root)
    return playbook


def add_playbook_insight(note: str, root: Path | None = None) -> ResearchPlaybook:
    """Append a human or agent learned format takeaway to channels/research.json."""
    clean = note.strip()
    if not clean:
        raise ValueError("Insight note cannot be blank")
    playbook = load_research_playbook(root=root)
    if clean not in playbook.learned_insights:
        playbook.learned_insights.append(clean)
    playbook.updated_at = datetime.now(UTC)
    save_research_playbook(playbook, root=root)
    return playbook
