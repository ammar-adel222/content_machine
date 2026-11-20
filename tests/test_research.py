"""Offline exhaustive tests for Milestone 6 cm research (YouTube Shorts, trends, playbook)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cm.cli import app
from cm.models import (
    CompetitorVideo,
    ResearchPlaybook,
    ResearchSnapshot,
)
from cm.paths import research_path
from cm.research import (
    YouTubeOrder,
    YouTubeSort,
    add_playbook_insight,
    classify_title_pattern,
    fetch_autocomplete,
    fetch_hackernews_top,
    fetch_opening_transcript,
    fetch_reddit_eli5,
    http_get_json,
    http_get_text,
    load_research_playbook,
    merge_snapshot_into_playbook,
    parse_iso8601_duration,
    redact_url,
    research_trends,
    research_youtube,
    summarize_format_patterns,
)

runner = CliRunner()


def test_utilities_duration_patterns_and_http_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert redact_url("https://api.example/v3/search?q=ai&key=SECRET_123&max=5") == (
        "https://api.example/v3/search?q=ai&key=***&max=5"
    )

    assert parse_iso8601_duration("PT58S") == 58
    assert parse_iso8601_duration("PT1M12S") == 72
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration("P1DT1M") == 86460
    assert parse_iso8601_duration("PT") == 0
    assert parse_iso8601_duration("invalid") == 0

    assert classify_title_pattern("Stop Doing This In SQL") == "myth_bust"
    assert classify_title_pattern("SQL vs NoSQL In 60s") == "contrast_vs"
    assert classify_title_pattern("Why Passwords Leak So Easily") == "why"
    assert classify_title_pattern("How B-Trees Actually Work") == "how_to"
    assert classify_title_pattern("5 Mistakes Every Dev Makes") == "number_list"
    assert classify_title_pattern("Can AI Crack AES-256?") == "question"
    assert classify_title_pattern("Inside A Modern CPU Cache") == "statement"

    class _FakeResp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    seen_headers: list[dict[str, str]] = []

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 0.0) -> _FakeResp:
        _ = timeout
        seen_headers.append(dict(req.header_items()))
        if "bad-json" in req.full_url:
            return _FakeResp(b"{broken")
        if "http-err" in req.full_url:
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                Message(),
                io.BytesIO(b"quotaExceeded"),
            )
        if "unreadable-err" in req.full_url:
            err = urllib.error.HTTPError(req.full_url, 500, "ServerErr", Message(), None)
            err.read = lambda: (_ for _ in ()).throw(OSError("closed"))  # type: ignore[method-assign]
            raise err
        if "net-err" in req.full_url:
            raise urllib.error.URLError("DNS down")
        return _FakeResp(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    assert http_get_text("https://example.com/ok?key=SECRET", headers={"X-Test": "1"}) == '{"ok": true}'
    assert any(h.get("X-test") == "1" for h in seen_headers)
    assert http_get_json("https://example.com/ok") == {"ok": True}

    with pytest.raises(RuntimeError, match="Invalid JSON"):
        http_get_json("https://example.com/bad-json?key=MYSECRET")

    with pytest.raises(RuntimeError, match=r"HTTP 403 .*key=\*\*\*.*quotaExceeded") as exc_info:
        http_get_text("https://example.com/http-err?key=MYSECRET")
    assert "MYSECRET" not in str(exc_info.value)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        http_get_text("https://example.com/unreadable-err?key=MYSECRET")

    with pytest.raises(RuntimeError, match="Request failed"):
        http_get_text("https://example.com/net-err?key=MYSECRET")


def test_fetch_opening_transcript_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: dict[str, str] = {}

    def _fake_get_text(url: str, timeout: float = 12.0, headers: dict[str, str] | None = None) -> str:
        _ = (timeout, headers)
        if url not in pages:
            raise RuntimeError("network error")
        return pages[url]

    monkeypatch.setattr("cm.research.http_get_text", _fake_get_text)

    # 1. Watch page failure & missing captionTracks
    assert fetch_opening_transcript("vid_fail") is None
    pages["https://www.youtube.com/watch?v=vid_no_tracks"] = "<html>no captions</html>"
    assert fetch_opening_transcript("vid_no_tracks") is None

    # 2. Corrupt JSON, non-list tracks, and empty valid tracks
    pages["https://www.youtube.com/watch?v=vid_bad_json"] = '{"captionTracks":[broken'
    assert fetch_opening_transcript("vid_bad_json") is None
    pages["https://www.youtube.com/watch?v=vid_not_list"] = '{"captionTracks":{"bad":1}}'
    assert fetch_opening_transcript("vid_not_list") is None
    pages["https://www.youtube.com/watch?v=vid_no_url"] = '{"captionTracks":[{"languageCode":"en"}]}'
    assert fetch_opening_transcript("vid_no_url") is None

    # 3. English track selection, XML error handling, window filter & word limit
    tracks = [
        {"languageCode": "es", "baseUrl": "https://caps.local/es"},
        {"languageCode": "en-US", "baseUrl": "https://caps.local/en"},
    ]
    pages["https://www.youtube.com/watch?v=vid_ok"] = f'"captionTracks":{json.dumps(tracks)}'
    pages["https://caps.local/en"] = "<broken-xml"
    assert fetch_opening_transcript("vid_ok") is None

    pages["https://caps.local/en"] = (
        '<transcript>'
        '<text start="0.0">Never store &amp; hash&#39;s raw</text>'
        '<text start="not-float">skip me</text>'
        '<text start="2.5">   </text>'
        '<text start="4.0">before attackers dump everything</text>'
        '<text start="15.5">late sentence outside window</text>'
        '</transcript>'
    )
    hook = fetch_opening_transcript("vid_ok", max_sec=10.0, max_words=6)
    assert hook == "Never store & hash's raw before"

    # Empty text yields None
    pages["https://caps.local/en"] = '<transcript><text start="20.0">too late</text></transcript>'
    assert fetch_opening_transcript("vid_ok", max_sec=10.0) is None

    # Falls back to non-English track when no English exists
    pages["https://www.youtube.com/watch?v=vid_de"] = (
        '"captionTracks":[{"languageCode":"de","baseUrl":"https://caps.local/de"}]'
    )
    pages["https://caps.local/de"] = '<transcript><text start="0.5">Hallo Welt</text></transcript>'
    assert fetch_opening_transcript("vid_de") == "Hallo Welt"


def test_research_youtube_full_ranking_filtering_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

    # Empty format summary
    assert summarize_format_patterns([]).sample_size == 0

    # Argument & missing key validation
    with pytest.raises(ValueError, match="blank"):
        research_youtube("   ")
    with pytest.raises(ValueError, match="limit"):
        research_youtube("ai", limit=0)
    with pytest.raises(ValueError, match="max_duration_sec"):
        research_youtube("ai", max_duration_sec=0)
    with pytest.raises(ValueError, match="transcript_limit"):
        research_youtube("ai", transcript_limit=-1)
    with pytest.raises(ValueError, match="days"):
        research_youtube("ai", days=0)
    with pytest.raises(ValueError, match="YOUTUBE_API_KEY is not set"):
        research_youtube("ai")

    (tmp_path / ".env").write_text("YOUTUBE_API_KEY=yt-test-key\n", encoding="utf-8")

    now = datetime.now(UTC)
    two_days_ago = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    twenty_days_ago = (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")

    captured_urls: list[str] = []

    def _fake_youtube_json(
        url: str, timeout: float = 12.0, headers: dict[str, str] | None = None
    ) -> object:
        _ = (timeout, headers)
        captured_urls.append(url)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        q_val = params.get("q", [""])[0]

        if parsed.path.endswith("/search"):
            if q_val == "empty":
                return {"items": []}
            return {
                "items": [
                    {"id": {"videoId": "v_outlier"}},
                    {"id": {"videoId": "v_big"}},
                    {"id": {"videoId": "v_hidden"}},
                    {"id": {"videoId": "v_toolong"}},
                ]
            }

        if parsed.path.endswith("/videos"):
            return {
                "items": [
                    "not-a-dict",
                    {
                        "id": "v_outlier",
                        "snippet": {
                            "title": "Why Passwords Fail Without Salt 🚀",
                            "channelId": "c_small",
                            "channelTitle": "TinyLab",
                            "publishedAt": two_days_ago,
                        },
                        "contentDetails": {"duration": "PT48S"},
                        "statistics": {"viewCount": "200000", "likeCount": "12000", "commentCount": "500"},
                    },
                    {
                        "id": "v_big",
                        "snippet": {
                            "title": "How Hashing Works",
                            "channelId": "c_huge",
                            "channelTitle": "MegaTech",
                            "publishedAt": twenty_days_ago,
                        },
                        "contentDetails": {"duration": "PT55S"},
                        "statistics": {"viewCount": "500000", "likeCount": "10000", "commentCount": "bad"},
                    },
                    {
                        "id": "v_hidden",
                        "snippet": {
                            "title": "SQL vs NoSQL",
                            "channelId": "c_hidden",
                            "channelTitle": "SecretCh",
                            "publishedAt": "",
                        },
                        "contentDetails": {"duration": "PT35S"},
                        "statistics": {"viewCount": "0"},
                    },
                    {
                        "id": "v_toolong",
                        "snippet": {
                            "title": "Deep Dive 2 Hours",
                            "channelId": "c_huge",
                            "channelTitle": "MegaTech",
                            "publishedAt": twenty_days_ago,
                        },
                        "contentDetails": {"duration": "PT15M"},
                        "statistics": {"viewCount": "900000"},
                    },
                ]
            }

        if parsed.path.endswith("/channels"):
            return {
                "items": [
                    "bad-item",
                    {"id": "c_small", "statistics": {"subscriberCount": "10000"}},
                    {"id": "c_huge", "statistics": {"subscriberCount": "2000000"}},
                    {"id": "c_hidden", "statistics": {"hiddenSubscriberCount": True}},
                ]
            }
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("cm.research.http_get_json", _fake_youtube_json)
    monkeypatch.setattr(
        "cm.research.fetch_opening_transcript",
        lambda vid: f"Hook opener for {vid}" if vid == "v_outlier" else None,
    )

    # Empty search result branch
    empty_snap = research_youtube("empty", save=False)
    assert empty_snap.videos == []
    assert "No YouTube videos matched" in empty_snap.warnings[0]

    # Empty with save=True creates empty playbook
    research_youtube("empty", save=True)
    assert research_path().is_file()

    # Main Shorts outlier scan
    snap = research_youtube(
        "password hashing",
        limit=10,
        max_duration_sec=60,
        days=30,
        order=YouTubeOrder.RELEVANCE,
        sort_by=YouTubeSort.OUTLIER,
        transcript_limit=2,
        save=True,
    )
    assert any("publishedAfter=" in u for u in captured_urls)
    assert any("videoDuration=short" in u for u in captured_urls)
    assert [v.video_id for v in snap.videos] == ["v_outlier", "v_big", "v_hidden"]
    assert snap.videos[0].outlier_score == 20.0
    assert snap.videos[0].hook_transcript == "Hook opener for v_outlier"
    assert snap.videos[1].outlier_score == 0.25
    assert snap.videos[1].comment_count == 0
    assert snap.videos[2].subscriber_count is None
    assert snap.videos[2].outlier_score is None
    assert snap.videos[2].engagement_rate == 0.0
    assert any("Filtered out 1 video(s)" in w for w in snap.warnings)
    assert snap.format_summary is not None
    assert snap.format_summary.sweet_spot_duration_sec is not None

    # Sort modes: VIEWS & VELOCITY, plus all_durations keeping watch URL
    snap_views = research_youtube(
        "password hashing",
        shorts_only=False,
        sort_by=YouTubeSort.VIEWS,
        transcript_limit=0,
        save=False,
    )
    assert [v.video_id for v in snap_views.videos] == ["v_toolong", "v_big", "v_outlier", "v_hidden"]
    assert snap_views.videos[0].url == "https://www.youtube.com/watch?v=v_toolong"
    assert snap_views.format_summary is not None
    assert "Best outlier scored" in snap_views.format_summary.takeaways[-1]
    hidden_summary = summarize_format_patterns([snap.videos[2]])
    assert "Fastest velocity" in hidden_summary.takeaways[-1]

    snap_vel = research_youtube("password hashing", sort_by=YouTubeSort.VELOCITY, save=False)
    assert snap_vel.videos[0].video_id == "v_outlier"

    # CLI output (handles Unicode emoji cleanly under cp1252) & JSON output
    res_human = runner.invoke(app, ["research", "youtube", "password hashing", "--limit", "5"])
    assert res_human.exit_code == 0, res_human.output
    res_human.output.encode("cp1252")
    assert "YouTube Competitors" in res_human.output
    assert "Format Takeaways:" in res_human.output

    res_json = runner.invoke(app, ["research", "youtube", "password hashing", "--json"])
    assert res_json.exit_code == 0, res_json.output
    parsed_snap = ResearchSnapshot.model_validate_json(res_json.output)
    assert len(parsed_snap.videos) == 3

    res_err = runner.invoke(app, ["research", "youtube", "   ", "--json"])
    assert res_err.exit_code == 1
    assert "error" in json.loads(res_err.output)


def test_trends_feeds_playbook_merge_insights_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    def _fake_trends_json(
        url: str, timeout: float = 12.0, headers: dict[str, str] | None = None
    ) -> object:
        _ = (timeout, headers)
        if "suggestqueries.google.com" in url:
            if "failseed" in url:
                raise RuntimeError("autocomplete down")
            if "ds=yt" in url:
                return ["how does ", ["how does cpu cache work", "  ", "how does tls work ⚡"]]
            return ["how does ", ["how does cpu cache work", "how does ram work"]]
        if "hn.algolia.com" in url:
            return {
                "hits": [
                    "bad",
                    {"title": "", "objectID": "0"},
                    {"title": "Show HN: 2D Blueprint Engine", "url": "https://hn.example/1", "points": 320, "num_comments": 45},
                    {"title": "Ask HN: Best cache books?", "objectID": "999", "points": 80},
                ]
            }
        if "reddit.com" in url:
            if "search.json" in url:
                return {
                    "data": {
                        "children": [
                            {"data": {"title": "ELI5: How do SQL indexes work?", "permalink": "/r/eli5/1", "score": 950, "num_comments": 77}},
                            {"data": {"title": "ELI5 -   ", "permalink": "/r/eli5/empty"}},
                        ]
                    }
                }
            raise RuntimeError("Reddit 429 rate limited")
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("cm.research.http_get_json", _fake_trends_json)

    # Validation errors
    with pytest.raises(ValueError, match="limit"):
        research_trends(limit=0)
    with pytest.raises(ValueError, match="At least one"):
        research_trends(sources=[])
    with pytest.raises(ValueError, match="Unsupported trend source"):
        research_trends(sources=["tiktok"])

    # Direct feed helper checks
    assert len(fetch_autocomplete("how does ", youtube=True)) == 2
    assert len(fetch_autocomplete("how does ", youtube=False)) == 2
    hn_items = fetch_hackernews_top(limit=5)
    assert len(hn_items) == 2
    assert hn_items[1].url == "https://news.ycombinator.com/item?id=999"
    assert len(fetch_reddit_eli5(limit=5, topic="sql")) == 1

    # Malformed autocomplete response
    monkeypatch.setattr("cm.research.http_get_json", lambda *_a, **_kw: {"unexpected": True})
    assert fetch_autocomplete("x") == []
    monkeypatch.setattr("cm.research.http_get_json", _fake_trends_json)

    # Full research_trends with topic, dedup, and partial Reddit failure warning
    snap = research_trends(
        seeds=["how does ", "how does ", "failseed"],
        sources=["youtube", "google", "hn", "reddit"],
        limit=5,
        topic=None,
        save=True,
    )
    assert any("youtube (failseed)" in w for w in snap.warnings)
    assert any("reddit:" in w for w in snap.warnings)
    assert any(t.source == "hackernews" for t in snap.trends)

    # Topic mode derives seeds & searches ELI5 cleanly
    snap_topic = research_trends(sources=["youtube", "reddit"], topic="sql", limit=5, save=False)
    assert snap_topic.queries == ["how does sql", "why does sql", "sql"]
    assert any(t.title == "How do SQL indexes work?" for t in snap_topic.trends)

    # Default seeds path
    snap_default = research_trends(sources=["hn"], save=False)
    assert len(snap_default.queries) == 4

    # Playbook load / merge / insights / corruption
    pb = load_research_playbook()
    assert len(pb.trends) >= 5

    # Duplicate video merge updates rather than duplicating
    v_test = CompetitorVideo(
        video_id="v1",
        url="https://www.youtube.com/shorts/v1",
        title="Why Indexes Fail",
        channel_id="c1",
        channel_title="Dev",
        published_at=datetime.now(UTC),
        duration_sec=44,
        view_count=50000,
        subscriber_count=5000,
        outlier_score=10.0,
        views_per_day=25000.0,
        title_pattern="why",
    )
    merge_snapshot_into_playbook(ResearchSnapshot(kind="youtube", videos=[v_test]))
    pb2 = add_playbook_insight("Open with benchmark failure inside first 2.5 seconds.")
    add_playbook_insight("Open with benchmark failure inside first 2.5 seconds.")
    assert len(pb2.learned_insights) == 1

    with pytest.raises(ValueError, match="blank"):
        add_playbook_insight("   ")

    # CLI commands: trends, show, insight, schema
    res_trends = runner.invoke(
        app, ["research", "trends", "--sources", "youtube,hn,reddit", "--limit", "3"]
    )
    assert res_trends.exit_code == 0, res_trends.output
    res_trends.output.encode("cp1252")
    assert "Trend Signals" in res_trends.output

    res_trends_json = runner.invoke(
        app, ["research", "trends", "--sources", "hn", "--json", "--no-save"]
    )
    assert res_trends_json.exit_code == 0
    assert ResearchSnapshot.model_validate_json(res_trends_json.output).kind == "trends"

    res_trends_bad = runner.invoke(app, ["research", "trends", "--sources", "bogus"])
    assert res_trends_bad.exit_code == 1
    assert "[FAIL]" in res_trends_bad.output

    res_show = runner.invoke(app, ["research", "show", "--top", "5"])
    assert res_show.exit_code == 0, res_show.output
    res_show.output.encode("cp1252")
    assert "Research Playbook" in res_show.output
    assert "Learned Insights:" in res_show.output

    res_show_json = runner.invoke(app, ["research", "show", "--json"])
    assert res_show_json.exit_code == 0
    assert ResearchPlaybook.model_validate_json(res_show_json.output).videos[0].video_id == "v1"

    res_insight = runner.invoke(app, ["research", "insight", "Keep hooks under 3.5 seconds"])
    assert res_insight.exit_code == 0, res_insight.output
    assert "[OK]" in res_insight.output

    res_insight_json = runner.invoke(app, ["research", "insight", "End with crisp payoff", "--json"])
    assert res_insight_json.exit_code == 0
    assert len(ResearchPlaybook.model_validate_json(res_insight_json.output).learned_insights) == 3

    res_insight_bad = runner.invoke(app, ["research", "insight", "   ", "--json"])
    assert res_insight_bad.exit_code == 1

    res_schema = runner.invoke(app, ["schema", "research"])
    assert res_schema.exit_code == 0
    schema_obj = json.loads(res_schema.output)
    assert schema_obj["title"] == "ResearchPlaybook"

    # Corrupt playbook file handling (direct & CLI)
    research_path().write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt research playbook"):
        load_research_playbook()
    res_show_corrupt = runner.invoke(app, ["research", "show", "--json"])
    assert res_show_corrupt.exit_code == 1
