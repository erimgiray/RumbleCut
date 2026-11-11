"""🚀 Publish: post clips to YouTube, Instagram, TikTok and X from RumbleCut.

Each platform is reached through *your own* free developer app (client ID + secret you paste into
Settings → Publishing accounts). You sign in once in your browser; RumbleCut keeps the login,
encrypted for your Windows account, and refreshes it when it runs out.

  YouTube    YouTube Data API v3 - resumable upload. Public, unlisted or private; can schedule.
  Instagram  Instagram API with Facebook Login - Reels, uploaded straight from this PC. Needs a
             Creator/Business Instagram account linked to a Facebook Page.
  TikTok     Content Posting API "upload" - the clip lands in your TikTok inbox as a draft; you add
             the caption and press Post in the TikTok app. (Direct public posting needs TikTok to
             audit the app.)
  X          X API v2 - chunked media upload, then a post. Pay-per-use (about $0.015 per post).

Scheduling: YouTube schedules itself (publishAt). Other posts wait in a queue; a Windows scheduled
task starts `RumbleCut.exe --publish-due` at the time, so RumbleCut doesn't need to be open (the PC
must be on).
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import __version__, ai, config
from . import ffmpeg_tools as ft
from .ffmpeg_tools import Cancelled

PLATFORMS = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok", "x": "X"}
REDIRECT_PORT = 47123
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback/"
GRAPH_VERSION = "v26.0"
UA = f"RumbleCut/{__version__}"

# Every URL in one place (tests point these at a local fake server).
URLS = {
    "google_auth": "https://accounts.google.com/o/oauth2/v2/auth",
    "google_token": "https://oauth2.googleapis.com/token",
    "youtube_api": "https://www.googleapis.com/youtube/v3",
    "youtube_upload": "https://www.googleapis.com/upload/youtube/v3/videos",
    "fb_dialog": f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth",
    "graph": f"https://graph.facebook.com/{GRAPH_VERSION}",
    "rupload": f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}",
    "tiktok_auth": "https://www.tiktok.com/v2/auth/authorize/",
    "tiktok_api": "https://open.tiktokapis.com/v2",
    "x_auth": "https://x.com/i/oauth2/authorize",
    "x_api": "https://api.x.com/2",
}

# What each platform needs from you, shown in Settings.
SETUP = {
    "youtube": {
        "id_label": "Client ID", "secret_label": "Client secret",
        "steps": [
            "Open console.cloud.google.com and create a project (any name, e.g. “RumbleCut”).",
            "APIs & Services → Library → enable “YouTube Data API v3”.",
            "APIs & Services → OAuth consent screen: External, add your own Google account as a test user, "
            "and add the scopes youtube.upload and youtube.readonly.",
            "Credentials → Create credentials → OAuth client ID → Application type “Desktop app”.",
            "Paste the Client ID and Client secret here, then press Connect.",
        ],
        "link": "https://console.cloud.google.com/apis/credentials",
    },
    "instagram": {
        "id_label": "App ID", "secret_label": "App secret",
        "steps": [
            "In the Instagram app: Settings → Account type and tools → Switch to professional account "
            "(Creator is fine). It's free and reversible.",
            "Create a Facebook Page (any name) and link your Instagram to it: Page settings → "
            "Linked accounts → Instagram.",
            "Open developers.facebook.com → My Apps → Create app → “Other” → type “Business”.",
            "Add the products “Facebook Login for Business” and “Instagram” (API setup with Facebook login).",
            f"Facebook Login → Settings → Valid OAuth Redirect URIs: {REDIRECT_URI}",
            "Leave the app in Development mode (fine for your own accounts). App settings → Basic: copy "
            "the App ID and App secret here, then press Connect.",
        ],
        "link": "https://developers.facebook.com/apps/",
    },
    "tiktok": {
        "id_label": "Client key", "secret_label": "Client secret",
        "steps": [
            "Open developers.tiktok.com → Manage apps → Connect an app (platform: Desktop).",
            "Add the products “Login Kit” and “Content Posting API” (leave Direct Post off).",
            "Scopes: user.info.basic and video.upload.",
            f"Login Kit → Redirect URI: {REDIRECT_URI}",
            "Use the Sandbox: add your TikTok account as a target user, then copy the Client key and "
            "Client secret here and press Connect. Clips arrive in your TikTok inbox as drafts.",
        ],
        "link": "https://developers.tiktok.com/apps/",
    },
    "x": {
        "id_label": "Client ID", "secret_label": "Client secret (if shown)",
        "steps": [
            "Open console.x.com (developer.x.com), create a project and app, and add some API credit "
            "(pay-per-use, about $0.015 per post).",
            "User authentication settings: OAuth 2.0, type “Native App” or “Web App”, permissions "
            "“Read and write”.",
            f"Callback URI: {REDIRECT_URI}   Website: https://github.com/erimgiray/RumbleCut",
            "Keys and tokens → OAuth 2.0 Client ID (and secret, for a Web App): paste here, then Connect.",
        ],
        "link": "https://console.x.com/",
    },
}


class PublishError(Exception):
    pass


# ============================================================================ stored secrets
def _store(key: str) -> dict:
    raw = config.load_settings().get(key, {}) or {}
    return raw if isinstance(raw, dict) else {}


def _get_secret(key: str, platform: str) -> dict:
    enc = _store(key).get(platform, "")
    if not enc:
        return {}
    try:
        return json.loads(ai._unprotect(enc) or "{}")
    except Exception:
        return {}


def _set_secret(key: str, platform: str, value: dict | None) -> None:
    d = dict(_store(key))
    if value:
        d[platform] = ai._protect(json.dumps(value))
    else:
        d.pop(platform, None)
    config.save_settings({key: d})


def set_credentials(platform: str, client_id: str, client_secret: str) -> None:
    _set_secret("pub_creds", platform, {"id": client_id.strip(), "secret": client_secret.strip()}
                if client_id.strip() else None)


def get_credentials(platform: str) -> tuple[str, str]:
    d = _get_secret("pub_creds", platform)
    return d.get("id", ""), d.get("secret", "")


def get_token(platform: str) -> dict:
    return _get_secret("pub_tokens", platform)


def save_token(platform: str, tok: dict) -> None:
    _set_secret("pub_tokens", platform, tok)


def disconnect(platform: str) -> None:
    save_token(platform, {})


def account_name(platform: str) -> str:
    return get_token(platform).get("account", "")


def is_connected(platform: str) -> bool:
    return bool(get_token(platform).get("access_token"))


# ============================================================================ HTTP
def _request(method: str, url: str, *, data: bytes | None = None, form: dict | None = None,
             body: dict | None = None, headers: dict | None = None, timeout: float = 120,
             expect_json: bool = True):
    h = {"User-Agent": UA, **(headers or {})}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json; charset=UTF-8")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            hdrs = dict(r.headers)
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status, hdrs = e.code, dict(e.headers or {})
        if status == 308:                         # YouTube resumable: "resume incomplete"
            return status, hdrs, {}
        msg = raw.decode("utf-8", "replace")[:400]
        try:
            j = json.loads(msg)
            err = j.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code") or msg
            else:
                msg = j.get("error_description") or j.get("detail") or j.get("title") or err or msg
        except Exception:
            pass
        raise PublishError(f"{e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise PublishError(f"couldn't connect ({e.reason})") from e
    except (ConnectionError, TimeoutError, OSError, http.client.HTTPException) as e:
        raise PublishError(f"the connection dropped ({e}) - check your internet and try again") from e
    if not expect_json:
        return status, hdrs, raw
    try:
        return status, hdrs, (json.loads(raw) if raw.strip() else {})
    except ValueError:
        return status, hdrs, {"_raw": raw.decode("utf-8", "replace")}


def _bearer(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


# ============================================================================ sign-in (OAuth)
def _pkce(hex_challenge: bool = False) -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = digest.hex() if hex_challenge else base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/callback"):
            self.send_response(404)
            self.end_headers()
            return
        q = dict(urllib.parse.parse_qsl(u.query))
        type(self).result.update(q)
        ok = "code" in q
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("Signed in - you can close this tab and go back to RumbleCut." if ok else
               f"Sign-in didn't finish: {q.get('error_description') or q.get('error') or 'no code returned'}")
        self.wfile.write(f"<html><body style='font-family:sans-serif;background:#1b1d22;color:#e5e7eb;"
                         f"padding:40px'><h2>RumbleCut</h2><p>{msg}</p></body></html>".encode())

    def log_message(self, *a):
        pass


class _DualStackServer(http.server.HTTPServer):
    """Listens on both ::1 and 127.0.0.1, because 'localhost' can be either on Windows."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


def _wait_for_code(auth_url: str, state: str, open_browser: Callable[[str], object] | None,
                   cancel: Callable[[], bool] | None, timeout: float) -> str:
    handler = type("Catcher", (_Catcher,), {"result": {}})
    try:
        srv = _DualStackServer(("::", REDIRECT_PORT), handler)
    except OSError:
        try:
            srv = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), handler)
        except OSError as e:
            raise PublishError(f"port {REDIRECT_PORT} is busy - close other sign-in windows and try again") from e
    srv.timeout = 0.5
    try:
        (open_browser or webbrowser.open)(auth_url)
        t0 = time.time()
        while not handler.result:
            if cancel and cancel():
                raise Cancelled()
            if time.time() - t0 > timeout:
                raise PublishError("sign-in timed out - press Connect to try again")
            srv.handle_request()
    finally:
        srv.server_close()
    r = handler.result
    if "code" not in r:
        raise PublishError(f"sign-in was cancelled or refused ({r.get('error_description') or r.get('error') or 'no code'})")
    if r.get("state") != state:
        raise PublishError("sign-in answer didn't match (state mismatch) - try again")
    return r["code"]


def connect(platform: str, open_browser: Callable[[str], object] | None = None,
            cancel: Callable[[], bool] | None = None, timeout: float = 300) -> str:
    """Sign in through the browser, store the login, and return the account name."""
    cid, secret = get_credentials(platform)
    if not cid:
        raise PublishError(f"add your {PLATFORMS[platform]} {SETUP[platform]['id_label']} in Settings first")
    state = secrets.token_urlsafe(16)
    if platform == "youtube":
        verifier, challenge = _pkce()
        url = URLS["google_auth"] + "?" + urllib.parse.urlencode({
            "client_id": cid, "redirect_uri": REDIRECT_URI, "response_type": "code",
            "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        code = _wait_for_code(url, state, open_browser, cancel, timeout)
        _, _, t = _request("POST", URLS["google_token"], form={
            "client_id": cid, "client_secret": secret, "code": code, "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
        tok = _norm_token(t)
        _, _, ch = _request("GET", URLS["youtube_api"] + "/channels?part=snippet&mine=true",
                            headers=_bearer(tok["access_token"]))
        items = ch.get("items") or []
        if not items:
            raise PublishError("this Google account has no YouTube channel yet - create one on youtube.com first")
        tok["account"] = items[0]["snippet"]["title"]
    elif platform == "instagram":
        url = URLS["fb_dialog"] + "?" + urllib.parse.urlencode({
            "client_id": cid, "redirect_uri": REDIRECT_URI, "state": state, "response_type": "code",
            "scope": "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement,business_management"})
        code = _wait_for_code(url, state, open_browser, cancel, timeout)
        _, _, t = _request("GET", URLS["graph"] + "/oauth/access_token?" + urllib.parse.urlencode({
            "client_id": cid, "client_secret": secret, "redirect_uri": REDIRECT_URI, "code": code}))
        _, _, t2 = _request("GET", URLS["graph"] + "/oauth/access_token?" + urllib.parse.urlencode({
            "grant_type": "fb_exchange_token", "client_id": cid, "client_secret": secret,
            "fb_exchange_token": t["access_token"]}))                      # long-lived (about 60 days)
        tok = _norm_token(t2 if t2.get("access_token") else t, default_life=60 * 86400)
        _, _, pages = _request("GET", URLS["graph"] + "/me/accounts?" + urllib.parse.urlencode({
            "fields": "name,instagram_business_account{id,username}", "access_token": tok["access_token"]}))
        igs = [(p.get("instagram_business_account") or {}) for p in pages.get("data", [])]
        igs = [x for x in igs if x.get("id")]
        if not igs:
            raise PublishError("no Instagram professional account is linked to your Facebook Pages - "
                               "switch Instagram to a Creator account and link it to a Page (see the steps)")
        tok["ig_id"] = igs[0]["id"]
        tok["account"] = "@" + igs[0].get("username", "instagram")
    elif platform == "tiktok":
        verifier, challenge = _pkce(hex_challenge=True)             # TikTok desktop wants hex
        url = URLS["tiktok_auth"] + "?" + urllib.parse.urlencode({
            "client_key": cid, "scope": "user.info.basic,video.upload", "response_type": "code",
            "redirect_uri": REDIRECT_URI, "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256"})
        code = _wait_for_code(url, state, open_browser, cancel, timeout)
        _, _, t = _request("POST", URLS["tiktok_api"] + "/oauth/token/", form={
            "client_key": cid, "client_secret": secret, "code": code, "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
        if t.get("error") and not t.get("access_token"):
            raise PublishError(t.get("error_description") or t["error"])
        tok = _norm_token(t)
        _, _, u = _request("GET", URLS["tiktok_api"] + "/user/info/?fields=open_id,display_name",
                           headers=_bearer(tok["access_token"]))
        tok["account"] = ((u.get("data") or {}).get("user") or {}).get("display_name") or "TikTok"
    elif platform == "x":
        verifier, challenge = _pkce()
        url = URLS["x_auth"] + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT_URI,
            "scope": "tweet.read tweet.write users.read media.write offline.access", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        code = _wait_for_code(url, state, open_browser, cancel, timeout)
        _, _, t = _request("POST", URLS["x_api"] + "/oauth2/token", form={
            "code": code, "grant_type": "authorization_code", "client_id": cid, "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier}, headers=_x_basic(cid, secret))
        tok = _norm_token(t)
        _, _, me = _request("GET", URLS["x_api"] + "/users/me", headers=_bearer(tok["access_token"]))
        tok["account"] = "@" + (me.get("data") or {}).get("username", "x")
    else:
        raise PublishError(f"unknown platform {platform}")
    save_token(platform, tok)
    return tok["account"]


def _x_basic(cid: str, secret: str) -> dict:
    if not secret:
        return {}
    return {"Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()}


def _norm_token(t: dict, default_life: int = 3600) -> dict:
    if not t.get("access_token"):
        raise PublishError(f"the sign-in didn't return a token ({t.get('error_description') or t.get('error') or t})")
    life = int(t.get("expires_in") or default_life)
    return {"access_token": t["access_token"], "refresh_token": t.get("refresh_token", ""),
            "expires_at": time.time() + life - 120}


def access_token(platform: str) -> str:
    """A valid access token, refreshed if it has run out."""
    tok = get_token(platform)
    if not tok.get("access_token"):
        raise PublishError(f"connect your {PLATFORMS[platform]} account in Settings → Publishing accounts")
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    cid, secret = get_credentials(platform)
    try:
        if platform == "youtube":
            _, _, t = _request("POST", URLS["google_token"], form={
                "client_id": cid, "client_secret": secret, "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"]})
        elif platform == "instagram":
            _, _, t = _request("GET", URLS["graph"] + "/oauth/access_token?" + urllib.parse.urlencode({
                "grant_type": "fb_exchange_token", "client_id": cid, "client_secret": secret,
                "fb_exchange_token": tok["access_token"]}))
            t.setdefault("expires_in", 60 * 86400)
        elif platform == "tiktok":
            _, _, t = _request("POST", URLS["tiktok_api"] + "/oauth/token/", form={
                "client_key": cid, "client_secret": secret, "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"]})
        else:
            _, _, t = _request("POST", URLS["x_api"] + "/oauth2/token", form={
                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid},
                headers=_x_basic(cid, secret))
    except PublishError as e:
        raise PublishError(f"your {PLATFORMS[platform]} login has expired - press Connect again ({e})") from e
    new = _norm_token(t)
    tok.update(access_token=new["access_token"], expires_at=new["expires_at"])
    if new["refresh_token"]:
        tok["refresh_token"] = new["refresh_token"]
    save_token(platform, tok)
    return tok["access_token"]


# ============================================================================ uploads
Progress = Callable[[float, str], None]


def _read_chunks(path: str, size: int):
    with open(path, "rb") as fh:
        while True:
            b = fh.read(size)
            if not b:
                return
            yield b


def upload_youtube(path: str, meta: dict, on_progress: Progress | None = None,
                   cancel: Callable[[], bool] | None = None) -> str:
    tok = access_token("youtube")
    size = os.path.getsize(path)
    privacy = meta.get("privacy", "public")
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if meta.get("publish_at"):                       # YouTube schedules itself: private until then
        status = {"privacyStatus": "private", "publishAt": meta["publish_at"], "selfDeclaredMadeForKids": False}
    tags = [t.lstrip("#") for t in meta.get("tags", []) if t.strip()]
    while sum(len(t) + 1 for t in tags) > 480:
        tags.pop()
    body = {"snippet": {"title": meta.get("title", "")[:100] or "Clip", "description": meta.get("description", "")[:4900],
                        "tags": tags, "categoryId": meta.get("category", "22")}, "status": status}
    _, hdrs, _ = _request("POST", URLS["youtube_upload"] + "?uploadType=resumable&part=snippet,status", body=body,
                          headers={**_bearer(tok), "X-Upload-Content-Length": str(size),
                                   "X-Upload-Content-Type": "video/mp4"})
    session = hdrs.get("Location") or hdrs.get("location")
    if not session:
        raise PublishError("YouTube didn't start the upload")
    sent, chunk = 0, 8 * 1024 * 1024
    result = {}
    for b in _read_chunks(path, chunk):
        if cancel and cancel():
            raise Cancelled()
        end = sent + len(b) - 1
        st, _, result = _request("PUT", session, data=b, headers={
            **_bearer(tok), "Content-Length": str(len(b)), "Content-Range": f"bytes {sent}-{end}/{size}",
            "Content-Type": "video/mp4"}, timeout=600)
        sent = end + 1
        if on_progress:
            on_progress(sent / size, f"YouTube: uploading… {sent * 100 // size}%")
    vid = (result or {}).get("id")
    if not vid:
        raise PublishError("YouTube finished the upload but didn't return the video")
    return f"https://youtu.be/{vid}"


def upload_instagram(path: str, meta: dict, on_progress: Progress | None = None,
                     cancel: Callable[[], bool] | None = None) -> str:
    tok = access_token("instagram")
    ig = get_token("instagram").get("ig_id")
    size = os.path.getsize(path)
    _, _, c = _request("POST", URLS["graph"] + f"/{ig}/media", form={
        "media_type": "REELS", "upload_type": "resumable", "caption": meta.get("caption", "")[:2200],
        "share_to_feed": "true", "access_token": tok})
    cont = c.get("id")
    if not cont:
        raise PublishError("Instagram didn't create the upload")
    if on_progress:
        on_progress(0.05, "Instagram: uploading…")
    with open(path, "rb") as fh:
        data = fh.read()
    _, _, up = _request("POST", f"{URLS['rupload']}/{cont}", data=data, headers={
        "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)}, timeout=900)
    if up and up.get("success") is False:
        raise PublishError(f"Instagram refused the upload: {up}")
    for i in range(90):                              # processing: usually under 2 minutes
        if cancel and cancel():
            raise Cancelled()
        _, _, st = _request("GET", URLS["graph"] + f"/{cont}?" + urllib.parse.urlencode(
            {"fields": "status_code,status", "access_token": tok}))
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise PublishError(f"Instagram couldn't process the video: {st.get('status', code)}")
        if on_progress:
            on_progress(0.3 + 0.6 * min(1, i / 30), "Instagram: processing the video…")
        time.sleep(float(os.environ.get("RUMBLECUT_POLL", "5")))
    else:
        raise PublishError("Instagram is taking too long to process the video - check the app later")
    _, _, pub = _request("POST", URLS["graph"] + f"/{ig}/media_publish", form={"creation_id": cont, "access_token": tok})
    mid = pub.get("id")
    if not mid:
        raise PublishError("Instagram didn't publish the reel")
    try:
        _, _, m = _request("GET", URLS["graph"] + f"/{mid}?" + urllib.parse.urlencode(
            {"fields": "permalink", "access_token": tok}))
        return m.get("permalink") or "https://www.instagram.com/"
    except PublishError:
        return "https://www.instagram.com/"


def upload_tiktok(path: str, meta: dict, on_progress: Progress | None = None,
                  cancel: Callable[[], bool] | None = None) -> str:
    tok = access_token("tiktok")
    size = os.path.getsize(path)
    mb = 1024 * 1024
    if size <= 64 * mb:
        chunk, count = size, 1
    else:
        chunk = 10 * mb
        count = size // chunk                           # the last chunk takes the remainder
    _, _, r = _request("POST", URLS["tiktok_api"] + "/post/publish/inbox/video/init/", body={
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                        "total_chunk_count": count}}, headers=_bearer(tok))
    err = (r.get("error") or {})
    if err.get("code") not in (None, "ok"):
        raise PublishError(f"TikTok: {err.get('message') or err.get('code')}")
    data = r.get("data") or {}
    upload_url, publish_id = data.get("upload_url"), data.get("publish_id")
    if not upload_url:
        raise PublishError("TikTok didn't start the upload")
    with open(path, "rb") as fh:
        for k in range(count):
            if cancel and cancel():
                raise Cancelled()
            start = k * chunk
            n = size - start if k == count - 1 else chunk
            fh.seek(start)
            b = fh.read(n)
            _request("PUT", upload_url, data=b, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(b)),
                "Content-Range": f"bytes {start}-{start + len(b) - 1}/{size}"}, timeout=600, expect_json=False)
            if on_progress:
                on_progress((k + 1) / count * 0.9, f"TikTok: uploading… {(k + 1) * 100 // count}%")
    for _ in range(60):
        if cancel and cancel():
            raise Cancelled()
        _, _, st = _request("POST", URLS["tiktok_api"] + "/post/publish/status/fetch/",
                            body={"publish_id": publish_id}, headers=_bearer(tok))
        s = (st.get("data") or {}).get("status", "")
        if s in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            return "Sent to your TikTok inbox - open TikTok, add the caption and post it"
        if s == "FAILED":
            raise PublishError(f"TikTok couldn't take the video: {(st.get('data') or {}).get('fail_reason', 'failed')}")
        time.sleep(float(os.environ.get("RUMBLECUT_POLL", "3")))
    return "Uploaded - TikTok is still processing; it'll appear in your TikTok inbox"


def upload_x(path: str, meta: dict, on_progress: Progress | None = None,
             cancel: Callable[[], bool] | None = None) -> str:
    tok = access_token("x")
    size = os.path.getsize(path)
    _, _, r = _request("POST", URLS["x_api"] + "/media/upload/initialize", body={
        "media_type": "video/mp4", "total_bytes": size, "media_category": "tweet_video"}, headers=_bearer(tok))
    mid = (r.get("data") or {}).get("id")
    if not mid:
        raise PublishError("X didn't start the upload")
    chunk = 4 * 1024 * 1024
    total = max(1, -(-size // chunk))
    for i, b in enumerate(_read_chunks(path, chunk)):
        if cancel and cancel():
            raise Cancelled()
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"segment_index\"\r\n\r\n{i}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"media\"; filename=\"blob\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + b + f"\r\n--{boundary}--\r\n".encode()
        _request("POST", URLS["x_api"] + f"/media/upload/{mid}/append", data=body, headers={
            **_bearer(tok), "Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=600,
            expect_json=False)
        if on_progress:
            on_progress((i + 1) / total * 0.8, f"X: uploading… {(i + 1) * 100 // total}%")
    _, _, f = _request("POST", URLS["x_api"] + f"/media/upload/{mid}/finalize", headers=_bearer(tok))
    info = (f.get("data") or {}).get("processing_info")
    while info and info.get("state") not in ("succeeded", None):
        if info.get("state") == "failed":
            raise PublishError(f"X couldn't process the video: {(info.get('error') or {}).get('message', 'failed')}")
        if cancel and cancel():
            raise Cancelled()
        time.sleep(min(10.0, float(info.get("check_after_secs", 2))) * float(os.environ.get("RUMBLECUT_POLL_SCALE", "1")))
        _, _, s = _request("GET", URLS["x_api"] + "/media/upload?" + urllib.parse.urlencode(
            {"command": "STATUS", "media_id": mid}), headers=_bearer(tok))
        info = (s.get("data") or {}).get("processing_info")
        if on_progress:
            on_progress(0.9, "X: processing the video…")
    _, _, p = _request("POST", URLS["x_api"] + "/tweets", body={
        "text": meta.get("text", "")[:280], "media": {"media_ids": [mid]}}, headers=_bearer(tok))
    pid = (p.get("data") or {}).get("id")
    if not pid:
        raise PublishError("X didn't create the post")
    return f"https://x.com/i/status/{pid}"


UPLOADERS = {"youtube": upload_youtube, "instagram": upload_instagram, "tiktok": upload_tiktok, "x": upload_x}

# platform limits worth checking before rendering/uploading
MAX_SECONDS = {"youtube": 12 * 3600, "instagram": 15 * 60, "tiktok": 10 * 60, "x": 140}


# ============================================================================ formats
FORMATS = {"vertical": "Vertical 9:16 (blurred background)", "original": "Original"}


def default_format(platform: str, length: float) -> str:
    if platform in ("tiktok", "instagram"):
        return "vertical"
    if platform == "youtube":
        return "vertical" if length <= 180 else "original"          # ≤ 3 min vertical = a Short
    return "original"


def render_for(clip, fmt: str, out_dir: Path, on_progress: Progress | None = None,
               cancel: Callable[[], bool] | None = None) -> str:
    """Render the clip (with its effects and approved pictures) in the format a platform wants."""
    from . import editor
    out_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(json.dumps([asdict(clip), fmt], sort_keys=True, default=str).encode()).hexdigest()[:12]
    out = out_dir / f"{ft.safe_filename(clip.title or 'clip', 50)} [{fmt} {key}].mp4"
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    tmp = out.with_suffix(".part.mp4")
    s = editor.ExportSettings(aspect=FORMATS[fmt], height=1080, quality="high")
    editor.render_clip(clip, s, str(tmp), on_progress=(lambda f: on_progress(f, f"Rendering the {fmt} version…"))
                       if on_progress else None, cancel=cancel)
    os.replace(tmp, out)
    return str(out)


# ============================================================================ AI-written posts
POST_SYSTEM = ("You write social posts for short clips cut from a livestream. Be punchy and accurate - only "
               "claim what's actually said in the clip. No emojis spam (at most 1-2). Answer only with JSON.")

POST_PROMPT = """Stream: {stream}
Clip title: {title}
What is said in the clip:
\"\"\"
{text}
\"\"\"

Write a post for each platform:
{{"youtube": {{"title": "max 90 characters, curiosity-driving, no clickbait lies", "description": "2-3 short sentences + 3-5 hashtags on the last line", "tags": ["5-10 short tags"]}},
 "instagram": {{"caption": "1-2 lines that hook, then 5-10 hashtags"}},
 "tiktok": {{"caption": "one short hooky line + 3-6 hashtags (max 150 characters)"}},
 "x": {{"text": "max 240 characters including 1-2 hashtags"}}}}"""


def write_posts(clip_text: str, stream_title: str = "", clip_title: str = "") -> dict:
    if not clip_text.strip():
        raise PublishError("no speech was found in this clip, so there's nothing to write about")
    d = ai.complete_json(POST_PROMPT.format(stream=stream_title or "unknown", title=clip_title or "",
                                            text=clip_text[:5000]), POST_SYSTEM, max_tokens=900)
    yt = d.get("youtube") or {}
    out = {
        "youtube": {"title": str(yt.get("title", ""))[:100], "description": str(yt.get("description", ""))[:4900],
                    "tags": [str(t) for t in (yt.get("tags") or [])][:15]},
        "instagram": {"caption": str((d.get("instagram") or {}).get("caption", ""))[:2200]},
        "tiktok": {"caption": str((d.get("tiktok") or {}).get("caption", ""))[:2200]},
        "x": {"text": str((d.get("x") or {}).get("text", ""))[:280]},
    }
    if not any([out["youtube"]["title"], out["instagram"]["caption"], out["x"]["text"]]):
        raise PublishError("the AI didn't write anything usable - try again")
    return out


# ============================================================================ jobs + schedule queue
@dataclass
class PostJob:
    platform: str
    video: str                      # rendered file
    meta: dict                      # title / description / tags / caption / text / privacy
    clip_title: str = ""
    when: str = ""                  # ISO time (UTC) for scheduled posts, "" = now
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "waiting"         # waiting | posting | done | failed
    result: str = ""                # link or message
    error: str = ""
    finished_at: str = ""


def _queue_file() -> Path:
    return config.app_data_dir() / "publish_queue.json"


def load_queue() -> list[PostJob]:
    try:
        raw = json.loads(_queue_file().read_text(encoding="utf-8"))
        return [PostJob(**{k: v for k, v in x.items() if k in PostJob.__dataclass_fields__}) for x in raw]
    except Exception:
        return []


def save_queue(jobs: list[PostJob]) -> None:
    f = _queue_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(j) for j in jobs], indent=1), encoding="utf-8")
    os.replace(tmp, f)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def post(job: PostJob, on_progress: Progress | None = None, cancel: Callable[[], bool] | None = None) -> PostJob:
    """Upload one job now; records the result on the job (and doesn't raise for upload errors)."""
    job.status = "posting"
    try:
        if job.platform not in UPLOADERS:
            raise PublishError(f"unknown platform {job.platform}")
        if not os.path.exists(job.video):
            raise PublishError("the rendered video is missing - post it again from RumbleCut")
        job.result = UPLOADERS[job.platform](job.video, job.meta, on_progress, cancel)
        job.status, job.error = "done", ""
    except Cancelled:
        job.status, job.error = "failed", "cancelled"
        raise
    except Exception as e:
        job.status, job.error = "failed", str(e) or e.__class__.__name__
    job.finished_at = _now_iso()
    return job


def enqueue(job: PostJob) -> None:
    jobs = load_queue()
    jobs.append(job)
    save_queue(jobs)
    if job.when:
        schedule_wakeup(job.when)


def run_due(on_log: Callable[[str], None] | None = None) -> list[PostJob]:
    """Post every queued job whose time has come (used by the app's timer and --publish-due)."""
    lock = config.app_data_dir() / "publish.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 3600:
            return []                                   # another RumbleCut is posting
        lock.unlink(missing_ok=True)
        return run_due(on_log)
    done: list[PostJob] = []
    try:
        jobs = load_queue()
        now = datetime.now(timezone.utc)
        for j in jobs:
            if j.status != "waiting" or not j.when:
                continue
            try:
                due = datetime.fromisoformat(j.when)
            except ValueError:
                continue
            if due > now:
                continue
            if on_log:
                on_log(f"Posting “{j.clip_title}” to {PLATFORMS.get(j.platform, j.platform)}…")
            post(j)
            save_queue(jobs)
            done.append(j)
            if on_log:
                on_log(f"  → {j.result if j.status == 'done' else 'failed: ' + j.error}")
    finally:
        lock.unlink(missing_ok=True)
    return done


def schedule_wakeup(when_iso: str) -> bool:
    """Ask Windows to start `RumbleCut.exe --publish-due` at that time (works with RumbleCut closed)."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    try:
        when = datetime.fromisoformat(when_iso).astimezone()       # local time for the task
    except ValueError:
        return False
    name = f"RumbleCut\\Publish {when:%Y-%m-%d %H%M}"
    exe = sys.executable
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Posts clips scheduled in RumbleCut</Description></RegistrationInfo>
  <Triggers><TimeTrigger><StartBoundary>{when:%Y-%m-%dT%H:%M:%S}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Settings><StartWhenAvailable>true</StartWhenAvailable><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <DeleteExpiredTaskAfter>PT1H</DeleteExpiredTaskAfter></Settings>
  <Actions Context="Author"><Exec><Command>{exe}</Command><Arguments>--publish-due</Arguments></Exec></Actions>
</Task>"""
    # an expiring task needs an EndBoundary
    xml = xml.replace("<Enabled>true</Enabled></TimeTrigger>",
                      f"<EndBoundary>{when + timedelta(days=365):%Y-%m-%dT%H:%M:%S}</EndBoundary>"
                      "<Enabled>true</Enabled></TimeTrigger>")
    f = config.app_data_dir() / "publish_task.xml"
    f.write_text(xml, encoding="utf-16")
    r = subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", str(f), "/F"], capture_output=True,
                       text=True, creationflags=getattr(ft, "NO_WINDOW", 0))
    return r.returncode == 0
