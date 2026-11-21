"""Channel analytics synchronization back into channels/catalog.json."""

from __future__ import annotations

import os
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from cm.catalog import load_catalog, save_catalog
from cm.models import AnalyticsSyncReport, PlatformMetrics, PublishedEntry
from cm.publish import META_GRAPH_API_BASE, _get_facebook_env, get_youtube_access_token, http_json
from cm.tts import load_env


def _to_int(raw: object) -> int:
    try:
        return max(0, int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _summary_total(payload: dict[str, object], key: str) -> int:
    block = payload.get(key)
    summary = block.get("summary") if isinstance(block, dict) else None
    return _to_int(summary.get("total_count")) if isinstance(summary, dict) else 0


def sync_channel_analytics(root: Path | None = None) -> AnalyticsSyncReport:
    """Pull live views, likes, and comments for published Shorts/Reels into catalog.json."""
    load_env(root)
    catalog = load_catalog(root)
    now = datetime.now(UTC)

    yt_map: dict[str, PublishedEntry] = {}
    ig_items: list[tuple[PublishedEntry, str]] = []
    fb_items: list[tuple[PublishedEntry, str]] = []

    for entry in catalog.published:
        yt_meta = entry.platforms.get("youtube")
        if yt_meta and yt_meta.get("video_id"):
            yt_map[str(yt_meta["video_id"])] = entry
        ig_meta = entry.platforms.get("instagram")
        if ig_meta and ig_meta.get("video_id"):
            ig_items.append((entry, str(ig_meta["video_id"])))
        fb_meta = entry.platforms.get("facebook")
        if fb_meta and fb_meta.get("video_id"):
            fb_items.append((entry, str(fb_meta["video_id"])))

    warnings: list[str] = []
    youtube_updated = 0
    instagram_updated = 0
    facebook_updated = 0

    if yt_map:
        api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
        auth_headers: dict[str, str] | None = None
        if not api_key:
            try:
                token = get_youtube_access_token(root=root)
                auth_headers = {"Authorization": f"Bearer {token}"}
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                warnings.append(f"YouTube analytics skipped: {exc}")

        if api_key or auth_headers is not None:
            ids_list = list(yt_map.keys())
            for idx in range(0, len(ids_list), 50):
                chunk = ids_list[idx : idx + 50]
                params: dict[str, str] = {"part": "statistics", "id": ",".join(chunk)}
                if api_key:
                    params["key"] = api_key
                url = (
                    "https://www.googleapis.com/youtube/v3/videos?"
                    + urllib.parse.urlencode(params)
                )
                try:
                    payload = http_json(url, headers=auth_headers)
                except RuntimeError as exc:
                    warnings.append(f"YouTube analytics batch failed: {exc}")
                    continue

                items = payload.get("items", []) if isinstance(payload, dict) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    vid = str(item.get("id", ""))
                    stats = item.get("statistics", {})
                    entry = yt_map.get(vid)
                    if entry is None or not isinstance(stats, dict):
                        continue
                    entry.analytics["youtube"] = PlatformMetrics(
                        views=_to_int(stats.get("viewCount")),
                        likes=_to_int(stats.get("likeCount")),
                        comments=_to_int(stats.get("commentCount")),
                        synced_at=now,
                    )
                    youtube_updated += 1

    if ig_items:
        ig_token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
        if not ig_token:
            warnings.append("Instagram analytics skipped: IG_ACCESS_TOKEN is not set.")
        else:
            for entry, media_id in ig_items:
                url = (
                    f"https://graph.facebook.com/v21.0/{urllib.parse.quote(media_id)}?"
                    + urllib.parse.urlencode(
                        {"fields": "like_count,comments_count,views", "access_token": ig_token}
                    )
                )
                try:
                    payload = http_json(url)
                except RuntimeError as exc:
                    warnings.append(f"Instagram analytics failed for {media_id}: {exc}")
                    continue
                if isinstance(payload, dict):
                    entry.analytics["instagram"] = PlatformMetrics(
                        views=_to_int(payload.get("views")),
                        likes=_to_int(payload.get("like_count")),
                        comments=_to_int(payload.get("comments_count")),
                        synced_at=now,
                    )
                    instagram_updated += 1

    if fb_items:
        fb_token, _ = _get_facebook_env()
        if not fb_token:
            warnings.append("Facebook analytics skipped: FB_PAGE_ACCESS_TOKEN/IG_ACCESS_TOKEN is not set.")
        else:
            for entry, video_id in fb_items:
                url = (
                    f"{META_GRAPH_API_BASE}/{urllib.parse.quote(video_id)}?"
                    + urllib.parse.urlencode(
                        {
                            "fields": "views,likes.summary(true),comments.summary(true)",
                            "access_token": fb_token,
                        }
                    )
                )
                try:
                    payload = http_json(url)
                except RuntimeError as exc:
                    warnings.append(f"Facebook analytics failed for {video_id}: {exc}")
                    continue
                if isinstance(payload, dict):
                    entry.analytics["facebook"] = PlatformMetrics(
                        views=_to_int(payload.get("views")),
                        likes=_summary_total(payload, "likes"),
                        comments=_summary_total(payload, "comments"),
                        synced_at=now,
                    )
                    facebook_updated += 1

    if youtube_updated > 0 or instagram_updated > 0 or facebook_updated > 0:
        save_catalog(catalog, root=root)

    return AnalyticsSyncReport(
        synced_at=now,
        videos_checked=len(catalog.published),
        youtube_updated=youtube_updated,
        instagram_updated=instagram_updated,
        facebook_updated=facebook_updated,
        warnings=warnings,
    )
