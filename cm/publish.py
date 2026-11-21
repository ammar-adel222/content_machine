"""YouTube OAuth 2.0 PKCE auth, S3/R2 MP4 staging, and Shorts/Reels auto-publishing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cm.catalog import record_published_entry
from cm.models import (
    AudioTimeline,
    Blueprint,
    FormatPreset,
    PipelineStage,
    PlatformPublishResult,
    PublishPlatform,
    PublishReceipt,
    StageStatus,
    YouTubePrivacy,
)
from cm.paths import atomic_write_text, get_repo_root, run_dir
from cm.renderer import probe_media
from cm.state import load_state, update_stage
from cm.tts import load_env

YOUTUBE_OAUTH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
_USER_AGENT = "content-machine/3.0 (publish-cli)"


def redact_secrets(text: str) -> str:
    """Redact API keys, OAuth tokens, and client secrets from diagnostic text."""
    redacted = re.sub(
        r"([?&]?(?:key|access_token|client_secret|refresh_token)=)[^&\s\"']+",
        r"\1***",
        text,
    )
    return re.sub(
        r'("(?:access_token|refresh_token|client_secret)"\s*:\s*")[^"]*"',
        r'\1***"',
        redacted,
    )


def http_request(
    url: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, str], bytes]:
    """Perform an HTTP request via stdlib urllib with safe secret redaction."""
    req_headers = {"User-Agent": _USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = int(getattr(resp, "status", 200))
            resp_headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
            return status_code, resp_headers, bytes(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = redact_secrets(bytes(exc.read()).decode("utf-8", errors="replace")[:400].strip())
        except Exception:
            detail = ""
        msg = detail or str(exc.reason)
        raise RuntimeError(f"HTTP {exc.code} {method} {redact_secrets(url)}: {msg}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"Request failed {method} {redact_secrets(url)}: {redact_secrets(str(exc))}"
        ) from exc


def http_json(
    url: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> object:
    """Execute an HTTP request and parse its UTF-8 JSON body."""
    _, _, raw_bytes = http_request(
        url, method=method, data=data, headers=headers, timeout=timeout
    )
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {redact_secrets(url)}: {exc}") from exc


def resolve_youtube_secret_paths(root: Path | None = None) -> tuple[Path, Path]:
    """Resolve client-secret and OAuth-token paths under root."""
    base = root or get_repo_root()
    raw_client = os.environ.get(
        "YOUTUBE_CLIENT_SECRETS_FILE", "secrets/youtube_client_secret.json"
    ).strip()
    raw_token = os.environ.get("YOUTUBE_TOKEN_FILE", "secrets/youtube_token.json").strip()
    client_path = Path(raw_client) if Path(raw_client).is_absolute() else (base / raw_client)
    token_path = Path(raw_token) if Path(raw_token).is_absolute() else (base / raw_token)
    return client_path.resolve(), token_path.resolve()


def load_youtube_client_config(client_secrets_path: Path) -> dict[str, str]:
    """Load and validate an OAuth 2.0 client JSON file ('installed' or 'web')."""
    if not client_secrets_path.is_file():
        raise FileNotFoundError(
            f"YouTube OAuth client secrets file not found: {client_secrets_path}"
        )
    try:
        raw = json.loads(client_secrets_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Corrupt YouTube client secrets JSON: {exc}") from exc

    block = raw.get("installed") or raw.get("web") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        raise ValueError(
            f"Client secrets JSON at {client_secrets_path} must contain 'installed' or 'web' key"
        )
    client_id = str(block.get("client_id", "")).strip()
    client_secret = str(block.get("client_secret", "")).strip()
    if not client_id or not client_secret:
        raise ValueError(f"Client secrets JSON at {client_secrets_path} missing client_id/secret")

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": str(block.get("auth_uri") or "https://accounts.google.com/o/oauth2/v2/auth"),
        "token_uri": str(block.get("token_uri") or "https://oauth2.googleapis.com/token"),
    }


def _save_youtube_token(
    token_path: Path,
    payload: dict[str, object],
    client_cfg: dict[str, str],
    prior_refresh_token: str | None = None,
) -> dict[str, object]:
    access_token = str(payload.get("access_token", "")).strip()
    if not access_token:
        raise RuntimeError("Google OAuth token response missing access_token")
    refresh_token = str(payload.get("refresh_token") or prior_refresh_token or "").strip()
    expires_in = max(60, int(payload.get("expires_in", 3600)))  # type: ignore[arg-type]
    expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
    scopes_raw = str(payload.get("scope") or " ".join(YOUTUBE_OAUTH_SCOPES))

    record: dict[str, object] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_uri": client_cfg["token_uri"],
        "client_id": client_cfg["client_id"],
        "scopes": [s for s in scopes_raw.split() if s],
        "expires_at": expires_at,
    }
    atomic_write_text(token_path, json.dumps(record, indent=2) + "\n")
    return record


def authenticate_youtube(
    root: Path | None = None,
    open_browser: bool = True,
    timeout_sec: float = 180.0,
    port: int = 0,
) -> dict[str, object]:
    """Run local loopback OAuth 2.0 PKCE authorization and save YOUTUBE_TOKEN_FILE."""
    load_env(root)
    client_path, token_path = resolve_youtube_secret_paths(root)
    client_cfg = load_youtube_client_config(client_path)

    code_verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    expected_state = secrets.token_urlsafe(24)

    done_event = threading.Event()
    captured: dict[str, str | None] = {"code": None, "state": None, "error": None}

    class _OAuthCallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            _ = (format, args)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            captured["code"] = (qs.get("code") or [None])[0]
            captured["state"] = (qs.get("state") or [None])[0]
            captured["error"] = (qs.get("error") or [None])[0]

            ok = bool(captured["code"]) and not captured["error"]
            msg = (
                "Authorization received! You may close this browser tab."
                if ok
                else f"Authorization failed: {html.escape(str(captured['error'] or 'missing code'))}"
            )
            body = f"<html><body style='font-family:sans-serif;padding:24px'><h2>{msg}</h2></body></html>"
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            done_event.set()

    server = HTTPServer(("127.0.0.1", port), _OAuthCallbackHandler)
    actual_port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{actual_port}/callback"
    auth_url = (
        client_cfg["auth_uri"]
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client_cfg["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(YOUTUBE_OAUTH_SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "state": expected_state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
    )

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if open_browser:  # pragma: no cover
            webbrowser.open(auth_url)
        received = done_event.wait(timeout=timeout_sec)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    if not received:
        raise TimeoutError(f"Timed out waiting for Google OAuth callback after {timeout_sec:.0f}s")
    if captured["error"]:
        raise RuntimeError(f"Google OAuth authorization error: {captured['error']}")
    if captured["state"] != expected_state:
        raise RuntimeError("OAuth state mismatch while validating loopback callback")
    if not captured["code"]:
        raise RuntimeError("OAuth callback missing authorization code")

    form = urllib.parse.urlencode(
        {
            "client_id": client_cfg["client_id"],
            "client_secret": client_cfg["client_secret"],
            "code": captured["code"],
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    token_resp = http_json(
        client_cfg["token_uri"],
        method="POST",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not isinstance(token_resp, dict):
        raise RuntimeError("Google OAuth token exchange returned non-object JSON")

    saved = _save_youtube_token(token_path, token_resp, client_cfg)
    return {
        "token_path": str(token_path),
        "scopes": saved["scopes"],
        "has_refresh_token": bool(saved["refresh_token"]),
        "expires_at": saved["expires_at"],
        "auth_url": auth_url,
    }


def get_youtube_access_token(root: Path | None = None) -> str:
    """Return a valid YouTube OAuth access token, refreshing transparently when expired."""
    load_env(root)
    client_path, token_path = resolve_youtube_secret_paths(root)
    if not token_path.is_file():
        raise FileNotFoundError(
            f"YouTube OAuth token not found at {token_path} (run: cm auth youtube)"
        )
    try:
        token_data = json.loads(token_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Corrupt YouTube token JSON at {token_path}: {exc}") from exc

    access_token = str(token_data.get("access_token", "")).strip()
    expires_raw = str(token_data.get("expires_at", "")).strip()
    if access_token and expires_raw:
        try:
            expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            if expires_at > datetime.now(UTC) + timedelta(seconds=60):
                return access_token
        except ValueError:
            pass

    refresh_token = str(token_data.get("refresh_token", "")).strip()
    if not refresh_token:
        raise RuntimeError(
            f"YouTube token at {token_path} is expired and has no refresh_token (re-run: cm auth youtube)"
        )

    client_cfg = load_youtube_client_config(client_path)
    form = urllib.parse.urlencode(
        {
            "client_id": client_cfg["client_id"],
            "client_secret": client_cfg["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    refreshed = http_json(
        client_cfg["token_uri"],
        method="POST",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not isinstance(refreshed, dict):
        raise RuntimeError("Google OAuth refresh returned non-object JSON")
    updated = _save_youtube_token(
        token_path, refreshed, client_cfg, prior_refresh_token=refresh_token
    )
    return str(updated["access_token"])


def build_publish_metadata(
    blueprint: Blueprint,
    title_override: str | None = None,
    description_override: str | None = None,
    extra_tags: list[str] | None = None,
) -> tuple[str, str, list[str]]:
    """Derive clean title (<=100 chars), description, and deduplicated tags/hashtags."""
    raw_title = (title_override or blueprint.title).strip()
    if not raw_title:
        raise ValueError("Publish title cannot be blank")
    final_title = raw_title[:100].strip()

    raw_tags: list[str] = list(extra_tags or [])
    if blueprint.format == FormatPreset.SHORT_9X16:
        raw_tags.append("shorts")
    raw_tags.extend([blueprint.topic_domain.value, "coding", "softwareengineering"])

    clean_tags: list[str] = []
    seen_tags: set[str] = set()
    for tag in raw_tags:
        tok = re.sub(r"[^a-zA-Z0-9_]", "", tag.strip().lstrip("#")).lower()
        if tok and tok not in seen_tags:
            seen_tags.add(tok)
            clean_tags.append(tok)

    if description_override is not None:
        desc_body = description_override.strip()
        if not desc_body:
            raise ValueError("Publish description cannot be blank")
    else:
        desc_body = f"{blueprint.core_premise}\n\nArchitecture: {blueprint.persistent_subject}"

    hashtags = " ".join(f"#{tag}" for tag in clean_tags[:6])
    final_description = (
        desc_body if not hashtags or hashtags.split()[0] in desc_body.lower()
        else f"{desc_body}\n\n{hashtags}"
    )
    return final_title, final_description, clean_tags


def _s3_sigv4_put(
    endpoint_url: str,
    bucket: str,
    object_key: str,
    access_key: str,
    secret_key: str,
    payload: bytes,
    content_type: str = "video/mp4",
    region: str = "auto",
) -> None:
    """Upload bytes via pure-stdlib AWS SigV4 HTTP PUT (Cloudflare R2 & S3 compatible)."""
    parsed = urllib.parse.urlparse(endpoint_url.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid S3/R2 endpoint URL: {endpoint_url}")

    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = parsed.netloc
    base_path = parsed.path.rstrip("/")
    clean_key = object_key.lstrip("/")
    canonical_uri = urllib.parse.quote(f"{base_path}/{bucket}/{clean_key}", safe="/~")
    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        ["PUT", canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, "s3")
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    target_url = f"{parsed.scheme}://{host}{canonical_uri}"
    http_request(
        target_url,
        method="PUT",
        data=payload,
        headers={
            "Content-Type": content_type,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": auth_header,
        },
        timeout=120.0,
    )


def stage_mp4_for_instagram(
    slug: str,
    video_path: Path,
    video_url_override: str | None = None,
    root: Path | None = None,
) -> str:
    """Resolve or upload a public HTTPS MP4 URL for Instagram Reels Graph API ingestion."""
    _ = root
    if video_url_override and video_url_override.strip():
        return video_url_override.strip()

    provider = os.environ.get("IG_STAGING_PROVIDER", "none").strip().lower()
    base_url = os.environ.get("IG_STAGING_PUBLIC_BASE_URL", "").strip().rstrip("/")

    if provider == "none":
        raise ValueError(
            "Instagram Graph API requires a public MP4 URL (pass --video-url or set IG_STAGING_PROVIDER in .env)"
        )
    if provider == "public_url":
        if not base_url:
            raise ValueError("IG_STAGING_PUBLIC_BASE_URL must be set when IG_STAGING_PROVIDER=public_url")
        return f"{base_url}/{slug}.mp4"
    if provider in {"r2", "s3"}:
        endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
        bucket = os.environ.get("S3_BUCKET", "").strip()
        access_key = os.environ.get("S3_ACCESS_KEY_ID", "").strip()
        secret_key = os.environ.get("S3_SECRET_ACCESS_KEY", "").strip()
        if not all([endpoint, bucket, access_key, secret_key, base_url]):
            raise ValueError(
                "S3/R2 staging requires S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY_ID, "
                "S3_SECRET_ACCESS_KEY, and IG_STAGING_PUBLIC_BASE_URL in .env"
            )
        object_key = f"cm/{slug}/{video_path.name}"
        region = "auto" if provider == "r2" else os.environ.get("S3_REGION", "us-east-1").strip()
        _s3_sigv4_put(
            endpoint_url=endpoint,
            bucket=bucket,
            object_key=object_key,
            access_key=access_key,
            secret_key=secret_key,
            payload=video_path.read_bytes(),
            region=region,
        )
        return f"{base_url}/{object_key}"

    raise ValueError(f"Unsupported IG_STAGING_PROVIDER '{provider}' (use none, public_url, r2, or s3)")


def upload_youtube_video(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy: YouTubePrivacy,
    is_short: bool,
    access_token: str,
) -> PlatformPublishResult:
    """Upload an MP4 file to YouTube via the 2-step Data API v3 resumable protocol."""
    mp4_bytes = video_path.read_bytes()
    init_body = json.dumps(
        {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": "28",
            },
            "status": {
                "privacyStatus": privacy.value,
                "selfDeclaredMadeForKids": False,
            },
        }
    ).encode("utf-8")

    _, resp_headers, _ = http_request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        method="POST",
        data=init_body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(len(mp4_bytes)),
        },
        timeout=30.0,
    )
    upload_url = resp_headers.get("location")
    if not upload_url:
        raise RuntimeError("YouTube resumable upload initialization did not return a Location header")

    payload = http_json(
        upload_url,
        method="PUT",
        data=mp4_bytes,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "video/mp4",
            "Content-Length": str(len(mp4_bytes)),
        },
        timeout=300.0,
    )
    if not isinstance(payload, dict) or not payload.get("id"):
        raise RuntimeError("YouTube upload response missing video 'id'")

    video_id = str(payload["id"])
    url = (
        f"https://www.youtube.com/shorts/{video_id}"
        if is_short
        else f"https://www.youtube.com/watch?v={video_id}"
    )
    return PlatformPublishResult(
        platform="youtube",
        status="published",
        video_id=video_id,
        url=url,
        privacy=privacy.value,
    )


def upload_instagram_reel(
    video_url: str,
    caption: str,
    ig_user_id: str,
    access_token: str,
    poll_interval_sec: float = 3.0,
    max_polls: int = 30,
) -> PlatformPublishResult:
    """Create, poll, and publish an Instagram Reel via Meta Graph API v21.0."""
    create_form = urllib.parse.urlencode(
        {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": access_token,
        }
    ).encode("utf-8")
    created = http_json(
        f"https://graph.facebook.com/v21.0/{urllib.parse.quote(ig_user_id)}/media",
        method="POST",
        data=create_form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not isinstance(created, dict) or not created.get("id"):
        raise RuntimeError("Instagram container creation response missing 'id'")
    creation_id = str(created["id"])

    finished = False
    for _ in range(max_polls):
        status_url = (
            f"https://graph.facebook.com/v21.0/{urllib.parse.quote(creation_id)}?"
            + urllib.parse.urlencode({"fields": "status_code,status", "access_token": access_token})
        )
        status_obj = http_json(status_url)
        status_code = (
            str(status_obj.get("status_code", "")).upper()
            if isinstance(status_obj, dict)
            else ""
        )
        if status_code == "FINISHED":
            finished = True
            break
        if status_code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram media container failed with status '{status_code}'")
        time.sleep(poll_interval_sec)

    if not finished:
        raise RuntimeError("Timed out waiting for Instagram Reels container processing")

    publish_form = urllib.parse.urlencode(
        {"creation_id": creation_id, "access_token": access_token}
    ).encode("utf-8")
    published = http_json(
        f"https://graph.facebook.com/v21.0/{urllib.parse.quote(ig_user_id)}/media_publish",
        method="POST",
        data=publish_form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not isinstance(published, dict) or not published.get("id"):
        raise RuntimeError("Instagram media_publish response missing 'id'")
    media_id = str(published["id"])

    permalink: str | None = None
    try:
        meta = http_json(
            f"https://graph.facebook.com/v21.0/{urllib.parse.quote(media_id)}?"
            + urllib.parse.urlencode({"fields": "permalink", "access_token": access_token})
        )
        if isinstance(meta, dict) and meta.get("permalink"):
            permalink = str(meta["permalink"])
    except RuntimeError:
        permalink = None

    return PlatformPublishResult(
        platform="instagram",
        status="published",
        video_id=media_id,
        url=permalink,
        staged_url=video_url,
    )


def publish_run(
    slug: str,
    platforms: list[PublishPlatform] | None = None,
    privacy: YouTubePrivacy | None = None,
    title: str | None = None,
    description: str | None = None,
    extra_tags: list[str] | None = None,
    video_path: Path | None = None,
    video_url: str | None = None,
    dry_run: bool = False,
    allow_mock_audio: bool = False,
    root: Path | None = None,
) -> PublishReceipt:
    """Validate render/audio readiness and publish (or dry-run) a video to Shorts/Reels."""
    load_env(root)
    load_state(slug, root)
    rdir = run_dir(slug, root)
    bp_path = rdir / "blueprint.json"
    blueprint = Blueprint.model_validate_json(bp_path.read_text(encoding="utf-8"))

    target_platforms = list(dict.fromkeys(platforms or [PublishPlatform.YOUTUBE]))
    raw_privacy = os.environ.get("YOUTUBE_DEFAULT_PRIVACY", "private").strip().lower()
    resolved_privacy = privacy or YouTubePrivacy(raw_privacy)

    mp4_path = (video_path or (rdir / "final.mp4")).resolve()
    if not mp4_path.is_file():
        raise FileNotFoundError(
            f"Rendered video not found at {mp4_path} (run: cm render {slug})"
        )
    probe_media(mp4_path)

    timeline_path = rdir / "audio" / "timeline.json"
    if timeline_path.is_file() and not dry_run and not allow_mock_audio:
        timeline = AudioTimeline.model_validate_json(timeline_path.read_text(encoding="utf-8"))
        if timeline.provider == "mock":
            raise ValueError(
                f"Run '{slug}' used mock TTS audio. Re-run 'cm tts {slug} --provider auto' "
                "or pass --allow-mock-audio to publish anyway."
            )

    final_title, final_desc, final_tags = build_publish_metadata(
        blueprint,
        title_override=title,
        description_override=description,
        extra_tags=extra_tags,
    )

    if dry_run:
        dry_results: list[PlatformPublishResult] = []
        for plat in target_platforms:
            if plat == PublishPlatform.YOUTUBE:
                dry_results.append(
                    PlatformPublishResult(
                        platform="youtube",
                        status="dry_run",
                        privacy=resolved_privacy.value,
                    )
                )
            else:
                dry_results.append(
                    PlatformPublishResult(
                        platform="instagram",
                        status="dry_run",
                        staged_url=video_url,
                    )
                )
        return PublishReceipt(
            slug=slug,
            title=final_title,
            description=final_desc,
            tags=final_tags,
            video_path=str(mp4_path),
            dry_run=True,
            results=dry_results,
        )

    results: list[PlatformPublishResult] = []
    catalog_platforms: dict[str, dict[str, str]] = {}

    for plat in target_platforms:
        try:
            if plat == PublishPlatform.YOUTUBE:
                token = get_youtube_access_token(root=root)
                res = upload_youtube_video(
                    video_path=mp4_path,
                    title=final_title,
                    description=final_desc,
                    tags=final_tags,
                    privacy=resolved_privacy,
                    is_short=(blueprint.format == FormatPreset.SHORT_9X16),
                    access_token=token,
                )
            else:
                ig_user_id = os.environ.get("IG_USER_ID", "").strip()
                ig_token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
                if not ig_user_id or not ig_token:
                    raise ValueError(
                        "Instagram publishing requires IG_USER_ID and IG_ACCESS_TOKEN in .env"
                    )
                staged_url = stage_mp4_for_instagram(
                    slug=slug,
                    video_path=mp4_path,
                    video_url_override=video_url,
                    root=root,
                )
                res = upload_instagram_reel(
                    video_url=staged_url,
                    caption=final_desc,
                    ig_user_id=ig_user_id,
                    access_token=ig_token,
                )
            results.append(res)
            meta: dict[str, str] = {"video_id": res.video_id or "", "status": res.status}
            if res.url:
                meta["url"] = res.url
            if res.privacy:
                meta["privacy"] = res.privacy
            catalog_platforms[plat.value] = meta
        except Exception as exc:
            results.append(
                PlatformPublishResult(
                    platform=plat.value,
                    status="failed",
                    error=redact_secrets(str(exc)),
                )
            )

    receipt = PublishReceipt(
        slug=slug,
        title=final_title,
        description=final_desc,
        tags=final_tags,
        video_path=str(mp4_path),
        dry_run=False,
        results=results,
    )
    atomic_write_text(rdir / "publish_receipt.json", receipt.model_dump_json(indent=2) + "\n")

    if catalog_platforms:
        record_published_entry(
            slug=slug,
            title=final_title,
            topic_domain=blueprint.topic_domain,
            fmt=blueprint.format,
            platforms=catalog_platforms,
            keywords=final_tags,
            root=root,
        )
        ok_summary = ", ".join(f"{k}:{v['video_id']}" for k, v in catalog_platforms.items())
        update_stage(
            slug,
            PipelineStage.PUBLISH,
            StageStatus.PASSED,
            notes=f"Published ({ok_summary})",
            error_count=sum(1 for r in results if r.status == "failed"),
            advance_on_pass=True,
            root=root,
        )
    else:
        update_stage(
            slug,
            PipelineStage.PUBLISH,
            StageStatus.FAILED,
            notes=f"Publish failed on {len(results)} platform(s)",
            error_count=len(results),
            advance_on_pass=False,
            root=root,
        )

    return receipt
