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
from collections.abc import Callable
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
META_GRAPH_API_VERSION = "v21.0"
META_GRAPH_API_BASE = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
_USER_AGENT = "content-machine/3.0 (publish-cli)"


def redact_secrets(text: str) -> str:
    """Redact API keys, OAuth tokens, and client secrets from diagnostic text."""
    redacted = re.sub(
        r"([?&]?(?:key|access_token|client_secret|refresh_token|fb_exchange_token)=)[^&\s\"']+",
        r"\1***",
        text,
    )
    redacted = re.sub(
        r"(Authorization:\s*(?:OAuth|Bearer)\s+)\S+",
        r"\1***",
        redacted,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r'("(?:access_token|refresh_token|client_secret|fb_exchange_token)"\s*:\s*")[^"]*"',
        r'\1***"',
        redacted,
    )


def _get_instagram_env() -> tuple[str, str]:
    """Read Instagram access token and business/creator account ID from environment."""
    token = (
        os.environ.get("IG_ACCESS_TOKEN", "").strip()
        or os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    )
    ig_user_id = (
        os.environ.get("IG_USER_ID", "").strip()
        or os.environ.get("INSTAGRAM_BUSINESS_ACCOUNT_ID", "").strip()
    )
    return token, ig_user_id


def _get_facebook_env() -> tuple[str, str]:
    """Read Facebook Page access token and Page ID from environment."""
    token = (
        os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
        or os.environ.get("IG_ACCESS_TOKEN", "").strip()
        or os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    )
    fb_page_id = (
        os.environ.get("FB_PAGE_ID", "").strip()
        or os.environ.get("FACEBOOK_PAGE_ID", "").strip()
    )
    return token, fb_page_id


def update_env_file(updates: dict[str, str], root: Path | None = None) -> Path:
    """Atomically update or append key=value entries in .env while preserving comments."""
    env_path = (root or get_repo_root()) / ".env"
    lines = (
        env_path.read_text(encoding="utf-8").splitlines(keepends=True)
        if env_path.is_file()
        else []
    )

    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line if line.endswith(("\n", "\r")) else f"{line}\n")

    for key, val in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}\n")

    atomic_write_text(env_path, "".join(new_lines))
    return env_path.resolve()


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
    on_auth_url: Callable[[str], None] | None = None,
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
        if on_auth_url is not None:
            on_auth_url(auth_url)
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


def exchange_instagram_long_lived_token(
    short_token: str, app_id: str, app_secret: str
) -> str:
    """Exchange a short-lived Meta user token for a 60-day long-lived token."""
    url = (
        f"{META_GRAPH_API_BASE}/oauth/access_token?"
        + urllib.parse.urlencode(
            {
                "grant_type": "fb_exchange_token",
                "client_id": app_id.strip(),
                "client_secret": app_secret.strip(),
                "fb_exchange_token": short_token.strip(),
            }
        )
    )
    payload = http_json(url)
    if not isinstance(payload, dict) or not str(payload.get("access_token", "")).strip():
        raise RuntimeError("Meta token exchange response missing 'access_token'")
    return str(payload["access_token"]).strip()


def discover_instagram_accounts(
    access_token: str,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Fetch Facebook Pages and linked Instagram Business/Creator accounts."""
    url = (
        f"{META_GRAPH_API_BASE}/me/accounts?"
        + urllib.parse.urlencode(
            {
                "fields": "id,name,access_token,instagram_business_account{id,username,name}",
                "access_token": access_token,
            }
        )
    )
    payload = http_json(url)
    if not isinstance(payload, dict):
        raise RuntimeError("Meta /me/accounts returned non-object JSON")
    pages_raw = payload.get("data", [])
    if not isinstance(pages_raw, list):
        pages_raw = []

    pages_summary: list[dict[str, object]] = []
    ig_accounts: list[dict[str, str]] = []
    for page in pages_raw:
        if not isinstance(page, dict):
            continue
        page_id = str(page.get("id", "")).strip()
        page_name = str(page.get("name", "")).strip()
        ig_block = page.get("instagram_business_account")
        ig_id = str(ig_block.get("id", "")).strip() if isinstance(ig_block, dict) else ""
        has_ig = bool(ig_id)
        pages_summary.append({"id": page_id, "name": page_name, "has_instagram": has_ig})
        if has_ig and isinstance(ig_block, dict):
            ig_accounts.append(
                {
                    "page_id": page_id,
                    "page_name": page_name,
                    "ig_id": ig_id,
                    "username": str(ig_block.get("username", "")).strip(),
                    "name": str(ig_block.get("name", "")).strip(),
                    "token": str(page.get("access_token") or access_token).strip(),
                }
            )
    return pages_summary, ig_accounts


def get_instagram_profile(ig_user_id: str, access_token: str) -> dict[str, object]:
    """Fetch verified Instagram Professional profile metadata."""
    url = (
        f"{META_GRAPH_API_BASE}/{urllib.parse.quote(ig_user_id.strip())}?"
        + urllib.parse.urlencode(
            {
                "fields": "id,username,name,followers_count",
                "access_token": access_token,
            }
        )
    )
    payload = http_json(url)
    if not isinstance(payload, dict) or not str(payload.get("id", "")).strip():
        raise RuntimeError("Instagram profile response missing 'id'")
    try:
        followers_count = max(0, int(payload.get("followers_count", 0)))
    except (TypeError, ValueError):
        followers_count = 0
    return {
        "id": str(payload["id"]).strip(),
        "username": str(payload.get("username", "")).strip(),
        "name": str(payload.get("name", "")).strip(),
        "followers_count": followers_count,
    }


def check_instagram_connection(root: Path | None = None) -> dict[str, object]:
    """Inspect .env Instagram/Facebook credentials and live Meta Graph API link status (no secrets returned)."""
    load_env(root)
    token, ig_user_id = _get_instagram_env()
    _, fb_page_id = _get_facebook_env()
    if not token:
        return {
            "connected": False,
            "token_valid": False,
            "has_token": False,
            "has_ig_user_id": bool(ig_user_id),
            "configured_ig_user_id": ig_user_id or None,
            "fb_page_id": fb_page_id or None,
            "fb_user_name": None,
            "pages": [],
            "accounts": [],
            "profile": None,
            "error": "IG_ACCESS_TOKEN is not set in .env",
        }

    me_url = f"{META_GRAPH_API_BASE}/me?" + urllib.parse.urlencode(
        {"fields": "id,name", "access_token": token}
    )
    try:
        me_obj = http_json(me_url)
    except RuntimeError as exc:
        return {
            "connected": False,
            "token_valid": False,
            "has_token": True,
            "has_ig_user_id": bool(ig_user_id),
            "configured_ig_user_id": ig_user_id or None,
            "fb_page_id": fb_page_id or None,
            "fb_user_name": None,
            "pages": [],
            "accounts": [],
            "profile": None,
            "error": redact_secrets(str(exc)),
        }

    fb_user_name = str(me_obj.get("name", "")).strip() or None if isinstance(me_obj, dict) else None
    pages: list[dict[str, object]] = []
    raw_accounts: list[dict[str, str]] = []
    accounts_error: str | None = None
    try:
        pages, raw_accounts = discover_instagram_accounts(token)
    except RuntimeError as exc:
        accounts_error = redact_secrets(str(exc))

    safe_accounts = [
        {k: v for k, v in acc.items() if k != "token"}
        for acc in raw_accounts
    ]
    target_ig_id = ig_user_id or (raw_accounts[0]["ig_id"] if len(raw_accounts) == 1 else "")
    profile: dict[str, object] | None = None
    profile_error: str | None = None
    if target_ig_id:
        lookup_token = next(
            (acc["token"] for acc in raw_accounts if acc["ig_id"] == target_ig_id),
            token,
        )
        try:
            profile = get_instagram_profile(target_ig_id, lookup_token)
        except RuntimeError as exc:
            profile_error = redact_secrets(str(exc))

    connected = bool(profile is not None and bool(ig_user_id))
    error: str | None = None
    if not connected:
        if profile_error:
            error = profile_error
        elif accounts_error and not target_ig_id:
            error = accounts_error
        elif not raw_accounts and not target_ig_id:
            if pages:
                error = (
                    "No Instagram Professional account is linked to your Facebook Page(s) yet. "
                    "Link Instagram to your Page, regenerate your token, and run: cm auth instagram"
                )
            else:
                error = (
                    "No Facebook Pages found for this token. Link a Facebook Page to your Instagram "
                    "Professional account, regenerate your token, and run: cm auth instagram"
                )
        elif profile is not None and not ig_user_id:
            uname = profile.get("username") or target_ig_id
            error = f"Found Instagram @{uname}, but IG_USER_ID is not set in .env (run: cm auth instagram)"
        elif len(raw_accounts) > 1 and not ig_user_id:
            error = "Multiple Instagram accounts found, and IG_USER_ID is not set in .env (run: cm auth instagram)"

    return {
        "connected": connected,
        "token_valid": True,
        "has_token": True,
        "has_ig_user_id": bool(ig_user_id),
        "configured_ig_user_id": ig_user_id or None,
        "fb_page_id": fb_page_id or None,
        "fb_user_name": fb_user_name,
        "pages": pages,
        "accounts": safe_accounts,
        "profile": profile,
        "error": error,
    }


def authenticate_instagram(
    token: str,
    app_id: str | None = None,
    app_secret: str | None = None,
    ig_user_id: str | None = None,
    account_index: int = 0,
    root: Path | None = None,
) -> dict[str, object]:
    """Exchange/discover Instagram + Facebook Page credentials via Meta Graph API and save to .env."""
    working_token = token.strip()
    if not working_token:
        raise ValueError("Meta access token cannot be blank")

    clean_app_id = (app_id or "").strip()
    clean_app_secret = (app_secret or "").strip()
    if bool(clean_app_id) != bool(clean_app_secret):
        raise ValueError("Both --app-id and --app-secret are required for long-lived token exchange")

    exchanged_long_lived = False
    if clean_app_id and clean_app_secret:
        working_token = exchange_instagram_long_lived_token(
            working_token, clean_app_id, clean_app_secret
        )
        exchanged_long_lived = True

    pages, ig_accounts = discover_instagram_accounts(working_token)
    manual_id = (ig_user_id or "").strip()
    selected_page_id = ""
    if manual_id:
        matched = next((acc for acc in ig_accounts if acc["ig_id"] == manual_id), None)
        selected_ig_id = manual_id
        selected_token = matched["token"] if matched else working_token
        selected_page_id = matched["page_id"] if matched else ""
    elif ig_accounts:
        if account_index < 0 or account_index >= len(ig_accounts):
            raise ValueError(
                f"Invalid --account-index {account_index} (found {len(ig_accounts)} linked Instagram account(s))"
            )
        chosen = ig_accounts[account_index]
        selected_ig_id = chosen["ig_id"]
        selected_token = chosen["token"]
        selected_page_id = chosen["page_id"]
    else:
        raise ValueError(
            f"Found {len(pages)} Facebook Page(s) but no linked Instagram Professional account. "
            "Switch Instagram to Professional, link it to your Facebook Page, approve both in "
            "Graph API Explorer, or pass --ig-user-id."
        )

    env_updates: dict[str, str] = {
        "IG_ACCESS_TOKEN": selected_token,
        "IG_USER_ID": selected_ig_id,
    }
    if selected_page_id:
        env_updates["FB_PAGE_ID"] = selected_page_id
        os.environ["FB_PAGE_ID"] = selected_page_id

    env_path = update_env_file(env_updates, root=root)
    os.environ["IG_ACCESS_TOKEN"] = selected_token
    os.environ["IG_USER_ID"] = selected_ig_id

    profile: dict[str, object] | None = None
    profile_warning: str | None = None
    try:
        profile = get_instagram_profile(selected_ig_id, selected_token)
    except RuntimeError as exc:
        profile_warning = redact_secrets(str(exc))

    return {
        "env_path": str(env_path),
        "ig_user_id": selected_ig_id,
        "fb_page_id": selected_page_id or None,
        "exchanged_long_lived": exchanged_long_lived,
        "pages_found": len(pages),
        "accounts_found": len(ig_accounts),
        "profile": profile,
        "profile_warning": profile_warning,
    }


def stage_mp4_for_instagram(
    slug: str,
    video_path: Path,
    video_url_override: str | None = None,
    root: Path | None = None,
) -> str | None:
    """Resolve optional public HTTPS MP4 URL, or return None for direct binary rupload."""
    _ = root
    if video_url_override and video_url_override.strip():
        return video_url_override.strip()

    provider = os.environ.get("IG_STAGING_PROVIDER", "direct").strip().lower()
    base_url = os.environ.get("IG_STAGING_PUBLIC_BASE_URL", "").strip().rstrip("/")

    if provider in {"", "direct", "none"}:
        return None
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

    raise ValueError(
        f"Unsupported IG_STAGING_PROVIDER '{provider}' (use direct, none, public_url, r2, or s3)"
    )


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
    video_url: str | None,
    caption: str,
    ig_user_id: str,
    access_token: str,
    *,
    video_path: Path | None = None,
    poll_interval_sec: float = 3.0,
    max_polls: int = 30,
) -> PlatformPublishResult:
    """Create, optionally rupload a local MP4, poll, and publish an Instagram Reel via Graph API."""
    clean_video_url = video_url.strip() if video_url and video_url.strip() else None
    if clean_video_url is None and video_path is None:
        raise ValueError("Instagram upload requires either video_path or video_url")

    create_params: dict[str, str] = {
        "media_type": "REELS",
        "caption": caption,
        "share_to_feed": "true",
        "access_token": access_token,
    }
    mp4_bytes: bytes | None = None
    if clean_video_url is not None:
        create_params["video_url"] = clean_video_url
    else:
        assert video_path is not None
        mp4_bytes = video_path.read_bytes()
        if not mp4_bytes:
            raise ValueError(f"Instagram video file is empty: {video_path}")
        create_params["upload_type"] = "resumable"

    created = http_json(
        f"{META_GRAPH_API_BASE}/{urllib.parse.quote(ig_user_id)}/media",
        method="POST",
        data=urllib.parse.urlencode(create_params).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not isinstance(created, dict) or not created.get("id"):
        raise RuntimeError("Instagram container creation response missing 'id'")
    creation_id = str(created["id"])

    if mp4_bytes is not None:
        upload_uri = str(
            created.get("uri")
            or f"https://rupload.facebook.com/ig-api-upload/{META_GRAPH_API_VERSION}/{urllib.parse.quote(creation_id)}"
        )
        http_request(
            upload_uri,
            method="POST",
            data=mp4_bytes,
            headers={
                "Authorization": f"OAuth {access_token}",
                "offset": "0",
                "file_size": str(len(mp4_bytes)),
                "Content-Type": "application/octet-stream",
            },
            timeout=300.0,
        )

    finished = False
    for _ in range(max_polls):
        status_url = (
            f"{META_GRAPH_API_BASE}/{urllib.parse.quote(creation_id)}?"
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
        f"{META_GRAPH_API_BASE}/{urllib.parse.quote(ig_user_id)}/media_publish",
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
            f"{META_GRAPH_API_BASE}/{urllib.parse.quote(media_id)}?"
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
        staged_url=clean_video_url,
    )


def upload_facebook_reel(
    video_path: Path,
    description: str,
    fb_page_id: str,
    access_token: str,
    *,
    video_url: str | None = None,
) -> PlatformPublishResult:
    """Create, rupload/stage, and publish a Facebook Page Reel via Meta Graph API."""
    clean_video_url = video_url.strip() if video_url and video_url.strip() else None
    mp4_bytes = b""
    if clean_video_url is None:
        mp4_bytes = video_path.read_bytes()
        if not mp4_bytes:
            raise ValueError(f"Facebook video file is empty: {video_path}")

    reels_endpoint = f"{META_GRAPH_API_BASE}/{urllib.parse.quote(fb_page_id)}/video_reels"
    started = http_json(
        reels_endpoint,
        method="POST",
        data=urllib.parse.urlencode(
            {"upload_phase": "start", "access_token": access_token}
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not isinstance(started, dict) or not str(started.get("video_id", "")).strip():
        raise RuntimeError("Facebook Reels upload initialization missing 'video_id'")
    video_id = str(started["video_id"]).strip()
    upload_url = str(
        started.get("upload_url")
        or f"https://rupload.facebook.com/video-upload/{META_GRAPH_API_VERSION}/{urllib.parse.quote(video_id)}"
    )

    if clean_video_url is not None:
        http_request(
            upload_url,
            method="POST",
            data=b"",
            headers={
                "Authorization": f"OAuth {access_token}",
                "file_url": clean_video_url,
            },
            timeout=120.0,
        )
    else:
        http_request(
            upload_url,
            method="POST",
            data=mp4_bytes,
            headers={
                "Authorization": f"OAuth {access_token}",
                "offset": "0",
                "file_size": str(len(mp4_bytes)),
                "Content-Type": "application/octet-stream",
            },
            timeout=300.0,
        )

    finished = http_json(
        reels_endpoint,
        method="POST",
        data=urllib.parse.urlencode(
            {
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": description,
                "access_token": access_token,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not isinstance(finished, dict) or finished.get("success") is False:
        raise RuntimeError("Facebook Reels finish phase failed")

    reel_url = f"https://www.facebook.com/reel/{video_id}"
    try:
        meta = http_json(
            f"{META_GRAPH_API_BASE}/{urllib.parse.quote(video_id)}?"
            + urllib.parse.urlencode({"fields": "permalink_url", "access_token": access_token})
        )
        if isinstance(meta, dict) and str(meta.get("permalink_url", "")).strip():
            raw_link = str(meta["permalink_url"]).strip()
            reel_url = (
                f"https://www.facebook.com{raw_link}"
                if raw_link.startswith("/")
                else raw_link
            )
    except RuntimeError:
        pass

    return PlatformPublishResult(
        platform="facebook",
        status="published",
        video_id=video_id,
        url=reel_url,
        staged_url=clean_video_url,
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
                        platform=plat.value,
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
            elif plat == PublishPlatform.INSTAGRAM:
                ig_token, ig_user_id = _get_instagram_env()
                if not ig_user_id or not ig_token:
                    raise ValueError(
                        "Instagram publishing requires IG_USER_ID and IG_ACCESS_TOKEN in .env "
                        "(run: cm auth instagram)"
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
                    video_path=None if staged_url else mp4_path,
                )
            else:
                fb_token, fb_page_id = _get_facebook_env()
                if not fb_page_id or not fb_token:
                    raise ValueError(
                        "Facebook publishing requires FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN/IG_ACCESS_TOKEN "
                        "in .env (run: cm auth instagram)"
                    )
                res = upload_facebook_reel(
                    video_path=mp4_path,
                    description=final_desc,
                    fb_page_id=fb_page_id,
                    access_token=fb_token,
                    video_url=video_url,
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
