"""Offline exhaustive tests for Milestone 8 publishing, OAuth PKCE, R2 staging, and analytics."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cm.analytics import _to_int, sync_channel_analytics
from cm.catalog import add_idea, load_catalog, record_published_entry
from cm.cli import app
from cm.models import (
    AnalyticsSyncReport,
    FormatPreset,
    IdeaStatus,
    PipelineStage,
    PublishPlatform,
    PublishReceipt,
    StageStatus,
    TopicDomain,
    YouTubePrivacy,
)
from cm.paths import atomic_write_text, run_dir
from cm.publish import (
    authenticate_youtube,
    build_publish_metadata,
    get_youtube_access_token,
    http_json,
    http_request,
    load_youtube_client_config,
    publish_run,
    redact_secrets,
    resolve_youtube_secret_paths,
    stage_mp4_for_instagram,
    upload_instagram_reel,
    upload_youtube_video,
)
from cm.state import init_run, load_state, update_stage
from cm.tts import TtsProvider, synthesize_run_tts

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_BP = REPO_ROOT / "examples" / "password_system.blueprint.json"


def _make_valid_mp4(out_mp4: Path) -> Path:
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10:d=0.4",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t",
            "0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(out_mp4),
        ],
        capture_output=True,
        check=True,
    )
    return out_mp4


def test_secret_redaction_and_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    masked = redact_secrets(
        'https://x.test?key=KEY123&access_token=TOK999 {"refresh_token": "REF77", "client_secret": "SEC"}'
    )
    assert "KEY123" not in masked
    assert "TOK999" not in masked
    assert "REF77" not in masked
    assert "SEC" not in masked

    class _Resp:
        def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.status = status
            self.headers = headers
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    def _fake_open(req: urllib.request.Request, timeout: float = 0.0) -> _Resp:
        _ = timeout
        if "bad-json" in req.full_url:
            return _Resp(200, {}, b"{not-json")
        if "err-body" in req.full_url:
            raise urllib.error.HTTPError(
                req.full_url,
                401,
                "Unauthorized",
                Message(),
                io.BytesIO(b"bad access_token=LEAKTOKEN"),
            )
        if "err-unread" in req.full_url:
            err = urllib.error.HTTPError(req.full_url, 502, "BadGateway", Message(), None)
            err.read = lambda: (_ for _ in ()).throw(OSError("boom"))  # type: ignore[method-assign]
            raise err
        if "net-down" in req.full_url:
            raise urllib.error.URLError("offline")
        return _Resp(200, {"Location": "https://upload.example/resumable"}, b'{"ok": 1}')

    monkeypatch.setattr(urllib.request, "urlopen", _fake_open)

    code, hdrs, raw = http_request("https://api.example/x", headers={"X-Custom": "1"})
    assert code == 200
    assert hdrs["location"] == "https://upload.example/resumable"
    assert raw == b'{"ok": 1}'
    assert http_json("https://api.example/x") == {"ok": 1}

    with pytest.raises(RuntimeError, match="Invalid JSON"):
        http_json("https://api.example/bad-json")
    with pytest.raises(RuntimeError, match=r"HTTP 401 .*access_token=\*\*\*") as exc_http:
        http_request("https://api.example/err-body?key=MYKEY")
    assert "LEAKTOKEN" not in str(exc_http.value)
    assert "MYKEY" not in str(exc_http.value)
    with pytest.raises(RuntimeError, match="HTTP 502"):
        http_request("https://api.example/err-unread")
    with pytest.raises(RuntimeError, match="Request failed"):
        http_request("https://api.example/net-down")


def test_youtube_oauth_pkce_flow_and_token_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    client_path, token_path = resolve_youtube_secret_paths()
    assert client_path == (tmp_path / "secrets" / "youtube_client_secret.json").resolve()
    assert token_path == (tmp_path / "secrets" / "youtube_token.json").resolve()

    # Client config error cases & 'web' support
    with pytest.raises(FileNotFoundError, match="not found"):
        load_youtube_client_config(client_path)

    client_path.parent.mkdir(parents=True, exist_ok=True)
    client_path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt YouTube client secrets"):
        load_youtube_client_config(client_path)

    client_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="installed"):
        load_youtube_client_config(client_path)

    client_path.write_text(json.dumps({"web": {"client_id": "id1", "client_secret": ""}}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing client_id"):
        load_youtube_client_config(client_path)

    client_path.write_text(
        json.dumps({"installed": {"client_id": "cid-123", "client_secret": "csec-456"}}),
        encoding="utf-8",
    )
    cfg = load_youtube_client_config(client_path)
    assert cfg["client_id"] == "cid-123"

    # Full live loopback PKCE exchange using webbrowser.open hook (zero sleep)
    token_calls: list[dict[str, str]] = []

    def _fake_http_json(
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> object:
        _ = (url, method, headers, timeout)
        parsed_form = dict(urllib.parse.parse_qsl((data or b"").decode("utf-8")))
        token_calls.append(parsed_form)
        if parsed_form.get("code") == "NON_DICT":
            return ["invalid"]
        if parsed_form.get("code") == "NO_TOKEN":
            return {"expires_in": 100}
        if parsed_form.get("grant_type") == "refresh_token":
            return {"access_token": "fresh-tok", "expires_in": 3600}
        return {"access_token": "init-tok", "refresh_token": "ref-tok", "expires_in": 3600}

    monkeypatch.setattr("cm.publish.http_json", _fake_http_json)

    def _trigger_callback(callback_query_override: str | None = None):
        def _fake_open(auth_url: str) -> bool:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
            assert q["code_challenge_method"] == ["S256"]
            redir = q["redirect_uri"][0]
            state = q["state"][0]
            query = (
                callback_query_override.format(state=state)
                if callback_query_override
                else urllib.parse.urlencode({"code": "AUTH_OK", "state": state})
            )

            def _hit() -> None:
                with contextlib.suppress(urllib.error.HTTPError):
                    urllib.request.urlopen(f"{redir}?{query}", timeout=5)

            threading.Thread(target=_hit, daemon=True).start()
            return True

        return _fake_open

    monkeypatch.setattr("cm.publish.webbrowser.open", _trigger_callback())
    summary = authenticate_youtube(open_browser=True, timeout_sec=5.0)
    assert summary["has_refresh_token"] is True
    assert token_path.is_file()
    saved_tok = json.loads(token_path.read_text(encoding="utf-8"))
    assert saved_tok["access_token"] == "init-tok"
    assert "client_secret" not in saved_tok

    # Cached valid token returns directly without network refresh
    assert get_youtube_access_token() == "init-tok"

    # Expired token triggers refresh cleanly & updates token_path
    saved_tok["expires_at"] = "invalid-iso-date"
    token_path.write_text(json.dumps(saved_tok), encoding="utf-8")
    assert get_youtube_access_token() == "fresh-tok"
    assert json.loads(token_path.read_text(encoding="utf-8"))["access_token"] == "fresh-tok"

    # Callback failure cases: declined, state mismatch, missing code, timeout, non-dict exchange
    monkeypatch.setattr("cm.publish.webbrowser.open", _trigger_callback("error=access_denied&state={state}"))
    with pytest.raises(RuntimeError, match="access_denied"):
        authenticate_youtube(open_browser=True, timeout_sec=5.0)

    monkeypatch.setattr("cm.publish.webbrowser.open", _trigger_callback("code=ABC&state=WRONG"))
    with pytest.raises(RuntimeError, match="state mismatch"):
        authenticate_youtube(open_browser=True, timeout_sec=5.0)

    monkeypatch.setattr("cm.publish.webbrowser.open", _trigger_callback("state={state}"))
    with pytest.raises(RuntimeError, match="missing authorization code"):
        authenticate_youtube(open_browser=True, timeout_sec=5.0)

    monkeypatch.setattr("cm.publish.webbrowser.open", _trigger_callback("code=NON_DICT&state={state}"))
    with pytest.raises(RuntimeError, match="non-object"):
        authenticate_youtube(open_browser=True, timeout_sec=5.0)

    monkeypatch.setattr("cm.publish.webbrowser.open", _trigger_callback("code=NO_TOKEN&state={state}"))
    with pytest.raises(RuntimeError, match="missing access_token"):
        authenticate_youtube(open_browser=True, timeout_sec=5.0)

    with pytest.raises(TimeoutError, match="Timed out"):
        authenticate_youtube(open_browser=False, timeout_sec=0.05)

    # Refresh token error branches
    saved_tok["expires_at"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    saved_tok["refresh_token"] = ""
    token_path.write_text(json.dumps(saved_tok), encoding="utf-8")
    with pytest.raises(RuntimeError, match="no refresh_token"):
        get_youtube_access_token()

    saved_tok["refresh_token"] = "ref-tok"
    token_path.write_text(json.dumps(saved_tok), encoding="utf-8")
    monkeypatch.setattr("cm.publish.http_json", lambda *_a, **_kw: ["bad"])
    with pytest.raises(RuntimeError, match="refresh returned non-object"):
        get_youtube_access_token()

    token_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt YouTube token"):
        get_youtube_access_token()

    token_path.unlink()
    with pytest.raises(FileNotFoundError, match="cm auth youtube"):
        get_youtube_access_token()


def test_metadata_staging_sigv4_and_uploaders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))
    init_run("meta-run", from_blueprint=EXAMPLE_BP)
    from cm.models import Blueprint

    bp = Blueprint.model_validate_json((run_dir("meta-run") / "blueprint.json").read_text(encoding="utf-8"))
    t, desc, tags = build_publish_metadata(bp, extra_tags=["#Security", "shorts", "auth"])
    assert len(t) <= 100
    assert "shorts" in tags and "#shorts" in desc.lower()
    with pytest.raises(ValueError, match="blank"):
        build_publish_metadata(bp, title_override="   ")
    with pytest.raises(ValueError, match="blank"):
        build_publish_metadata(bp, description_override="   ")

    # Long format omits shorts tag
    bp_long = bp.model_copy(update={"format": FormatPreset.LONG_16X9})
    _, _, long_tags = build_publish_metadata(bp_long)
    assert "shorts" not in long_tags

    mp4 = _make_valid_mp4(tmp_path / "sample.mp4")

    # Staging modes
    assert stage_mp4_for_instagram("meta-run", mp4, video_url_override=" https://cdn.test/v.mp4 ") == "https://cdn.test/v.mp4"

    monkeypatch.setenv("IG_STAGING_PROVIDER", "none")
    with pytest.raises(ValueError, match="public MP4 URL"):
        stage_mp4_for_instagram("meta-run", mp4)

    monkeypatch.setenv("IG_STAGING_PROVIDER", "public_url")
    monkeypatch.delenv("IG_STAGING_PUBLIC_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="IG_STAGING_PUBLIC_BASE_URL"):
        stage_mp4_for_instagram("meta-run", mp4)
    monkeypatch.setenv("IG_STAGING_PUBLIC_BASE_URL", "https://cdn.example.com/")
    assert stage_mp4_for_instagram("meta-run", mp4) == "https://cdn.example.com/meta-run.mp4"

    monkeypatch.setenv("IG_STAGING_PROVIDER", "ftp")
    with pytest.raises(ValueError, match="Unsupported IG_STAGING_PROVIDER"):
        stage_mp4_for_instagram("meta-run", mp4)

    monkeypatch.setenv("IG_STAGING_PROVIDER", "r2")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    with pytest.raises(ValueError, match="S3_ENDPOINT_URL"):
        stage_mp4_for_instagram("meta-run", mp4)

    # S3/R2 SigV4 PUT checks signed Authorization header and invalid endpoint
    put_calls: list[tuple[str, dict[str, str]]] = []

    def _fake_http_req(
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, dict[str, str], bytes]:
        _ = (data, timeout)
        put_calls.append((url, dict(headers or {})))
        if "no-loc" in (headers or {}).get("X-Test-Mode", ""):
            return 200, {}, b""
        return 200, {"location": "https://upload.yt/session/1"}, b"{}"

    monkeypatch.setattr("cm.publish.http_request", _fake_http_req)
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://r2.example.com")
    monkeypatch.setenv("S3_BUCKET", "cm-bucket")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "ak123")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "sk456")
    staged = stage_mp4_for_instagram("meta-run", mp4)
    assert staged == "https://cdn.example.com/cm/meta-run/sample.mp4"
    assert put_calls[0][1]["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=ak123/")
    assert "x-amz-content-sha256" in put_calls[0][1]

    monkeypatch.setenv("S3_ENDPOINT_URL", "not-a-url")
    with pytest.raises(ValueError, match="Invalid S3/R2 endpoint"):
        stage_mp4_for_instagram("meta-run", mp4)

    # YouTube uploader success & error paths
    monkeypatch.setattr("cm.publish.http_json", lambda *_a, **_kw: {"id": "ytVid1"})
    res_short = upload_youtube_video(mp4, t, desc, tags, YouTubePrivacy.UNLISTED, True, "tok")
    assert res_short.url == "https://www.youtube.com/shorts/ytVid1"
    res_long = upload_youtube_video(mp4, t, desc, tags, YouTubePrivacy.PUBLIC, False, "tok")
    assert res_long.url == "https://www.youtube.com/watch?v=ytVid1"

    monkeypatch.setattr("cm.publish.http_json", lambda *_a, **_kw: {})
    with pytest.raises(RuntimeError, match="missing video 'id'"):
        upload_youtube_video(mp4, t, desc, tags, YouTubePrivacy.PRIVATE, True, "tok")

    monkeypatch.setattr("cm.publish.http_request", lambda *_a, **_kw: (200, {}, b""))
    with pytest.raises(RuntimeError, match="Location header"):
        upload_youtube_video(mp4, t, desc, tags, YouTubePrivacy.PRIVATE, True, "tok")

    # Instagram uploader polling + permalink + failure modes
    ig_poll_count = 0

    def _fake_ig_json(
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> object:
        nonlocal ig_poll_count
        _ = (method, data, headers, timeout)
        if url.endswith("/media"):
            return {"id": "cont-1"}
        if "fields=status_code" in url:
            ig_poll_count += 1
            return {"status_code": "IN_PROGRESS" if ig_poll_count == 1 else "FINISHED"}
        if url.endswith("/media_publish"):
            return {"id": "ig-999"}
        if "fields=permalink" in url:
            return {"permalink": "https://www.instagram.com/reel/XYZ/"}
        raise AssertionError(url)

    monkeypatch.setattr("cm.publish.http_json", _fake_ig_json)
    ig_res = upload_instagram_reel(
        "https://cdn.example.com/v.mp4", "cap", "ig123", "igt", poll_interval_sec=0.0, max_polls=3
    )
    assert ig_res.video_id == "ig-999"
    assert ig_res.url == "https://www.instagram.com/reel/XYZ/"

    # Permalink HTTP error falls back cleanly to url=None
    def _fake_ig_no_permalink(url: str, **_kw: object) -> object:
        if "fields=permalink" in url:
            raise RuntimeError("permalink permission denied")
        return _fake_ig_json(url)

    monkeypatch.setattr("cm.publish.http_json", _fake_ig_no_permalink)
    ig_res2 = upload_instagram_reel(
        "https://cdn.example.com/v.mp4", "cap", "ig123", "igt", poll_interval_sec=0.0, max_polls=3
    )
    assert ig_res2.url is None

    # Instagram error branches
    monkeypatch.setattr("cm.publish.http_json", lambda *_a, **_kw: {})
    with pytest.raises(RuntimeError, match="container creation"):
        upload_instagram_reel("u", "c", "id", "tok")

    monkeypatch.setattr(
        "cm.publish.http_json",
        lambda u, **_k: {"id": "c1"} if u.endswith("/media") else {"status_code": "ERROR"},
    )
    with pytest.raises(RuntimeError, match="status 'ERROR'"):
        upload_instagram_reel("u", "c", "id", "tok", poll_interval_sec=0.0, max_polls=2)

    monkeypatch.setattr(
        "cm.publish.http_json",
        lambda u, **_k: {"id": "c1"} if u.endswith("/media") else {"status_code": "IN_PROGRESS"},
    )
    with pytest.raises(RuntimeError, match="Timed out"):
        upload_instagram_reel("u", "c", "id", "tok", poll_interval_sec=0.0, max_polls=2)

    monkeypatch.setattr(
        "cm.publish.http_json",
        lambda u, **_k: (
            {"id": "c1"}
            if u.endswith("/media")
            else ({"status_code": "FINISHED"} if "status_code" in u else {})
        ),
    )
    with pytest.raises(RuntimeError, match="media_publish"):
        upload_instagram_reel("u", "c", "id", "tok", poll_interval_sec=0.0, max_polls=2)


def test_publish_run_dry_run_live_analytics_sync_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("IG_ACCESS_TOKEN", raising=False)

    # Add an idea with matching slug to verify auto-PUBLISHED status transition
    idea, _ = add_idea(
        title="Password Storage",
        topic_domain=TopicDomain.SECURITY,
        hook="How do hackers crack passwords?",
        failure_chain=["Plaintext -> breach", "SHA256 -> rainbow", "Salted -> GPU"],
        slug="pub-short",
    )
    init_run("pub-short", from_blueprint=EXAMPLE_BP, idea=idea)

    # Missing final.mp4 raises clear error
    with pytest.raises(FileNotFoundError, match="Rendered video not found"):
        publish_run("pub-short", dry_run=True)

    _make_valid_mp4(run_dir("pub-short") / "final.mp4")

    # Mock TTS audio triggers guard on live publish unless allow_mock_audio=True
    synthesize_run_tts("pub-short", provider=TtsProvider.MOCK)
    update_stage("pub-short", PipelineStage.SCENE, StageStatus.PASSED)
    update_stage("pub-short", PipelineStage.QA, StageStatus.PASSED)
    update_stage("pub-short", PipelineStage.RENDER, StageStatus.PASSED)

    with pytest.raises(ValueError, match="mock TTS audio"):
        publish_run("pub-short", dry_run=False)

    # Dry-run succeeds even with mock TTS & leaves catalog/state untouched
    dry_receipt = publish_run(
        "pub-short",
        platforms=[PublishPlatform.YOUTUBE, PublishPlatform.INSTAGRAM],
        video_url="https://cdn.example/dry.mp4",
        dry_run=True,
    )
    assert dry_receipt.dry_run is True
    assert len(dry_receipt.results) == 2
    assert load_catalog().published == []
    assert load_state("pub-short").stages[PipelineStage.PUBLISH].status == StageStatus.PENDING

    # Live CLI dry-run + schema commands
    res_dry_cli = runner.invoke(app, ["publish", "pub-short", "--dry-run"])
    assert res_dry_cli.exit_code == 0, res_dry_cli.output
    res_dry_cli.output.encode("cp1252")
    assert "Publish (DRY-RUN)" in res_dry_cli.output

    res_pub_schema = runner.invoke(app, ["schema", "publish"])
    assert res_pub_schema.exit_code == 0
    assert json.loads(res_pub_schema.output)["title"] == "PublishReceipt"

    res_ana_schema = runner.invoke(app, ["schema", "analytics"])
    assert res_ana_schema.exit_code == 0
    assert json.loads(res_ana_schema.output)["title"] == "AnalyticsSyncReport"

    # Empty analytics sync
    assert sync_channel_analytics().videos_checked == 0

    # Live publish where YouTube succeeds and Instagram fails (missing creds) -> partial success records YT
    monkeypatch.setattr("cm.publish.get_youtube_access_token", lambda root=None: "yt-tok")
    monkeypatch.setattr(
        "cm.publish.upload_youtube_video",
        lambda **_kw: PublishReceipt.model_validate(
            {
                "slug": "pub-short",
                "title": "t",
                "description": "d",
                "video_path": "p",
                "results": [
                    {
                        "platform": "youtube",
                        "status": "published",
                        "video_id": "YT_ABC123",
                        "url": "https://www.youtube.com/shorts/YT_ABC123",
                        "privacy": "private",
                    }
                ],
            }
        ).results[0],
    )
    live_res = runner.invoke(
        app,
        [
            "publish",
            "pub-short",
            "-p",
            "youtube",
            "-p",
            "instagram",
            "--allow-mock-audio",
            "--json",
        ],
    )
    assert live_res.exit_code == 0, live_res.output
    live_receipt = PublishReceipt.model_validate_json(live_res.output)
    assert live_receipt.results[0].status == "published"
    assert live_receipt.results[1].status == "failed"

    assert load_state("pub-short").stages[PipelineStage.PUBLISH].status == StageStatus.PASSED
    cat = load_catalog()
    assert len(cat.published) == 1
    assert cat.published[0].platforms["youtube"]["video_id"] == "YT_ABC123"
    assert cat.ideas[0].status == IdeaStatus.PUBLISHED

    # Upsert on existing slug + Instagram live path via CLI
    monkeypatch.setenv("IG_USER_ID", "ig-u1")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "ig-tok-secret")
    monkeypatch.setattr(
        "cm.publish.upload_instagram_reel",
        lambda **_kw: PublishReceipt.model_validate(
            {
                "slug": "pub-short",
                "title": "t",
                "description": "d",
                "video_path": "p",
                "results": [
                    {
                        "platform": "instagram",
                        "status": "published",
                        "video_id": "IG_MEDIA_1",
                        "url": "https://www.instagram.com/reel/1/",
                    }
                ],
            }
        ).results[0],
    )
    res_ig_cli = runner.invoke(
        app,
        [
            "publish",
            "pub-short",
            "-p",
            "instagram",
            "--video-url",
            "https://cdn.example/final.mp4",
            "--allow-mock-audio",
        ],
    )
    assert res_ig_cli.exit_code == 0, res_ig_cli.output
    res_ig_cli.output.encode("cp1252")

    # Add second published entry via record_published_entry to verify upsert + keyword dedup
    record_published_entry(
        slug="pub-short",
        title="Password Storage Updated",
        topic_domain=TopicDomain.SECURITY,
        fmt=FormatPreset.SHORT_9X16,
        platforms={"youtube": {"video_id": "YT_ABC123", "status": "published"}},
        keywords=["shorts", "hashing"],
    )
    assert len(load_catalog().published) == 1
    assert "hashing" in load_catalog().published[0].keywords

    # All-failed live publish returns exit code 1
    monkeypatch.delenv("IG_USER_ID", raising=False)
    monkeypatch.delenv("IG_ACCESS_TOKEN", raising=False)
    res_all_fail = runner.invoke(
        app, ["publish", "pub-short", "-p", "instagram", "--allow-mock-audio", "--json"]
    )
    assert res_all_fail.exit_code == 1
    assert load_state("pub-short").stages[PipelineStage.PUBLISH].status == StageStatus.FAILED

    # Analytics sync: first test missing YT/IG credentials produce warnings without crashing
    monkeypatch.setattr(
        "cm.analytics.get_youtube_access_token",
        lambda root=None: (_ for _ in ()).throw(FileNotFoundError("no token")),
    )
    rep_warn = sync_channel_analytics()
    assert len(rep_warn.warnings) == 2

    # Analytics sync with YOUTUBE_API_KEY + IG_ACCESS_TOKEN + malformed items/stats
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-key-1")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "ig-tok-1")

    def _fake_analytics_json(url: str, headers: dict[str, str] | None = None) -> object:
        _ = headers
        if "googleapis.com/youtube/v3/videos" in url:
            return {
                "items": [
                    "bad",
                    {"id": "unknown-id", "statistics": {}},
                    {
                        "id": "YT_ABC123",
                        "statistics": {"viewCount": "4500", "likeCount": "310", "commentCount": "bad"},
                    },
                ]
            }
        if "graph.facebook.com" in url:
            return {"views": 1200, "like_count": 95, "comments_count": 14}
        raise AssertionError(url)

    monkeypatch.setattr("cm.analytics.http_json", _fake_analytics_json)
    assert _to_int("not-int") == 0

    res_sync_cli = runner.invoke(app, ["analytics", "sync"])
    assert res_sync_cli.exit_code == 0, res_sync_cli.output
    res_sync_cli.output.encode("cp1252")
    assert "YouTube updated: 1, Instagram updated: 1" in " ".join(res_sync_cli.output.split())

    res_sync_json = runner.invoke(app, ["analytics", "sync", "--json"])
    assert res_sync_json.exit_code == 0
    sync_rep = AnalyticsSyncReport.model_validate_json(res_sync_json.output)
    assert sync_rep.youtube_updated == 1 and sync_rep.instagram_updated == 1

    cat_after = load_catalog()
    assert cat_after.published[0].analytics["youtube"].views == 4500
    assert cat_after.published[0].analytics["youtube"].comments == 0
    assert cat_after.published[0].analytics["instagram"].views == 1200

    # Analytics sync via OAuth Bearer fallback + HTTP failure warning paths
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr("cm.analytics.get_youtube_access_token", lambda root=None: "oauth-tok")
    monkeypatch.setattr(
        "cm.analytics.http_json",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("upstream 500")),
    )
    rep_err = sync_channel_analytics()
    assert len(rep_err.warnings) == 2
    res_warn_cli = runner.invoke(app, ["analytics", "sync"])
    assert res_warn_cli.exit_code == 0
    assert "[WARN]" in res_warn_cli.output

    # CLI auth youtube & error paths
    monkeypatch.setattr(
        "cm.cli.authenticate_youtube",
        lambda **_kw: {
            "token_path": str(tmp_path / "secrets" / "youtube_token.json"),
            "has_refresh_token": True,
            "scopes": [],
            "expires_at": "2030-01-01T00:00:00Z",
            "auth_url": "https://accounts.google.com/...",
        },
    )
    assert runner.invoke(app, ["auth", "youtube", "--no-browser"]).exit_code == 0
    assert runner.invoke(app, ["auth", "youtube", "--json"]).exit_code == 0

    monkeypatch.setattr(
        "cm.cli.authenticate_youtube",
        lambda **_kw: (_ for _ in ()).throw(FileNotFoundError("missing secret json")),
    )
    assert runner.invoke(app, ["auth", "youtube", "--json"]).exit_code == 1
    assert runner.invoke(app, ["publish", "no-such-slug", "--dry-run"]).exit_code == 1
    monkeypatch.setattr(
        "cm.cli.sync_channel_analytics",
        lambda **_kw: (_ for _ in ()).throw(ValueError("corrupt catalog")),
    )
    assert runner.invoke(app, ["analytics", "sync", "--json"]).exit_code == 1

    # Helper atomic write check inside run_dir
    assert atomic_write_text(run_dir("pub-short") / ".check", "ok").is_file()
