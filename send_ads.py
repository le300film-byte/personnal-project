"""
Discord Marketplace Ad Sender  v5.2  (self-bot / alt account)
==============================================================
Sends ONE ad (SELL or BUY, chosen at workflow start) to marketplace channels
with human-like timing, browser-grade TLS/HTTP2 fingerprint (curl_cffi
impersonating Chrome), WebSocket gateway connection (real online presence),
cookie+fingerprint warmup, smart cooldown (only reposts when others have
posted after you), image EXIF strip + hash randomization, post-send typo
edits, DM forwarding to a webhook, and auto-learn (remembers which message
variations get blocked by anti-spam).

v5.2 additions:
  - DM forwarding to a private Discord webhook (username/avatar spoof,
    attachments, clickable "Open DM" deep link, forwards both sides)
  - Public-activity auto-pause when a buyer DMs (no posts/reactions/typing
    for DM_PAUSE_MINUTES to avoid simultaneous-action fingerprints)
  - Auto-learn blocked variations: post-send verification, strike-based
    blacklist persisted across runs via an optional GitHub Gist
  - Safety valve: if BLOCKED_SAFETY_STOP consecutive variations get
    deleted → account/IP is flagged → stop with exit code 2
  - WARP/proxy geo-country check (abort if outside ALLOWED_COUNTRIES)

v5.1 anti-detection stack (in order of impact):
  1. curl_cffi 'chrome' impersonation — real TLS/JA3/HTTP2 fingerprint
  2. WebSocket gateway connection — IDENTIFY + heartbeats + READY (online)
  3. Cookie/x-fingerprint warmup (GET /, /app, /experiments) pre-auth
  4. Per-channel Referer + X-Discord-Idempotency-Key + message nonce
  5. allowed_mentions (no @everyone/@here ping) + suppress_embeds sometimes
  6. Channel ACKs after reads (real clients mark channels as read)
  7. Startup bootup sequence (wait, browse, read channels before first post)
  8. Text-only warmup for first N posts (images trigger stronger anti-spam)
  9. Image: EXIF strip, filename random, JPEG quality jitter, tiny pixel jitter
  10. Variation engine: emojis, typos, casing, no-emoji versions, extra phrases
  11. Typing indicator with length-scaled duration + pre-thinking pause
  12. Occasional reactions to other users' messages (low rate)
  13. Occasional post-send "typo fix" edit (like a real user correcting themselves)
  14. AFK breaks (10-30 min, 2-4 per run) + random distraction pauses
  15. Outbound IP + country check on startup (warns on Azure/datacenter/geo-mismatch)
  16. Channel randomization order; inter-post "glance elsewhere" reads
  17. Proper 429 rate-limit handling (global cooldowns, bounded backoff)
  18. Ban detection (401/403 re-verify, exit code 2 to cancel whole workflow)
"""

import os
import sys
import time
import json
import random
import mimetypes
import base64
import re
import uuid
import io
import threading
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse, quote as urlquote

from curl_cffi import requests as creq
import curl_cffi

try:
    from PIL import Image, PngImagePlugin, ImageEnhance
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

try:
    import websocket as _ws
    _HAS_WS = True
except Exception:
    _HAS_WS = False

_SELF_TEST = "--self-test" in sys.argv
if _SELF_TEST:
    os.environ.setdefault("USER_TOKEN", "FAKE_TOKEN_FOR_SELF_TEST")
    os.environ.setdefault("CHANNEL_IDS", "000000000000000000,111111111111111111")
    os.environ.setdefault("AD_TYPE", "sell")
    os.environ.setdefault("MESSAGE", "SELLING BB LF 2.5$/1K DM ME QUICK")
    os.environ.setdefault("ATTACH_IMAGE", "no")

# --------------------------------------------------------------------------- #
# Logging                                                                     #
# --------------------------------------------------------------------------- #
def _ts():
    return datetime.now().strftime("%H:%M:%S")

def log(m):
    print(f"[{_ts()}] {m}", flush=True)

def dbg(m):
    if DEBUG:
        print(f"[{_ts()}] [DEBUG] {m}", flush=True)

def _env(name, default=""):
    return os.environ.get(name, "").strip() or default

def _required(name):
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"[{_ts()}] ❌ REQUIRED CONFIG MISSING: environment variable '{name}' is not set.", file=sys.stderr)
        print(f"         → Set it in your GitHub Actions workflow's `env:` block or as a repository secret.", file=sys.stderr)
        print(f"         → See SETUP_GUIDE.md for the full list of required variables.", file=sys.stderr)
        sys.exit(1)
    return v

def _int(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log(f"⚠️ CONFIG: '{name}'='{raw}' is not a valid integer, falling back to default {default}")
        return default

def _float(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log(f"⚠️ CONFIG: '{name}'='{raw}' is not a valid number, falling back to default {default}")
        return default

def _bool(name, default=False):
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "yes", "true", "on", "y")

def _list(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [x.strip().upper() for x in raw.split(",") if x.strip()]

# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
VERSION = "v5.2"
USER_TOKEN    = _required("USER_TOKEN")
CHANNEL_IDS   = [c.strip() for c in _required("CHANNEL_IDS").split(",") if c.strip()]
AD_TYPE       = _required("AD_TYPE").lower()
MESSAGE       = _required("MESSAGE")
ATTACH_IMAGE  = _bool("ATTACH_IMAGE", False)
INTERVAL_MIN  = _float("INTERVAL_MIN", 5)
TOTAL_RUN_MIN = _float("TOTAL_RUN_MIN", 360)
IMAGE_PATH    = _env("IMAGE_PATH")
CUSTOM_STATUS_TEXT = _env("CUSTOM_STATUS_TEXT", "Trading")
STATUS_EMOJI       = _env("STATUS_EMOJI", "💰")
MIN_AFK_BREAKS = _int("MIN_AFK_BREAKS", 2)
MAX_AFK_BREAKS = _int("MAX_AFK_BREAKS", 4)
AFK_MIN_MIN   = _float("AFK_MIN_MIN", 10)
AFK_MAX_MIN   = _float("AFK_MAX_MIN", 30)
DISCORD_LOCALE    = _env("DISCORD_LOCALE", "en-US")
DISCORD_TIMEZONE  = _env("DISCORD_TIMEZONE", "America/New_York")
HTTPS_PROXY       = _env("HTTPS_PROXY") or _env("HTTP_PROXY")
DEBUG             = _bool("DEBUG", False)

WARMUP_POSTS      = _int("WARMUP_POSTS", 3)
RANDOM_REACT      = _bool("RANDOM_REACT", True)
STRIP_EXIF        = _bool("STRIP_EXIF", True)
IDLE_REACT_CHANCE = _float("IDLE_REACT_CHANCE", 0.10)
PROXY_CHECK       = _bool("PROXY_CHECK", True)
ENABLE_GATEWAY    = _bool("ENABLE_GATEWAY", True)
TYPO_EDIT_CHANCE  = _float("TYPO_EDIT_CHANCE", 0.18)
SUPPRESS_EMBEDS   = _bool("SUPPRESS_EMBEDS", False)
IMAGE_JITTER      = _bool("IMAGE_JITTER", True)

# v5.2 new
DM_WEBHOOK_URL    = _env("DM_WEBHOOK_URL")
LOG_WEBHOOK_URL   = _env("LOG_WEBHOOK_URL")  # optional: separate webhook for plain action logs
DASHBOARD_WEBHOOK_URL = _env("DASHBOARD_WEBHOOK_URL")  # optional: periodic run-summary embeds
DM_PAUSE_MINUTES  = _float("DM_PAUSE_MINUTES", 2.0)
FORWARD_OWN_DMS   = _bool("FORWARD_OWN_DMS", True)
BLOCKED_STRIKES   = _int("BLOCKED_STRIKES", 2)
BLOCKED_SAFETY_STOP = _int("BLOCKED_SAFETY_STOP", 5)
GIST_TOKEN        = _env("GIST_TOKEN")
GIST_ID           = _env("GIST_ID")
ALLOWED_COUNTRIES = _list("ALLOWED_COUNTRIES", [])  # e.g. FR,ES,NL,DE,IE,GB,PT,MA,IT

if MIN_AFK_BREAKS < 0: MIN_AFK_BREAKS = 0
if MAX_AFK_BREAKS < MIN_AFK_BREAKS: MAX_AFK_BREAKS = MIN_AFK_BREAKS
if AFK_MIN_MIN < 1: AFK_MIN_MIN = 1
if AFK_MAX_MIN < AFK_MIN_MIN: AFK_MAX_MIN = AFK_MIN_MIN
if INTERVAL_MIN < 2:
    log(f"⚠️ CONFIG: INTERVAL_MIN={INTERVAL_MIN} is too aggressive (minimum safe interval is 2 min). Clamping to 2.")
    INTERVAL_MIN = 2
if DM_PAUSE_MINUTES < 0.5: DM_PAUSE_MINUTES = 0.5
if BLOCKED_STRIKES < 1: BLOCKED_STRIKES = 1
if BLOCKED_SAFETY_STOP < 2: BLOCKED_SAFETY_STOP = 2
if TOTAL_RUN_MIN < 5:
    log(f"⚠️ CONFIG: TOTAL_RUN_MIN={TOTAL_RUN_MIN} is too short (minimum safe runtime is 5 min). Clamping to 5.")
    TOTAL_RUN_MIN = 5
if TOTAL_RUN_MIN > 2880:  # 48h
    log(f"⚠️ CONFIG: TOTAL_RUN_MIN={TOTAL_RUN_MIN} exceeds the 48h safety cap. Clamping to 2880 min (48h).")
    TOTAL_RUN_MIN = 2880

if AD_TYPE not in ("sell", "buy"):
    log(f"❌ CONFIG ERROR: AD_TYPE must be 'sell' or 'buy', got '{AD_TYPE}'. Check workflow inputs / AD_TYPE env var.")
    sys.exit(1)

DISCORD_MSG_LIMIT = 2000
if len(MESSAGE) > DISCORD_MSG_LIMIT:
    log(f"❌ CONFIG ERROR: MESSAGE is {len(MESSAGE)} chars (Discord limit is {DISCORD_MSG_LIMIT}). Shorten your ad copy.")
    sys.exit(1)

if not CHANNEL_IDS:
    log("❌ CONFIG ERROR: No valid CHANNEL_IDS after parsing (empty list). Provide comma-separated channel IDs in the CHANNEL_IDS secret.")
    sys.exit(1)

# --------------------------------------------------------------------------- #
# Shared state between main + gateway thread                                  #
# --------------------------------------------------------------------------- #
_state_lock = threading.Lock()
_public_pause_until = 0.0          # epoch time — no public posts/reacts/typing until then
_dm_channel_cache = {}             # cid -> {username, avatar, id} cache for DMs
_blocked_variations = set()       # strings that have been strike-blacklisted
_strikes = defaultdict(int)       # variation_string -> strike count
_consecutive_deletions = 0        # how many DIFFERENT variations have been deleted back-to-back
_me_cache = {"id": None, "username": None, "avatar": None, "discriminator": None}
_stop_event = threading.Event()
_last_save_to_gist = 0.0
_dm_forward_failures = 0
_avatar_base = "https://cdn.discordapp.com"

def public_activity_allowed():
    """Return True if we are NOT in a buyer-DM pause."""
    with _state_lock:
        return time.time() >= _public_pause_until

def extend_dm_pause():
    """When a DM comes in, extend the public pause."""
    global _public_pause_until
    with _state_lock:
        new_until = time.time() + DM_PAUSE_MINUTES * 60
        if new_until > _public_pause_until:
            _public_pause_until = new_until
            log(f"⏸️  📥 BUYER DM DETECTED → Public activity PAUSED for {DM_PAUSE_MINUTES:.0f} min. Safe to reply; bot stays silent in public channels.")

def _sleep_chunked_respecting_pause(seconds, end_time=None):
    """Chunked sleep that returns early if we should not be doing public stuff.
    Returns True if we slept the full time, False if caller should back off."""
    if seconds <= 0:
        return True
    stop = time.time() + seconds
    while time.time() < stop:
        if end_time and time.time() >= end_time:
            return False
        if not public_activity_allowed():
            # In pause: just sleep without doing anything public
            wait = min(15, stop - time.time())
            time.sleep(wait)
            continue
        time.sleep(min(5, stop - time.time()))
    return True

# --------------------------------------------------------------------------- #
# Browser fingerprint                                                         #
# --------------------------------------------------------------------------- #
_BROWSER = "chrome"
_DEFAULT_BUILD = 387211
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
_CHROME_VERSION_FALLBACK = "140.0.0.0"

def _scrape_build_number_and_ua(session):
    """Scrape buildNumber from discord.com/app HTML (or its JS assets)."""
    global _UA
    try:
        r = session.get("https://discord.com/app", timeout=15)
        sent_ua = r.request.headers.get("User-Agent", "") if r.request else ""
        if sent_ua and "Chrome/" in sent_ua:
            _UA = sent_ua
            m = re.search(r"Chrome/(\d+[\d.]+)", sent_ua)
            cv = m.group(1) if m else _CHROME_VERSION_FALLBACK
        else:
            cv = _CHROME_VERSION_FALLBACK
        mb = re.search(r'"buildNumber"\s*:\s*(\d{5,})', r.text)
        if mb:
            return int(mb.group(1)), cv
        scripts = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for s in scripts[:5]:
            try:
                rr = session.get(f"https://discord.com{s}", timeout=12)
                mb = re.search(r'buildNumber["\s:=:]+(\d{5,})', rr.text)
                if mb:
                    return int(mb.group(1)), cv
            except Exception:
                continue
        return _DEFAULT_BUILD, cv
    except Exception:
        return _DEFAULT_BUILD, _CHROME_VERSION_FALLBACK

# --------------------------------------------------------------------------- #
# Session                                                                     #
# --------------------------------------------------------------------------- #
def _build_session():
    proxy_map = {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None
    return creq.Session(impersonate=_BROWSER, proxies=proxy_map)

SESSION = _build_session()

# --------------------------------------------------------------------------- #
# Warmup: cookies + fingerprint + super-props                                 #
# --------------------------------------------------------------------------- #
_X_FINGERPRINT = None
CLIENT_BUILD = _DEFAULT_BUILD
_CHROME_VER = _CHROME_VERSION_FALLBACK

def _warmup_fingerprint():
    global _X_FINGERPRINT, _UA, CLIENT_BUILD, _CHROME_VER
    log("🔑 Warming up browser session (cookies + X-Fingerprint + X-Super-Properties)...")
    try:
        log("   ℹ️  GET discord.com/ (landing page, sets initial cookies)...")
        SESSION.get("https://discord.com/", timeout=15)
        time.sleep(random.uniform(0.5, 1.2))
        log("   ℹ️  GET discord.com/app (app shell, scrapes build number)...")
        r = SESSION.get("https://discord.com/app", timeout=15)
        time.sleep(random.uniform(0.6, 1.3))
        CLIENT_BUILD, _CHROME_VER = _scrape_build_number_and_ua(SESSION)
        log("   ℹ️  GET /api/v9/experiments (fetches X-Fingerprint token)...")
        r2 = SESSION.get("https://discord.com/api/v9/experiments", timeout=10)
        if r2.status_code == 200:
            try:
                _X_FINGERPRINT = r2.json().get("fingerprint")
                log(f"   ✅ experiments endpoint → fingerprint = {(_X_FINGERPRINT[:16] + '…') if _X_FINGERPRINT else 'NONE'}")
            except Exception:
                log("   ⚠️ experiments endpoint returned non-JSON — continuing without fingerprint")
        else:
            log(f"   ⚠️ experiments endpoint returned HTTP {r2.status_code} — continuing without fingerprint")
        try:
            log("   ℹ️  POST /api/v9/science (telemetry ping, makes us look like a real client)...")
            SESSION.post("https://discord.com/api/v9/science",
                         json={"events": [], "client_track_timestamp": int(time.time()*1000)},
                         timeout=5)
            log("   ✅ science telemetry sent")
        except Exception:
            log("   ℹ️  science ping skipped (non-critical)")
        has_locale = any(c.name == "locale" for c in SESSION.cookies.jar)
        if not has_locale:
            try:
                SESSION.cookies.set("locale", DISCORD_LOCALE, domain="discord.com")
                log(f"   ✅ locale cookie set to {DISCORD_LOCALE}")
            except Exception:
                log("   ⚠️ could not set locale cookie — continuing anyway")
        else:
            log(f"   ✅ locale cookie already present ({DISCORD_LOCALE})")
    except Exception as e:
        log(f"   ⚠️ Warmup error ({type(e).__name__}: {e}) -- continuing with default browser profile")
        CLIENT_BUILD, _CHROME_VER = _DEFAULT_BUILD, _CHROME_VERSION_FALLBACK

    cv_major = _CHROME_VER.split(".")[0]
    super_props = {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": DISCORD_LOCALE,
        "browser_user_agent": _UA,
        "browser_version": _CHROME_VER,
        "os_version": "10",
        "referrer": "",
        "referring_domain": "",
        "referrer_current": "",
        "referring_domain_current": "",
        "release_channel": "stable",
        "client_build_number": CLIENT_BUILD,
        "client_event_source": None,
        "design_id": 0,
    }
    sp_b64 = base64.b64encode(json.dumps(super_props, separators=(",", ":")).encode()).decode()
    headers = {
        "Authorization": USER_TOKEN,
        "Accept": "*/*",
        "Accept-Language": f"{DISCORD_LOCALE},en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/channels/@me",
        "X-Super-Properties": sp_b64,
        "X-Debug-Options": "bugReporterEnabled",
        "X-Discord-Locale": DISCORD_LOCALE,
        "X-Discord-Timezone": DISCORD_TIMEZONE,
    }
    if _X_FINGERPRINT:
        headers["X-Fingerprint"] = _X_FINGERPRINT
    SESSION.headers.update(headers)
    log("   ─────────────────────────────────────")
    log(f"   🌐 UA         : Chrome {cv_major}")
    log(f"   🏗️  Build      : {CLIENT_BUILD}")
    log(f"   🔐 Fingerprint: {'OK' if _X_FINGERPRINT else 'NOT RECEIVED'}")
    log(f"   🍪 Cookies    : {len(list(SESSION.cookies))}")
    log(f"   🌍 Locale/TZ  : {DISCORD_LOCALE} / {DISCORD_TIMEZONE}")
    log("   ✅ Browser fingerprint ready — all subsequent requests will use these headers.")

# --------------------------------------------------------------------------- #
# API helpers                                                                 #
# --------------------------------------------------------------------------- #
_global_cooldown_until = 0.0

def sleep_chunked(seconds, end_time=None):
    if seconds <= 0:
        return
    stop = time.time() + seconds
    while time.time() < stop:
        if end_time and time.time() >= end_time:
            return
        time.sleep(min(5, stop - time.time()))

def _apply_global_cooldown():
    global _global_cooldown_until
    # Sleep in short chunks (≤30s) so that a long global rate-limit
    # (e.g. retry_after=3600) doesn't freeze the main thread for an hour,
    # and so that KeyboardInterrupt / SystemExit can be delivered promptly.
    while True:
        now = time.time()
        remaining = _global_cooldown_until - now
        if remaining <= 0:
            return
        wait = min(remaining + random.uniform(0.5, 2.0), 30.0)
        dbg(f"   ⏳ Global cooldown {wait:.1f}s (remaining ~{remaining:.0f}s)")
        time.sleep(wait)

def _make_nonce():
    DISCORD_EPOCH = 1420070400000
    ts = int(time.time() * 1000) - DISCORD_EPOCH
    incr = random.randint(0, 0xFFF)
    worker = random.randint(0, 0x1F)
    pid = random.randint(0, 0x1F)
    return str((ts << 22) | (worker << 17) | (pid << 12) | incr)

def api(method, url, retries=3, referer=None, files_mp=None, json_body=None,
        data=None, extra_headers=None):
    global _global_cooldown_until
    _429_streak = 0
    headers = {}
    # Idempotency key: send on POST /messages (new message) and PUT reactions.
    # Do NOT send on ACK, typing, reactions POST, or PATCH.
    if method.upper() == "POST" and url.rstrip("/").endswith("/messages"):
        headers["X-Discord-Idempotency-Key"] = uuid.uuid4().hex
    if method.upper() == "PUT" and "/reactions/" in url:
        headers["X-Discord-Idempotency-Key"] = uuid.uuid4().hex
    if referer:
        headers["Referer"] = referer
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body, separators=(",", ":")).encode()
    if extra_headers:
        headers.update(extra_headers)

    for attempt in range(1, retries + 1):
        _apply_global_cooldown()
        try:
            r = SESSION.request(
                method, url,
                data=data if files_mp is None else None,
                multipart=files_mp,
                headers=headers if headers else None,
                timeout=30,
            )
            is_multipart = files_mp is not None
            if is_multipart:
                try:
                    files_mp.close()
                except Exception:
                    pass
                files_mp = None
        except Exception as e:
            short = url.split("/api/")[-1][:60] if "/api/" in url else url[-40:]
            log(f"   🔄 NETWORK ERROR ({method} {short}): {type(e).__name__} (attempt {attempt}/{retries})")
            if attempt < retries:
                backoff = 3 * attempt + random.uniform(0, 1)
                dbg(f"      retrying in {backoff:.1f}s...")
                time.sleep(backoff)
                continue
            log(f"   ❌ NETWORK ERROR: {method} {short} failed after {retries} attempts ({type(e).__name__})")
            return _fake_err_response(0, str(e))

        if r.status_code == 429:
            _429_streak += 1
            try:
                d = r.json()
            except Exception:
                d = {}
            raw_wait = d.get("retry_after", 8)
            try:
                raw_wait = float(raw_wait)
            except (TypeError, ValueError):
                raw_wait = 8.0
            is_global = bool(d.get("global"))
            this_wait = min(raw_wait, 600) + random.uniform(1, 3)
            scope = "GLOBAL (all requests paused)" if is_global else f"bucket {d.get('bucket','?')}"
            log(f"   ⏳ RATE LIMITED ({scope}) → waiting {this_wait:.1f}s [streak {_429_streak}/6]")
            if is_global:
                _global_cooldown_until = max(_global_cooldown_until, time.time() + raw_wait)
                log(f"   ℹ️  Global cooldown set for {raw_wait:.0f}s — all requests paused until window clears.")
            if _429_streak >= 6:
                log("   ❌ RATE LIMIT: Too many consecutive 429s ({_429_streak}). Backing off to avoid ban.".format(_429_streak=_429_streak))
                return r
            time.sleep(this_wait)
            continue

        if 500 <= r.status_code < 600 and attempt < retries:
            # NOTE: do NOT retry multipart uploads — files_mp stream was
            # already consumed/closed after the first send, so a retry would
            # post the JSON payload with NO image (text-only duplicate ad).
            # Real browsers don't transparently retry multipart POSTs either.
            if files_mp is not None:
                dbg(f"[API] 5xx on multipart upload (HTTP {r.status_code}), NOT retrying (would create text-only duplicate)")
                if files_mp is not None:
                    try: files_mp.close()
                    except Exception: pass
                return r
            backoff = 3 * attempt + random.uniform(0, 2)
            log(f"   🔄 DISCORD SERVER ERROR {r.status_code} (attempt {attempt}/{retries}) → retrying in {backoff:.1f}s")
            time.sleep(backoff)
            continue

        if files_mp is not None:
            try:
                files_mp.close()
            except Exception:
                pass
        return r
    return _fake_err_response(0, "max retries exceeded")

def _fake_err_response(code, msg):
    r = creq.Response()
    r.status_code = code
    r._content = msg.encode() if isinstance(msg, str) else msg
    return r

# --------------------------------------------------------------------------- #
# Webhook (DM forwarding)                                                     #
# --------------------------------------------------------------------------- #
def _avatar_url(user):
    """Build Discord CDN avatar URL for a user."""
    uid = user.get("id")
    av = user.get("avatar")
    if av:
        ext = "gif" if av.startswith("a_") else "png"
        return f"{_avatar_base}/avatars/{uid}/{av}.{ext}?size=256"
    disc = user.get("discriminator") or "0"
    try:
        idx = int(disc) % 5
    except Exception:
        idx = 0
    return f"{_avatar_base}/embed/avatars/{idx}.png"

def send_webhook(content, username=None, avatar_url=None, embed=None, embeds=None):
    """Send a single message to the configured DM webhook."""
    global _dm_forward_failures
    if not DM_WEBHOOK_URL:
        return True
    if _dm_forward_failures >= 5:
        dbg("webhook: too many failures, dropping")
        return False
    payload = {}
    if content:
        payload["content"] = content
    if username:
        payload["username"] = username[:80]
    if avatar_url:
        payload["avatar_url"] = avatar_url
    if embed is not None:
        payload["embeds"] = [embed]
    elif embeds is not None:
        payload["embeds"] = embeds
    if not payload.get("content") and not payload.get("embeds"):
        return True
    try:
        # Route webhook POSTs through the same proxy (if set) so the
        # outbound IP is consistent with the rest of the bot. Use a
        # throwaway session (no Discord auth cookies) — webhooks use
        # their own URL token.
        wh_proxies = {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None
        r = None
        for attempt in range(3):
            try:
                r = creq.post(DM_WEBHOOK_URL + "?wait=true",
                              json=payload, impersonate=_BROWSER, timeout=15,
                              proxies=wh_proxies)
                if r.status_code in (200, 204):
                    _dm_forward_failures = 0
                    return True
                if 500 <= r.status_code < 600:
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            except Exception as inner:
                dbg(f"webhook attempt {attempt+1} err: {type(inner).__name__}")
                time.sleep(2 * (attempt + 1))
        dbg(f"webhook failed ({getattr(r, 'status_code', '?')}): {getattr(r,'text','')[:200]}")
        _dm_forward_failures += 1
        return False
    except Exception as e:
        dbg(f"webhook exception: {type(e).__name__}: {e}")
        _dm_forward_failures += 1
        return False

# --------------------------------------------------------------------------- #
# Log webhook (optional — plain text action log to a separate Discord channel)#
# --------------------------------------------------------------------------- #
_log_webhook_failures = 0

def send_log_webhook(msg):
    """Send a single plain-text timestamped line to LOG_WEBHOOK_URL (if set).

    Used for action log events (startup, sends, failures, DMs, finish).
    Completely optional and fire-and-forget via a daemon thread — webhook
    latency never blocks the main posting loop. Failures never crash the bot.
    """
    global _log_webhook_failures
    if not LOG_WEBHOOK_URL:
        return
    if _log_webhook_failures >= 5:
        return  # stop trying after repeated failures
    line = f"`[{_ts()}]` {msg}"

    def _send():
        global _log_webhook_failures
        try:
            wh_proxies = {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None
            r = creq.post(LOG_WEBHOOK_URL + "?wait=true",
                          json={"content": line[:2000]},
                          impersonate=_BROWSER, timeout=20,
                          proxies=wh_proxies)
            if r.status_code in (200, 204):
                _log_webhook_failures = 0
            elif not (500 <= r.status_code < 600):
                dbg(f"[LOG-WEBHOOK] failed (HTTP {r.status_code}): {getattr(r,'text','')[:120]}")
                _log_webhook_failures += 1
        except Exception as e:
            dbg(f"[LOG-WEBHOOK] exception: {type(e).__name__}: {e}")
            _log_webhook_failures += 1
    threading.Thread(target=_send, daemon=True).start()

# --------------------------------------------------------------------------- #
# Dashboard webhook (optional — periodic run summaries as a Discord embed)    #
# --------------------------------------------------------------------------- #
_dash_webhook_failures = 0
_last_dash_summary = 0.0  # epoch of last dashboard summary push
_dash_lock = threading.Lock()

def send_dashboard(embed_dict):
    """Send a single embed to DASHBOARD_WEBHOOK_URL (if set).

    Thread-safe: can be called from any thread (main or daemon verification).
    Failures never crash the bot and self-throttle after 5 consecutive errors.
    """
    global _dash_webhook_failures
    if not DASHBOARD_WEBHOOK_URL:
        return False
    if _dash_webhook_failures >= 5:
        return False
    try:
        wh_proxies = {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None
        payload = {
            "username": "Ad-Bot Dashboard",
            "embeds": [embed_dict],
        }
        # Run in a daemon thread so it never blocks the main loop
        def _send():
            global _dash_webhook_failures
            try:
                r = creq.post(DASHBOARD_WEBHOOK_URL + "?wait=true",
                              json=payload, impersonate=_BROWSER, timeout=20,
                              proxies=wh_proxies)
                if r.status_code in (200, 204):
                    _dash_webhook_failures = 0
                elif r.status_code not in (429,) and not (500 <= r.status_code < 600):
                    dbg(f"[DASH-WEBHOOK] failed (HTTP {r.status_code})")
                    _dash_webhook_failures += 1
            except Exception as e:
                dbg(f"[DASH-WEBHOOK] exception: {type(e).__name__}: {e}")
                _dash_webhook_failures += 1
        threading.Thread(target=_send, daemon=True).start()
        return True
    except Exception as e:
        dbg(f"[DASH-WEBHOOK] spawn error: {type(e).__name__}: {e}")
        return False

def _dashboard_startup_embed(version, ad_type, ch_list, interval_min, runtime_min, variants, use_img, total_channels, active_count):
    """Build the startup dashboard embed."""
    return {
        "title": f"🟢 STARTED {version}",
        "color": 0x57F287,  # green
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [
            {"name": "Mode", "value": f"`{ad_type}`", "inline": True},
            {"name": "Interval", "value": f"~{interval_min} min/ch (±jitter)", "inline": True},
            {"name": "Runtime", "value": f"{runtime_min:.0f} min ({runtime_min/60:.1f}h)", "inline": True},
            {"name": "Channels", "value": ch_list or "—", "inline": False},
            {"name": "Active / Total", "value": f"{active_count} / {total_channels}", "inline": True},
            {"name": "Variations", "value": str(variants), "inline": True},
            {"name": "Image", "value": "ON (after warmup)" if use_img else "OFF (text-only)", "inline": True},
        ],
    }

def _dashboard_cycle_embed(cycle, elapsed_min, sent, img_attach, txt_only, edits, errs, skips, per_ch, active_count, total_channels, active_channels_set, ch_names_dict, slowmodes_dict, last_sent_dict, my_last_id_dict, in_afk_flag=False, afk_left=0.0, is_shutdown=False):
    """Build per-cycle / shutdown dashboard embed."""
    color = 0xED4245 if (errs > 0 or is_shutdown) else 0x5865F2  # red/blue
    title = f"🏁 SHUTDOWN summary" if is_shutdown else f"📊 Cycle {cycle}"
    lines = []
    for cid in CHANNEL_IDS:
        name = ch_names_dict.get(cid, cid)
        s = per_ch[cid]
        alive = "✅" if cid in active_channels_set else "⛔"
        last_ts = last_sent_dict.get(cid)
        if last_ts:
            last_str = datetime.fromtimestamp(last_ts).strftime("%H:%M:%S")
        else:
            last_str = "—"
        lines.append(
            f"{alive} **#{name}** `{cid}`\n"
            f"   ↳ sent:{s['sent']} (💬{s['txt']}/📷{s['img']}/✏️{s['edits']})  "
            f"err:{s['errors']}  last:{last_str}"
        )
    ch_breakdown = "\n".join(lines) if lines else "—"
    afk_str = f"☕ AFK — {afk_left/60:.1f}m remaining" if in_afk_flag else "active"
    fields = [
        {"name": "Uptime", "value": f"{elapsed_min:.1f} min ({elapsed_min/60:.2f}h)", "inline": True},
        {"name": "Total sent", "value": f"**{sent}**  (📷{img_attach} / 💬{txt_only})", "inline": True},
        {"name": "✏️ Edits", "value": str(edits), "inline": True},
        {"name": "❌ Errors", "value": str(errs), "inline": True},
        {"name": "⏭️ Skips", "value": str(skips), "inline": True},
        {"name": "Channels (active/total)", "value": f"{active_count} / {total_channels}", "inline": True},
        {"name": "Status", "value": afk_str, "inline": False},
        {"name": "Per-channel", "value": ch_breakdown[:1000] or "—", "inline": False},
    ]
    return {
        "title": title,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": fields,
    }

def _format_attachments(attachments):
    """Return a string listing attachment URLs for forwarding."""
    if not attachments:
        return ""
    lines = []
    for a in attachments:
        url = a.get("url", "")
        fn = a.get("filename", "attachment")
        size = a.get("size", 0)
        if a.get("content_type", "").startswith("image/"):
            lines.append(f"🖼️ [{fn}]({url})")
        else:
            mb = size / (1024*1024) if size else 0
            size_str = f" ({mb:.1f}MB)" if mb else ""
            lines.append(f"📎 [{fn}]({url}){size_str}")
    return "\n".join(lines)

def forward_dm_message(channel_id, user_obj, content, attachments, is_me=False):
    """Forward a DM (one side of the conversation) to the webhook."""
    if not DM_WEBHOOK_URL:
        return
    uname = user_obj.get("username") or "unknown"
    if is_me:
        uname = f"{uname} (alt)"
    av = _avatar_url(user_obj)
    att_text = _format_attachments(attachments)
    body = content or ""
    if att_text:
        body = (body + "\n" + att_text).strip()
    # Discord deep link — opens the DM channel directly
    deep_link = f"https://discord.com/channels/@me/{channel_id}"
    embed = {
        "type": "rich",
        "color": 0x2F3136 if is_me else 0x57F287,  # grey for us, green for buyer
        "footer": {"text": "Open DM", "icon_url": av},
        "url": deep_link,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if not body:
        body = "*(empty — embed/attachment only)*"
    send_webhook(body[:2000], username=uname[:80], avatar_url=av, embed=embed)

# --------------------------------------------------------------------------- #
# Blocklist Gist persistence                                                  #
# --------------------------------------------------------------------------- #
_GIST_FILENAME = "blocked_variations.json"

def load_blocked_from_gist():
    if not GIST_TOKEN or not GIST_ID:
        dbg("[GIST] No GIST_TOKEN/GIST_ID configured — starting with fresh (empty) blocklist")
        return
    log(f"📚 Loading auto-learn blocklist from gist {GIST_ID[:8]}...")
    try:
        r = creq.get(f"https://api.github.com/gists/{GIST_ID}",
                     headers={"Authorization": f"token {GIST_TOKEN}",
                              "Accept": "application/vnd.github+json",
                              "User-Agent": "discord-ad-sender"},
                     impersonate=_BROWSER, timeout=15)
        if r.status_code != 200:
            log(f"⚠️ Could not fetch gist ({r.status_code}) — starting with empty blocklist")
            return
        j = r.json()
        file_info = j.get("files", {}).get(_GIST_FILENAME)
        if not file_info:
            log("   (no blocklist file in gist yet, will create on first save)")
            return
        raw = file_info.get("content") or ""
        data = json.loads(raw)
        loaded = set(data.get("blocked", []))
        with _state_lock:
            _blocked_variations.update(loaded)
        log(f"📚 Loaded {len(loaded)} blocked variations from gist")
    except Exception as e:
        log(f"⚠️ Failed to load gist blocklist: {type(e).__name__}: {e}")

def save_blocked_to_gist(force=False):
    global _last_save_to_gist
    if not GIST_TOKEN or not GIST_ID:
        return False
    if not force and (time.time() - _last_save_to_gist) < 300:
        return False  # throttle to every 5 min
    try:
        with _state_lock:
            snapshot = list(_blocked_variations)
        payload = {
            "files": {
                _GIST_FILENAME: {
                    "content": json.dumps({
                        "version": VERSION,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "count": len(snapshot),
                        "blocked": snapshot,
                    }, indent=2, ensure_ascii=False)
                }
            }
        }
        r = creq.patch(f"https://api.github.com/gists/{GIST_ID}",
                      headers={"Authorization": f"token {GIST_TOKEN}",
                               "Accept": "application/vnd.github+json",
                               "User-Agent": "discord-ad-sender"},
                      data=json.dumps(payload),
                      impersonate=_BROWSER, timeout=15)
        if r.status_code in (200, 201):
            _last_save_to_gist = time.time()
            dbg(f"saved {len(snapshot)} blocked variations to gist")
            return True
        dbg(f"gist save failed ({r.status_code}): {getattr(r,'text','')[:200]}")
        return False
    except Exception as e:
        dbg(f"gist save exception: {e}")
        return False

def _blacklist_variation(text):
    """Add a variation to the blocklist (thread-safe, persist to gist)."""
    with _state_lock:
        if text in _blocked_variations:
            return
        _blocked_variations.add(text)
    snip = text.replace("\n", " ⏎ ")[:60]
    log(f"   🚫 Blacklisted variation: \"{snip}{'...' if len(text) > 60 else ''}\"")
    save_blocked_to_gist()

def _record_strike(text, cid, mid):
    """Record a strike for a variation. If strikes >= BLOCKED_STRIKES, blacklist.

    NOTE: this can be called from a background daemon thread (post-send
    verification), so for the safety stop we use os._exit() — threading.Thread
    swallows SystemExit raised in a child thread, so sys.exit() there only
    kills the verification thread and the bot keeps running.
    """
    global _consecutive_deletions
    with _state_lock:
        _strikes[text] += 1
        n = _strikes[text]
        _consecutive_deletions += 1
        consec = _consecutive_deletions
    if n >= BLOCKED_STRIKES:
        _blacklist_variation(text)
    else:
        log(f"   ⚠️ Strike {n}/{BLOCKED_STRIKES} for this variation")
    if consec >= BLOCKED_SAFETY_STOP:
        log("")
        log(f"🛑 SAFETY STOP: {consec} different variations deleted in a row.")
        log("   This means the ANTI-SPAM IS DELETING EVERYTHING — the account/IP is")
        log("   flagged, not the text. Stopping to avoid burning the alt further.")
        try:
            save_blocked_to_gist(force=True)
        except Exception:
            pass
        log("   Cancel the run, age the alt more (24h+), switch proxy/IP, and retry.")
        send_log_webhook(
            f"🛑 **SAFETY STOP** `{consec}` consecutive deletions — account/IP flagged. Aborting."
        )
        # Use os._exit, not sys.exit: this path can be reached from a daemon
        # verification thread, and SystemExit in a child thread only kills
        # that thread, leaving the bot running blind.
        os._exit(2)

def _reset_consecutive_deletions():
    """Call when a post survives verification."""
    global _consecutive_deletions
    with _state_lock:
        _consecutive_deletions = 0

# --------------------------------------------------------------------------- #
# Post-send verification (auto-learn)                                         #
# --------------------------------------------------------------------------- #
def _verify_message_alive(cid, mid, text, delay=35):
    """Background: wait `delay` seconds, fetch the message. If gone, strike it."""
    def _run():
        time.sleep(delay + random.uniform(-3, 8))
        try:
            ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
            r = SESSION.get(f"https://discord.com/api/v9/channels/{cid}/messages/{mid}",
                            referer=ref, timeout=10)
            if r.status_code == 200:
                # survived
                _reset_consecutive_deletions()
                dbg(f"post survived (cid={cid}, mid={mid})")
            elif r.status_code in (404, 403):
                _record_strike(text, cid, mid)
            else:
                dbg(f"verify got {r.status_code}, ignoring")
        except Exception as e:
            dbg(f"verify exception: {e}")
    t = threading.Thread(target=_run, daemon=True)
    t.start()

# --------------------------------------------------------------------------- #
# Image processing                                                            #
# --------------------------------------------------------------------------- #
_IMG_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp"}

def _random_img_name(original_name):
    ext = Path(original_name).suffix.lower() or ".png"
    bases = ["image", "img", "pic", "photo", "ss", "Screenshot", "trade",
             "ad", "IMG", "Image", "screenshot", "Capture", "shot"]
    return f"{random.choice(bases)}_{random.randint(1000,99999)}{ext}"

def _process_image(raw_bytes, original_name):
    ext = Path(original_name).suffix.lower() or ".png"
    mime = _IMG_EXTS.get(ext, "image/png")
    fname = _random_img_name(original_name)
    if not _HAS_PIL or not STRIP_EXIF:
        return fname, raw_bytes, mime
    try:
        im = Image.open(io.BytesIO(raw_bytes))
        out = io.BytesIO()
        if IMAGE_JITTER and ext != ".gif":
            try:
                im = im.convert("RGB") if ext in (".jpg", ".jpeg") else im
                w, h = im.size
                n_jitter = min(30, (w * h) // 5000)
                px = im.load()
                for _ in range(n_jitter):
                    x = random.randint(0, w - 1)
                    y = random.randint(0, h - 1)
                    try:
                        p = px[x, y]
                        if isinstance(p, tuple):
                            jittered = tuple(max(0, min(255, c + random.randint(-1, 1))) for c in p[:3]) + p[3:]
                            px[x, y] = jittered
                    except Exception:
                        pass
            except Exception:
                pass
        if ext in (".jpg", ".jpeg"):
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            q = random.randint(90, 96)
            im.save(out, format="JPEG", quality=q, optimize=True,
                    subsampling="4:2:0" if q < 95 else 0)
            fname = fname.rsplit(".", 1)[0] + ".jpg"
            mime = "image/jpeg"
        elif ext == ".webp":
            im.save(out, format="WEBP", quality=random.randint(90, 96))
        elif ext == ".gif":
            return fname, raw_bytes, mime
        else:
            info = PngImagePlugin.PngInfo()
            im.save(out, format="PNG", pnginfo=info, optimize=True)
        out.seek(0)
        data = out.read()
        if len(data) > len(raw_bytes) * 1.08:
            return fname, raw_bytes, mime
        return fname, data, mime
    except Exception as e:
        dbg(f"image processing failed: {e}; using raw")
        return fname, raw_bytes, mime

# --------------------------------------------------------------------------- #
# Discord API wrappers                                                        #
# --------------------------------------------------------------------------- #
_guild_id_cache = {}

def validate_token():
    log("🔐 Authenticating with Discord (GET /users/@me)...")
    r = api("GET", "https://discord.com/api/v9/users/@me", retries=2)
    if r.status_code == 200:
        try:
            me = r.json()
            # cache my identity for webhook spoofing
            _me_cache["id"] = me.get("id")
            _me_cache["username"] = me.get("username")
            _me_cache["avatar"] = me.get("avatar")
            _me_cache["discriminator"] = me.get("discriminator")
            return me, None
        except Exception:
            return None, "unknown"
    try:
        msg = r.json().get("message", "")[:200]
    except Exception:
        msg = (getattr(r, "text", "") or "")[:200]
    if r.status_code in (401, 403):
        reason = "invalid"
    elif r.status_code == 0:
        reason = "network"
    elif 500 <= r.status_code < 600:
        reason = "server"
    else:
        reason = "unknown"
    log(f"❌ AUTH FAILED — status {r.status_code} ({reason}): {msg}")
    if reason == "invalid":
        log("   → Token is invalid/revoked/banned. Recopy your token or use a new alt.")
    elif reason == "network":
        log("   → Network error during auth. Check proxy/WARP connection.")
    return None, reason

def set_status():
    if not CUSTOM_STATUS_TEXT:
        log("ℹ️  No custom status configured; skipping presence update.")
        return False
    payload = {"custom_status": {"text": CUSTOM_STATUS_TEXT}}
    if STATUS_EMOJI:
        payload["custom_status"]["emoji_name"] = STATUS_EMOJI
    try:
        r = api("PATCH", "https://discord.com/api/v9/users/@me/settings",
                json_body=payload, retries=2)
        if r.status_code == 200:
            log(f"🟢 Custom status set → '{STATUS_EMOJI} {CUSTOM_STATUS_TEXT}'")
            return True
        log(f"⚠️ Failed to set custom status (HTTP {r.status_code}) — non-critical, continuing.")
    except Exception as e:
        log(f"⚠️ Status error: {type(e).__name__}: {e}")
    return False

def keepalive():
    try:
        api("GET", "https://discord.com/api/v9/users/@me", retries=1)
        dbg("[KEEPALIVE] 💓 REST keepalive ping sent (maintains NAT mapping + keeps auth fresh)")
    except Exception:
        dbg("[KEEPALIVE] ⚠️ REST keepalive failed (non-critical)")

def get_channel_info(cid):
    ref = f"https://discord.com/channels/@me/{cid}"
    r = api("GET", f"https://discord.com/api/v9/channels/{cid}", referer=ref, retries=2)
    try:
        if r.status_code == 200:
            j = r.json()
            gid = j.get("guild_id")
            if gid:
                _guild_id_cache[cid] = gid
            return j
    except Exception:
        pass
    return None

def ack_channel(cid, last_msg_id):
    if not last_msg_id:
        return
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    try:
        api("POST",
            f"https://discord.com/api/v9/channels/{cid}/messages/{last_msg_id}/ack",
            referer=ref, json_body={"token": None}, retries=1)
        dbg(f"✔️ ack #{cid} @ {last_msg_id}")
    except Exception:
        pass

def get_last_messages(cid, limit=5, force_refresh=False):
    url = f"https://discord.com/api/v9/channels/{cid}/messages?limit={limit}"
    if force_refresh:
        url += f"&_={int(time.time()*1000)}"
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    r = api("GET", url, referer=ref, retries=2)
    try:
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return None

def am_i_last(cid, my_id):
    """Return (i_am_last, last_author, last_snip, recent_msgs).

    NOTE: In high-traffic channels (30-50 msgs/min) we are almost NEVER the
    last message by the time we check. We only use this for the *optional*
    smart-cooldown skip (in low-traffic channels). It is NOT used to decide
    WHEN to post — that's the per-channel scheduler's job.

    We fetch 20 recent messages (not 5) to reduce false "DELETED" alarms in
    busy channels where our ad is simply buried quickly.
    """
    msgs = get_last_messages(cid, 20)
    if msgs is None or len(msgs) == 0:
        return True, "?", "?", None
    last = msgs[0]
    last_author = last.get("author", {}).get("username", "?")
    last_author_id = last.get("author", {}).get("id")
    snip = (last.get("content") or "").replace("\n", " ")[:40] or "<embed/image/empty>"
    return (last_author_id == my_id), last_author, snip, msgs

my_last_msg_id = {}

def read_channel(cid, limit=15):
    msgs = get_last_messages(cid, limit)
    if msgs is None:
        msgs = []
    dbg(f"👁️ read #{cid} ({len(msgs)} msgs)")
    if msgs:
        try:
            ack_channel(cid, msgs[0].get("id"))
        except Exception:
            pass
    return msgs

# --------------------------------------------------------------------------- #
# WebSocket gateway                                                           #
# --------------------------------------------------------------------------- #
class GatewayThread(threading.Thread):
    def __init__(self, token, status_text, status_emoji, log_fn, dbg_fn):
        super().__init__(daemon=True)
        self.token = token
        self.status_text = status_text
        self.status_emoji = status_emoji
        self._ws = None
        self._stop = threading.Event()
        self._hb_interval = 41250
        self._seq = None
        self._session_id = None
        self._resume_url = None
        self._last_hb_ack = time.time()
        self.connected = threading.Event()
        self.log = log_fn
        self.dbg = dbg_fn

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _get_gateway_url(self):
        try:
            r = SESSION.get("https://discord.com/api/v9/gateway", timeout=10)
            if r.status_code == 200:
                return r.json().get("url", "wss://gateway.discord.gg")
        except Exception:
            pass
        return "wss://gateway.discord.gg"

    def _send(self, payload):
        if not self._ws:
            return False
        try:
            self._ws.send(json.dumps(payload))
            return True
        except Exception as e:
            self.dbg(f"WS send failed: {e}")
            return False

    def _identify(self):
        identify = {
            "op": 2,
            "d": {
                "token": self.token,
                "properties": {
                    "os": "Windows",
                    "browser": "Chrome",
                    "device": "",
                    "system_locale": DISCORD_LOCALE,
                    "browser_user_agent": _UA,
                    "browser_version": _CHROME_VER,
                    "os_version": "10",
                    "referrer": "",
                    "referring_domain": "",
                    "referrer_current": "",
                    "referring_domain_current": "",
                    "release_channel": "stable",
                    "client_build_number": CLIENT_BUILD,
                    "client_event_source": None,
                },
                "compress": False,
                "large_threshold": 50,
                "capabilities": 16381 | 32768 | 65536,
                "presence": {
                    "status": "online",
                    "since": 0,
                    "activities": [{
                        "type": 4,
                        "name": "Custom Status",
                        "state": self.status_text,
                        "emoji": ({"name": self.status_emoji} if self.status_emoji else None),
                    }] if self.status_text else [],
                    "afk": False,
                },
                "client_state": {
                    "guild_hashes": {},
                    "highest_last_message_id": "0",
                    "read_state_version": -1,
                    "user_guild_settings_version": -1,
                    "user_settings_version": -1,
                    "private_channels_version": "0",
                },
            },
        }
        self._send(identify)

    def _handle_dm(self, d):
        """Process an incoming DM MESSAGE_CREATE from the gateway.

        Runs on the gateway thread, so it MUST NOT do blocking REST calls
        that could outlast the heartbeat interval — missed heartbeats cause
        zombie sessions. If we need to resolve a new channel's type, we
        optimistically treat it as a DM (guild_id is already None which is
        the strong signal) and fetch metadata asynchronously.
        """
        try:
            ch = d.get("channel") or {}
            cid = d.get("channel_id")
            if not cid:
                return
            ctype = ch.get("type")
            if ctype is None:
                if cid in _dm_channel_cache:
                    ctype = _dm_channel_cache[cid].get("type")
                else:
                    # guild_id being absent is already the strongest signal
                    # this is a DM. Treat as type 1 now, fetch metadata async.
                    ctype = 1

                    def _bg_fetch():
                        try:
                            info = get_channel_info(cid)
                            if info:
                                _dm_channel_cache[cid] = info
                        except Exception:
                            pass
                    threading.Thread(target=_bg_fetch, daemon=True).start()
            if ctype != 1:
                return
            if d.get("guild_id") is not None:
                return  # safety: not a DM
            author = d.get("author") or {}
            content = d.get("content") or ""
            attachments = d.get("attachments") or []
            is_me = (author.get("id") == _me_cache.get("id"))
            # Incoming DM from a buyer → pause public activity + forward
            if not is_me:
                snip = (content[:60].replace("\n", " ⏎ ") or "<embed/attachment>")
                buyer_name = author.get('username','?')
                self.log(f"💌 📥 BUYER DM from @{buyer_name}: \"{snip}{'...' if len(content)>60 else ''}\"")
                self.log(f"   → Public posting paused {DM_PAUSE_MINUTES:.0f} min; forwarding to webhook; deep link: https://discord.com/channels/@me/{cid}")
                send_log_webhook(
                    f"📩 **DM** from @{buyer_name} (cid=`{cid}`) → PAUSE {DM_PAUSE_MINUTES:.0f}min"
                )
                extend_dm_pause()
                # Forward off-thread so webhook POST doesn't block heartbeats
                def _fwd():
                    try:
                        forward_dm_message(cid, author, content, attachments, is_me=False)
                    except Exception:
                        pass
                threading.Thread(target=_fwd, daemon=True).start()
            elif FORWARD_OWN_DMS:
                def _fwd_me():
                    try:
                        forward_dm_message(cid, author, content, attachments, is_me=True)
                    except Exception:
                        pass
                threading.Thread(target=_fwd_me, daemon=True).start()
        except Exception as e:
            self.dbg(f"_handle_dm error: {type(e).__name__}: {e}")

    def run(self):
        while not self._stop.is_set():
            try:
                self._connect_once()
            except Exception as e:
                self.dbg(f"WS connect error: {type(e).__name__}: {e}")
            if self._stop.is_set():
                break
            time.sleep(random.uniform(3, 7))
        self.log("🔌 WebSocket gateway stopped")

    def _connect_once(self):
        gw_url = self._get_gateway_url()
        url = f"{gw_url}/?v=9&encoding=json"
        self.dbg(f"WS connecting to {gw_url[:50]}...")
        ws_kwargs = {"timeout": 30}
        if HTTPS_PROXY:
            try:
                pu = urlparse(HTTPS_PROXY)
                host = pu.hostname
                port = pu.port or (443 if pu.scheme == "https" else 80)
                auth = None
                if pu.username:
                    auth = (pu.username, pu.password or "")
                ws_kwargs["http_proxy_host"] = host
                ws_kwargs["http_proxy_port"] = port
                if pu.scheme == "https":
                    ws_kwargs["proxy_type"] = "http"
                if auth:
                    ws_kwargs["http_proxy_auth"] = auth
                self.dbg(f"WS via proxy {host}:{port}")
            except Exception as e:
                self.dbg(f"WS proxy parse failed: {e}")
        cookie_str = "; ".join(
            f"{c.name}={c.value}" for c in SESSION.cookies.jar if c.domain and "discord" in c.domain
        )
        ws_kwargs["header"] = [
            f"User-Agent: {_UA}",
            "Origin: https://discord.com",
            f"Cookie: {cookie_str}",
        ]
        self._ws = _ws.create_connection(url, **ws_kwargs)
        # Timeout must exceed one heartbeat interval so a quiet connection
        # doesn't get torn down between heartbeats. We then use the timeout
        # to detect zombie sessions whose heartbeats are no longer ACK'd.
        self._ws.settimeout(self._hb_interval / 1000.0 + 15)

        hello = json.loads(self._ws.recv())
        if hello.get("op") != 10:
            raise RuntimeError(f"Expected HELLO, got op={hello.get('op')}")
        self._hb_interval = hello["d"].get("heartbeat_interval", 41250)
        self.dbg(f"WS hello: hb_interval={self._hb_interval}")

        self._identify()
        self._last_hb_ack = time.time()

        hb_stop = threading.Event()
        def hb_runner():
            while not hb_stop.is_set() and not self._stop.is_set():
                time.sleep(self._hb_interval / 1000.0)
                if hb_stop.is_set():
                    break
                self._send({"op": 1, "d": self._seq})
                self.dbg(f"💓 WS heartbeat seq={self._seq}")
        hb_thread = threading.Thread(target=hb_runner, daemon=True)
        hb_thread.start()

        got_ready = False
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except _ws.WebSocketTimeoutException:
                # No data within one HB interval + margin. If we also missed
                # a heartbeat ACK for >2 intervals, treat as a dead connection.
                if time.time() - self._last_hb_ack > self._hb_interval / 1000.0 * 2 + 10:
                    self.dbg("WS heartbeat ACK timed out — reconnecting")
                    break
                continue
            except Exception:
                break
            if not raw:
                break
            try:
                pkt = json.loads(raw)
            except Exception:
                continue
            op = pkt.get("op")
            t = pkt.get("t")
            d = pkt.get("d")
            s = pkt.get("s")
            if s is not None:
                self._seq = s

            if op == 11:
                self._last_hb_ack = time.time()
            elif op == 9:
                self.dbg("WS invalid session; will reconnect fresh")
                self._session_id = None
                break
            elif op == 7:
                self.dbg("WS requested reconnect")
                break
            elif t == "READY":
                self._session_id = d.get("session_id")
                self._resume_url = d.get("resume_gateway_url")
                # Cache private channels so we know DMs later
                pcs = d.get("private_channels", [])
                for pc in pcs:
                    _dm_channel_cache[pc["id"]] = pc
                if not got_ready:
                    got_ready = True
                    self.connected.set()
                    user = d.get("user", {})
                    # Refresh our identity
                    _me_cache["id"] = user.get("id")
                    _me_cache["username"] = user.get("username")
                    _me_cache["avatar"] = user.get("avatar")
                    _me_cache["discriminator"] = user.get("discriminator")
                    self.log(f"🟢 Gateway online as {user.get('username','?')} "
                             f"(session {self._session_id[:8] if self._session_id else '?'})")
            elif t == "MESSAGE_CREATE":
                # Handle DMs (non-guild, type 1)
                if not d.get("guild_id"):
                    self._handle_dm(d)
            elif t == "CHANNEL_CREATE":
                # Track newly opened DMs
                try:
                    ctype = d.get("type")
                    cid = d.get("id")
                    if ctype == 1 and cid:
                        _dm_channel_cache[cid] = d
                except Exception:
                    pass

        hb_stop.set()
        try:
            self._ws.close()
        except Exception:
            pass
        if got_ready:
            self.dbg("WS disconnected; will reconnect")
        time.sleep(random.uniform(2, 5))

_gw_thread = None

def start_gateway():
    global _gw_thread
    if not ENABLE_GATEWAY:
        log("🌐 WebSocket gateway: DISABLED by ENABLE_GATEWAY=0 (account will appear offline — suspicious!)")
        return
    if not _HAS_WS:
        log("⚠️ websocket-client not installed; gateway disabled (account will appear offline). Install websocket-client for presence.")
        return
    log("🔌 Connecting to WebSocket gateway (wss://gateway.discord.gg)...")
    try:
        _gw_thread = GatewayThread(USER_TOKEN, CUSTOM_STATUS_TEXT, STATUS_EMOJI, log, dbg)
        _gw_thread.start()
        if _gw_thread.connected.wait(timeout=15):
            log("🟢 Gateway CONNECTED → account appears ONLINE with real-time presence + DM listening.")
            if DM_WEBHOOK_URL:
                log(f"💌 DM forwarding ENABLED (webhook set; public activity auto-pauses {DM_PAUSE_MINUTES:.0f} min on buyer DM)")
        else:
            log("⚠️ Gateway still connecting after 15s — continuing in background. Presence may appear shortly.")
    except Exception as e:
        log(f"⚠️ Gateway thread failed to start: {type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
# Typing / sending / editing                                                  #
# --------------------------------------------------------------------------- #
def typing_duration(text):
    words = len(text.split())
    chars = len(text)
    lines = text.count("\n") + 1
    cpm = random.uniform(210, 350)
    d = (chars / cpm) * 60
    if d < 1.3:
        d = random.uniform(1.2, 2.2)
    if d > 9.0:
        d = random.uniform(6.5, 9.0)
    if lines > 2:
        d += random.uniform(1.0, 3.0)
    if random.random() < 0.15:
        d += random.uniform(1.0, 3.0)
    return d

def send_typing(cid, text):
    # Don't fire typing during a DM pause (we're supposed to be busy reading DMs).
    # Still sleep the full human-style duration so callers don't rush.
    if not public_activity_allowed():
        time.sleep(typing_duration(text) + random.uniform(1.8, 4.5))
        return
    try:
        # Pre-thinking pause: 1.8-4.5s gazing at the channel before typing,
        # plus a 5% chance of a longer "hesitation" pause (1-4s) like a human
        # who second-guesses their wording.
        pre_pause = random.uniform(1.8, 4.5)
        if random.random() < 0.05:
            pre_pause += random.uniform(1.0, 4.0)
        time.sleep(pre_pause)
        ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
        api("POST", f"https://discord.com/api/v9/channels/{cid}/typing",
            referer=ref, json_body={}, retries=1)
    except Exception:
        pass
    # Small mid-typing hesitation 8% of the time (like pausing to think).
    dur = typing_duration(text)
    if random.random() < 0.08 and dur > 3:
        split = random.uniform(0.3, 0.7)
        time.sleep(dur * split)
        time.sleep(random.uniform(0.8, 2.5))
        time.sleep(dur * (1 - split))
    else:
        time.sleep(dur)

def _make_message_payload(text, nonce, with_image=False):
    payload = {
        "content": text,
        "tts": False,
        "nonce": nonce,
        "allowed_mentions": {
            "parse": ["users", "roles"],
            "replied_user": False,
        },
    }
    flags = 0
    if SUPPRESS_EMBEDS and random.random() < 0.4:
        flags |= 4
    if flags:
        payload["flags"] = flags
    return payload

def _build_multipart(payload_dict, fname, fbytes, fmime):
    mp = curl_cffi.CurlMime()
    mp.addpart(
        name="payload_json",
        content_type="application/json",
        data=json.dumps(payload_dict, separators=(",", ":")).encode(),
    )
    mp.addpart(
        name="files[0]",
        content_type=fmime,
        filename=fname,
        data=fbytes,
    )
    return mp

def send_message(cid, text, img=None):
    # Block any public posting during a DM pause
    if not public_activity_allowed():
        return False, 0, "paused for DM", None, None
    send_typing(cid, text)
    nonce = _make_nonce()
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    payload = _make_message_payload(text, nonce, with_image=bool(img))
    if img:
        fname, fbytes, fmime = img
        mp = _build_multipart(payload, fname, fbytes, fmime)
        # NOTE: retries=1 — CurlMime streams are consumed after the first
        # send attempt and cannot be safely replayed on 429/5xx. A single
        # attempt matches real browser upload behavior (browsers don't
        # transparently retry a multipart POST mid-stream).
        r = api("POST", f"https://discord.com/api/v9/channels/{cid}/messages",
                referer=ref, files_mp=mp, retries=1)
    else:
        r = api("POST", f"https://discord.com/api/v9/channels/{cid}/messages",
                referer=ref, json_body=payload, retries=3)
    if r.status_code == 200:
        try:
            msg = r.json()
            # Schedule a verification in ~35s (auto-learn)
            mid = msg.get("id")
            if mid:
                _verify_message_alive(cid, mid, text)
            return True, 200, "", mid, msg
        except Exception:
            return True, 200, "", None, None
    try:
        err = r.json().get("message", getattr(r, "text", ""))[:120]
    except Exception:
        err = str(getattr(r, "status_code", "?"))
    # If AutoMod blocked us outright (403 with message about blocked content),
    # immediately strike+blacklist (no need to wait 35s).
    if r.status_code in (400, 403) and any(kw in (err or "").lower()
            for kw in ("blocked", "automod", "flagged", "not allowed")):
        _record_strike(text, cid, None)
    return False, r.status_code, err, None, None

def edit_message(cid, msg_id, new_text):
    if not public_activity_allowed():
        return False
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    payload = {"content": new_text}
    try:
        time.sleep(random.uniform(5, 22))
        api("POST", f"https://discord.com/api/v9/channels/{cid}/typing",
            referer=ref, json_body={}, retries=1)
        time.sleep(random.uniform(1.0, 2.5))
        r = api("PATCH", f"https://discord.com/api/v9/channels/{cid}/messages/{msg_id}",
                referer=ref, json_body=payload, retries=2)
        return r.status_code in (200, 204)
    except Exception:
        return False

def maybe_typo_edit(cid, msg_id, original_text):
    if not msg_id:
        return
    if not public_activity_allowed():
        return
    if random.random() > TYPO_EDIT_CHANCE:
        return
    if "\n" in original_text or len(original_text) < 8:
        return
    new_text = original_text
    if random.random() < 0.35:
        if not new_text.endswith((".", "!", "?")) and random.random() < 0.5:
            new_text = new_text.rstrip() + "."
    if random.random() < 0.25 and "  " in new_text:
        new_text = new_text.replace("  ", " ", 1)
    swaps = [("DM", "dm"), ("dm", "DM"), ("LF", "lf"), ("lf", "LF"),
             ("BB", "bb"), ("bb", "BB"), ("QUICK", "quick"), ("quick", "QUICK")]
    if random.random() < 0.3:
        a, b = random.choice(swaps)
        if a in new_text:
            new_text = new_text.replace(a, b, 1)
    if random.random() < 0.25 and len(new_text) < DISCORD_MSG_LIMIT - 5:
        new_text = new_text.rstrip() + random.choice([" 🔥", " ⚡", " 💸", " ✅"])
    if new_text == original_text or len(new_text) > DISCORD_MSG_LIMIT:
        return
    def _do_edit():
        ok = edit_message(cid, msg_id, new_text)
        if ok:
            snip = new_text.replace("\n", " ⏎ ")[:40]
            log(f"   ✏️  #{cid}: typo-edit applied → \"{snip}...\" (msg {msg_id})")
    t = threading.Thread(target=_do_edit, daemon=True)
    t.start()

# --------------------------------------------------------------------------- #
# Message variations                                                          #
# --------------------------------------------------------------------------- #
_EMOJIS = ["🔥", "💸", "⚡", "✅", "💰", "🤑", "📈", "💎", "🔔", "👀", "🏷️", "💯"]
_SUFFIXES = ["", " ✅", " ⚡", " 🔥", " dm fast", " online now ✅",
             " quick reply ⚡", " dm me", " hmu", " quick dm",
             " in server now", " reply fast", "", "", " rn"]
_PREFIXES = ["", "💸 ", "⚡ ", "🔥 ", "✅ ", "💰 ", "", ""]
_EXTRA_PHRASES = [
    "", "", "", "",
    " still going", " online rn", " prices firm",
    " quick trade", " no lowballs", " fast replies",
    " can do any amount", " hmu", " still buying",
    " still selling", " reply fast", " in server",
]
_TYPOS_FWD = [("you", "u"), ("please", "pls"), ("to", "t"), ("for", "fr"),
             ("are", "r"), ("your", "ur"), ("be", "b")]
_TYPOS_REV = [("u", "you"), ("pls", "please"), ("ur", "your")]

def build_variations(base):
    is_multiline = "\n" in base.strip()
    out = set()
    if is_multiline:
        lines = base.split("\n")
        header = lines[0]
        rest = "\n".join(lines[1:]) if len(lines) > 1 else ""
        for pre in _PREFIXES + ["🤑 ", "📈 ", ""]:
            for suf in _SUFFIXES[:7]:
                h = f"{pre}{header}{suf}".strip()
                c = h + ("\n" + rest if rest else "")
                if len(c) <= DISCORD_MSG_LIMIT:
                    out.add(c)
        for _ in range(12):
            e = random.choice(_EMOJIS)
            s = random.choice(_SUFFIXES)
            h = f"{e} {header} {s}".strip()
            c = h + ("\n" + rest if rest else "")
            if len(c) <= DISCORD_MSG_LIMIT:
                out.add(c)
    else:
        for pre in _PREFIXES:
            for suf in _SUFFIXES:
                v = f"{pre}{base}{suf}".strip()
                if len(v) <= DISCORD_MSG_LIMIT:
                    out.add(v)
        for _ in range(35):
            e1 = random.choice(_EMOJIS + ["", "", "", ""])
            extra = random.choice(_EXTRA_PHRASES)
            suf = random.choice(_SUFFIXES)
            parts = [e1, base] if e1 else [base]
            if extra:
                parts.append(extra)
            if suf:
                parts.append(suf)
            v = " ".join(parts).replace("  ", " ").strip()
            if random.random() < 0.18:
                v = v.lower()
            if random.random() < 0.08:
                a, b = random.choice(_TYPOS_FWD)
                if a in v and len(v) > 5:
                    v = v.replace(a, b, 1)
            if random.random() < 0.04:
                a, b = random.choice(_TYPOS_REV)
                if a in v:
                    v = v.replace(a, b, 1)
            if len(v) <= DISCORD_MSG_LIMIT:
                out.add(v)
        for _ in range(6):
            suf = random.choice(_SUFFIXES)
            extra = random.choice(_EXTRA_PHRASES[:5])
            parts = [base]
            if extra:
                parts.append(extra)
            if suf:
                parts.append(suf)
            v = " ".join(parts).replace("  ", " ").strip()
            if len(v) <= DISCORD_MSG_LIMIT:
                out.add(v)
    uniq = [v for v in out if len(v) <= DISCORD_MSG_LIMIT]
    if base in uniq:
        uniq.remove(base)
    uniq.insert(0, base)
    return uniq

# --------------------------------------------------------------------------- #
# Image loading                                                               #
# --------------------------------------------------------------------------- #
def load_image():
    if not IMAGE_PATH or not ATTACH_IMAGE:
        log("🖼️  Image: DISABLED (no IMAGE_PATH set or ATTACH_IMAGE=0) — text-only mode.")
        return None, None
    p = Path(IMAGE_PATH).expanduser()
    if not p.exists():
        log(f"⚠️ IMAGE: file not found at {IMAGE_PATH} — falling back to text-only. Check IMAGE_PATH in workflow inputs.")
        return None, None
    try:
        data = p.read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        size_mb = len(data) / 1024 / 1024
        if size_mb > 8:
            log(f"⚠️ IMAGE: {p.name} is {size_mb:.2f}MB (exceeds Discord's 8MB limit) — falling back to text-only. Compress the image.")
            return None, None
        log(f"🖼️  IMAGE loaded: {p.name} ({size_mb:.2f}MB, {mime})")
        if _HAS_PIL and STRIP_EXIF:
            log("   Anti-fingerprinting: EXIF stripped, filename + JPEG bytes randomized per post, ±1px RGB jitter.")
        else:
            log("   ⚠️ EXIF strip / jitter disabled (STRIP_EXIF=0 or Pillow missing) — image metadata may be identifiable.")
        return data, p.name
    except Exception as e:
        log(f"⚠️ Failed to read image: {type(e).__name__}: {e} — falling back to text-only.")
        return None, None

def make_image_payload(raw_bytes, original_name):
    return _process_image(raw_bytes, original_name)

# --------------------------------------------------------------------------- #
# AFK planner / keepalive sleep                                               #
# --------------------------------------------------------------------------- #
def plan_breaks(run_seconds):
    n = random.randint(MIN_AFK_BREAKS, MAX_AFK_BREAKS)
    if n <= 0:
        return []
    out = []
    min_start = 20 * 60
    margin_end = AFK_MAX_MIN * 60
    gap = 15 * 60
    usable = run_seconds - margin_end
    if usable < min_start + AFK_MIN_MIN * 60:
        return []
    for _ in range(n):
        for _attempt in range(100):
            bs = time.time() + random.uniform(min_start, max(min_start + 60, usable))
            bd = random.uniform(AFK_MIN_MIN, AFK_MAX_MIN) * 60
            be = bs + bd
            ok = all(be + gap < es or bs > ee + gap for es, ee in out)
            if ok:
                out.append((bs, be))
                break
    out.sort(key=lambda x: x[0])
    return out

def in_break(breaks, now):
    for s, e in breaks:
        if s <= now < e:
            return True, e - now
    return False, 0

class _KeepaliveSleep:
    def __init__(self):
        self.last_ping = time.time()
    def sleep(self, seconds, end_time=None):
        if seconds <= 0:
            return
        stop = time.time() + seconds
        while time.time() < stop:
            if end_time and time.time() >= end_time:
                return
            chunk = min(30, stop - time.time())
            time.sleep(chunk)
            # Only fire keepalive if we're allowed to do public activity
            if public_activity_allowed() and time.time() - self.last_ping >= 270:
                keepalive()
                self.last_ping = time.time()

_ksleeper = None
def sleep_with_keepalive(seconds, end_time=None):
    global _ksleeper
    if _ksleeper is None:
        _ksleeper = _KeepaliveSleep()
    _ksleeper.sleep(seconds, end_time)

# --------------------------------------------------------------------------- #
# Random reactions                                                            #
# --------------------------------------------------------------------------- #
_REACT_EMOJIS = ["🔥", "💯", "👀", "✅", "👌", "💸", "🤑", "💎"]

def maybe_react(cid, msgs, my_id):
    if not RANDOM_REACT:
        return
    if not public_activity_allowed():
        return
    if random.random() > IDLE_REACT_CHANCE:
        return
    if not msgs:
        return
    candidates = [m for m in msgs[:8]
                  if m.get("author", {}).get("id") != my_id
                  and (m.get("content") or "").strip()
                  and not (m.get("content") or "").strip().startswith("!")]
    if not candidates:
        return
    m = random.choice(candidates)
    emo = random.choice(_REACT_EMOJIS)
    import urllib.parse
    eurl = urllib.parse.quote(emo, safe="")
    url = (f"https://discord.com/api/v9/channels/{cid}/messages/"
           f"{m['id']}/reactions/{eurl}/@me")
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    try:
        r = api("PUT", url, referer=ref, json_body={}, retries=1)
        if r.status_code in (204, 200):
            snip = (m.get("content") or "").replace("\n", " ")[:25]
            log(f"   👌 #{cid}: reacted {emo} to recent msg → \"{snip}...\"")
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# IP + country check                                                          #
# --------------------------------------------------------------------------- #
def check_proxy_ip():
    if not PROXY_CHECK:
        log("🌐 IP check disabled by PROXY_CHECK=0 (not recommended — cannot verify WARP/proxy is working).")
        return
    log("🌐 Checking outbound IP and ISP/org (verifying WARP/proxy is masking datacenter IP)...")
    try:
        r = SESSION.get("https://api.ipify.org?format=json", timeout=10)
        if r.status_code == 200:
            ip = r.json().get("ip", "?")
            org = "?"
            country = None
            country_name = None
            for host in ("ipapi.co", "ipinfo.io"):
                try:
                    r2 = SESSION.get(f"https://{host}/{ip}/json", timeout=8)
                    if r2.status_code == 200:
                        j = r2.json()
                        org = j.get("org") or j.get("asn") or "?"
                        country = (j.get("country_code") or j.get("country") or "").upper()
                        country_name = j.get("country_name") or j.get("country") or "?"
                        break
                except Exception:
                    continue
            log(f"🌐 OUTBOUND IP: {ip}  |  ISP/ORG: {org}  |  COUNTRY: {country or '?'}")
            if ALLOWED_COUNTRIES and country and country not in ALLOWED_COUNTRIES:
                log(f"   ❌ GEO CHECK FAILED: IP is in {country_name} ({country}), which is NOT in ALLOWED_COUNTRIES.")
                log(f"   → Add '{country}' to the ALLOWED_COUNTRIES secret or retry for a new WARP IP. Aborting.")
                sys.exit(2)
            if not HTTPS_PROXY:
                o = str(org).lower()
                if "cloudflare" in o or "as13335" in o:
                    log("   ℹ️  Cloudflare/WARP detected — traffic exits via Cloudflare (not Azure/datacenter).")
                    log("      Note: WARP is VPN-class, not residential. Some strict servers may flag new accounts.")
                    log("      Recommendation: text-only for the first ~10 posts, then enable images.")
                elif any(kw in o for kw in ("microsoft", "azure", "amazon", "aws",
                                           "google", "ovh", "digitalocean",
                                           "hetzner", "oracle", "linode",
                                           "digital ocean", "github")):
                    log("   ⚠️  DATACENTER IP DETECTED! Anti-spam (Wick/Carl/Beemo) may shadow-delete your messages.")
                    log("      This was the cause of the v4.2 silent-shadowban failure. ACTION: enable Cloudflare WARP")
                    log("      (WARP_ENABLED=true in the workflow), set a residential HTTPS_PROXY, or use a self-hosted runner.")
        else:
            log(f"   ⚠️  ipify.org returned HTTP {r.status_code}; cannot verify IP. Continuing (risky).")
    except Exception as e:
        log(f"   ⚠️  IP check failed ({type(e).__name__}: {e}) — continuing but cannot confirm WARP/proxy is active.")

# --------------------------------------------------------------------------- #
# Self-tests                                                                  #
# --------------------------------------------------------------------------- #
def self_test():
    print("=" * 60)
    print(f"🧪 Self-test ({VERSION}, no network calls)")
    print("=" * 60)

    vs = build_variations("SELLING BB LF 2.5$/1K DM ME QUICK CAN DO SMALL AND BIG AMOUNTS")
    assert len(vs) >= 40, f"sell variations: {len(vs)}"
    assert len(set(vs)) == len(vs), "dupes"
    for v in vs:
        assert len(v) <= DISCORD_MSG_LIMIT
    print(f"✅ Sell variations: {len(vs)} unique")

    vb = build_variations(
        "BUYING BLADE BALL:\n\n-TOKENS 2.2/1K\n\n-RAP 1.8$/1K (nlf boosted)\n\nDM me quick")
    assert len(vb) >= 6, f"buy variations: {len(vb)}"
    print(f"✅ Buy variations: {len(vb)} unique")

    assert typing_duration("hi") < typing_duration("x" * 200)
    print("✅ Typing duration scales with length")

    mp = _build_multipart({"content": "hi", "nonce": "123", "tts": False},
                          "test.png", b"PNGDATA"*10, "image/png")
    assert mp is not None
    mp.close()
    print("✅ Multipart CurlMime construction OK")

    import unittest.mock as mock
    overlaps = 0
    for seed in range(1000):
        random.seed(seed)
        with mock.patch("time.time", return_value=1_000_000):
            br = plan_breaks(6 * 3600)
        for i in range(len(br) - 1):
            if br[i][1] + 15*60 > br[i+1][0]:
                overlaps += 1
    assert overlaps == 0
    print("✅ AFK planner: zero overlaps across 1000 seeds")

    with mock.patch("time.time", return_value=1_000_000):
        assert plan_breaks(15 * 60) == []
    print("✅ AFK planner: short runs return []")

    fn, d, m = _process_image(b"rawbytes", "ad.png")
    assert fn.endswith((".png",".jpg",".jpeg",".webp",".gif"))
    assert m.startswith("image/")
    print(f"✅ Image processing: random filename {fn}")

    nonce = _make_nonce()
    assert nonce.isdigit() and len(nonce) >= 17
    print(f"✅ Message nonce looks like snowflake ({nonce[:10]}...)")

    payload = _make_message_payload("test message", nonce)
    assert payload["nonce"] == nonce
    assert "everyone" not in payload["allowed_mentions"]["parse"]
    print("✅ allowed_mentions blocks @everyone/@here pings")

    # v5.2: DM pause mechanics
    global _public_pause_until, _consecutive_deletions
    with _state_lock:
        saved = _public_pause_until
        _public_pause_until = time.time() + 60
    assert not public_activity_allowed(), "public pause should block"
    extend_dm_pause()
    with _state_lock:
        assert _public_pause_until > time.time() + 60, "extend_dm_pause should extend"
        _public_pause_until = 0
    assert public_activity_allowed()
    with _state_lock:
        _public_pause_until = saved
    print("✅ DM public-pause mechanics work")

    # v5.2: strike/blacklist
    with _state_lock:
        before = len(_blocked_variations)
        _consecutive_deletions = 0
    _record_strike("__test_variation__", "0", "0")
    _record_strike("__test_variation__", "0", "0")  # 2nd strike = blacklist
    with _state_lock:
        assert "__test_variation__" in _blocked_variations
        _blocked_variations.discard("__test_variation__")
        _strikes.pop("__test_variation__", None)
        _consecutive_deletions = 0
    print("✅ Strike/blacklist logic works")

    # v5.2: webhook payload builder (avatar URLs)
    class _FakeUser(dict): pass
    fu = _FakeUser(id="123", avatar="abc123", username="tester", discriminator="0001")
    av = _avatar_url(fu)
    assert "cdn.discordapp.com/avatars/123/abc123" in av
    print("✅ CDN avatar URL construction OK")

    print()
    print("=" * 60)
    print(f"🎉 ALL SELF-TESTS PASSED ({VERSION})")
    print("=" * 60)

# --------------------------------------------------------------------------- #
# Main loop                                                                   #
# --------------------------------------------------------------------------- #
def main():
    global _ksleeper, _gw_thread
    _ksleeper = _KeepaliveSleep()

    _warmup_fingerprint()

    start = time.time()
    run_end = start + TOTAL_RUN_MIN * 60
    variations = build_variations(MESSAGE)
    raw_image, image_name = load_image()
    use_img_ever = bool(raw_image) and ATTACH_IMAGE

    # Load persisted blocklist (if gist configured)
    load_blocked_from_gist()
    with _state_lock:
        if _blocked_variations:
            # Filter out blacklisted variations from our working list
            before = len(variations)
            variations = [v for v in variations if v not in _blocked_variations]
            log(f"🧠 Auto-learn: filtered out {before - len(variations)} previously-blocked variations "
                f"({len(variations)} usable remain)")
            if not variations:
                # If every variation was blacklisted, rebuild from scratch but warn
                log("⚠️ All base variations were blocked — resetting blocklist for THIS RUN only.")
                log("   This is a bad sign: prior runs had every message variant deleted. Consider fresh IP/copy.")
                _blocked_variations.clear()
                variations = build_variations(MESSAGE)

    last_sent = {}
    slowmodes = {}
    ch_names = {}
    channel_errors = defaultdict(int)
    dead_channels = set()
    stats = defaultdict(lambda: {"sent": 0, "errors": 0, "skipped": 0,
                                 "cooldown": 0, "img": 0, "txt": 0, "edits": 0})
    total_sent = total_err = total_skip = total_distractions = 0
    total_img = total_edits = 0
    cycle = 0
    sent_count_global = 0
    last_gist_save = 0
    returning_from_afk = False

    log("=" * 66)
    log(f"🎯 MARKETPLACE AD SENDER  {VERSION}  |  MODE: {AD_TYPE.upper()}")
    log("=" * 66)
    log(f"📌 CHANNELS ({len(CHANNEL_IDS)}):")
    for c in CHANNEL_IDS:
        log(f"   • {c}")
    log(f"⏱️  INTERVAL      : ~{INTERVAL_MIN} min/channel (±30-45% jitter, bursty cadence)")
    log(f"⌛ RUNTIME       : {TOTAL_RUN_MIN:.0f} min ({TOTAL_RUN_MIN/60:.1f}h) → ends at {datetime.fromtimestamp(run_end).strftime('%Y-%m-%d %H:%M:%S')}")
    first_line = MESSAGE.split('\n')[0]
    log(f"📝 BASE MESSAGE  : \"{first_line[:70]}{'...' if len(first_line)>70 else ''}\"")
    log(f"   Variations    : {len(variations)} unique message variants generated ({len(MESSAGE)} chars base)")
    log(f"🖼️  IMAGE         : {'YES' if use_img_ever else 'NO (text-only)'}"
        + (f" (text-only warmup: first {WARMUP_POSTS} posts, then 100% attach)" if use_img_ever else ""))
    log(f"🗑️  AUTO-DELETE   : OFF — messages stack naturally (no self-delete)")
    log(f"🧠 SMART COOLDOWN: ON — only repost after someone else posts after us")
    log(f"🔌 WS GATEWAY    : {'ON (account appears ONLINE, real presence)' if ENABLE_GATEWAY and _HAS_WS else 'OFF (suspicious! — account looks offline)'}")
    log(f"✏️  TYPO EDITS    : {TYPO_EDIT_CHANCE*100:.0f}% chance after post (5-22s delay, natural correction)")
    log(f"👌 REACTIONS     : {'ON' if RANDOM_REACT else 'OFF'} (~{IDLE_REACT_CHANCE*100:.0f}% chance per cooldown read)")
    log(f"☕ AFK BREAKS     : {MIN_AFK_BREAKS}-{MAX_AFK_BREAKS} per 6h chunk, {AFK_MIN_MIN:.0f}-{AFK_MAX_MIN:.0f} min each")
    log(f"🔒 TLS/HTTP2     : curl_cffi impersonating Chrome (real JA3/HTTP2 fingerprint)")
    log(f"💌 DM FORWARDING : {'ON' if DM_WEBHOOK_URL else 'OFF'}"
        + (f" (auto-pause public activity {DM_PAUSE_MINUTES:.0f} min when buyer DMs)" if DM_WEBHOOK_URL else ""))
    log(f"📋 LOG WEBHOOK   : {'ON' if LOG_WEBHOOK_URL else 'OFF (optional action-log channel)'}")
    log(f"📊 DASHBOARD    : {'ON (periodic summaries)' if DASHBOARD_WEBHOOK_URL else 'OFF (optional)'}")
    log(f"🧠 AUTO-LEARN    : strikes={BLOCKED_STRIKES}, safety_stop={BLOCKED_SAFETY_STOP}"
        + (f", gist={GIST_ID[:8]}... (persisted across runs)" if GIST_ID else " (no gist persistence — resets each run)"))
    if ALLOWED_COUNTRIES:
        log(f"🌍 GEO CHECK     : ALLOWED_COUNTRIES = {','.join(ALLOWED_COUNTRIES)} (abort if WARP routes elsewhere)")
    log(f"🐛 DEBUG LOGS    : {'ON (verbose)' if DEBUG else 'OFF'}")
    if HTTPS_PROXY:
        log(f"🔗 PROXY         : ON (HTTPS_PROXY set, credentials hidden)")
    else:
        log(f"🔗 PROXY         : OFF (Cloudflare WARP will be used on GHA cloud)")
    log("=" * 66)

    check_proxy_ip()

    startup_phase1 = random.uniform(8, 20)
    log(f"⏳ Simulated app launch: {startup_phase1:.0f}s boot delay (simulating opening Discord app)...")
    sleep_chunked(startup_phase1, run_end)

    me, vreason = validate_token()
    if not me:
        log(f"❌ AUTH FAILED — could not authenticate (reason: {vreason}). Aborting.")
        log("   → Double-check USER_TOKEN secret. If v4.2 was shadowbanned, the token may still be valid but the session flagged.")
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, total_img, total_edits, stats)
        sys.exit(1)
    my_id = me.get("id")
    username = me.get("username", "???")
    if not my_id:
        log("❌ AUTH ERROR: Could not read user id from /users/@me response (malformed response?). Aborting.")
        sys.exit(1)
    log(f"✅ AUTH OK → Logged in as {username}")
    log(f"   USER ID       : {my_id}")
    verified = me.get('verified', False)
    mfa = me.get('mfa_enabled', False)
    log(f"   EMAIL VERIFIED: {'✅ YES' if verified else '❌ NO — higher flag risk! Verify email before long runs.'}")
    log(f"   2FA ENABLED   : {'✅ YES' if mfa else '⚠️ NO — tip: enabling 2FA raises account trust score.'}")

    start_gateway()
    time.sleep(random.uniform(2, 5))
    set_status()

    log("📡 Browsing channels (warmup reads — simulating opening each channel before posting)...")
    ok_count = 0
    for cid in CHANNEL_IDS:
        log(f"📥 Fetching channel info for {cid}...")
        info = get_channel_info(cid)
        if not info:
            log(f"   ❌ CHANNEL {cid}: could not fetch info. Alt may not be in the server, channel may be deleted, or ID is wrong. Skipping this channel for the whole run.")
            dead_channels.add(cid)
            sleep_chunked(random.uniform(2.0, 4.0))
            continue
        name = info.get("name", "?")
        slowmodes[cid] = info.get("rate_limit_per_user", 0)
        ch_names[cid] = name
        gid = _guild_id_cache.get(cid,'?')
        log(f"   ✅ CHANNEL → #{name} (id={cid}) in GUILD {gid} | slowmode = {slowmodes[cid]}s")
        sleep_chunked(random.uniform(0.8, 1.8))
        if public_activity_allowed():
            log(f"   👁️  Reading channel #{name} (recent messages, marking as read)...")
            ch_msgs = read_channel(cid)
            if ch_msgs:
                last_snip = (ch_msgs[0].get("content") or "").replace("\n", " ")[:40] or "<embed/image/empty>"
                log(f"      ✅ Ack sent. Last msg visible: \"{last_snip}...\"")
        gaze = random.uniform(3.0, 9.0)
        log(f"   👀 Gazing at #{name} for {gaze:.0f}s (simulating reading chat before moving on)...")
        sleep_chunked(gaze, run_end)
        ok_count += 1

    active_channels = [c for c in CHANNEL_IDS if c not in dead_channels]
    if ok_count == 0:
        log("❌ FATAL: No accessible channels. Verify the alt is in the servers and CHANNEL_IDS are correct. Aborting.")
        sys.exit(1)
    if dead_channels:
        log(f"⚠️  {len(dead_channels)}/{len(CHANNEL_IDS)} channels INACCESSIBLE and will be skipped for this run.")

    warmup_wait = random.uniform(40, 90)
    log(f"👀 Reading chat across accessible channels for {warmup_wait:.0f}s before first post (simulating scrolling/reading)...")
    sleep_chunked(warmup_wait, run_end)
    for cid in active_channels:
        if public_activity_allowed():
            read_channel(cid)
        sleep_chunked(random.uniform(2.0, 6.0))
    final_wait = random.uniform(8, 20)
    log(f"⌛ Final pre-post pause {final_wait:.0f}s...")
    sleep_chunked(final_wait, run_end)

    breaks = plan_breaks(TOTAL_RUN_MIN * 60)
    log(f"☕ AFK BREAKS scheduled: {len(breaks)} (each 10-30 min, ≥15 min apart):")
    for s, e in breaks:
        log(f"   • {datetime.fromtimestamp(s).strftime('%H:%M')} → "
            f"{datetime.fromtimestamp(e).strftime('%H:%M')} ({(e-s)/60:.0f} min)")

    log("")
    log("🚀 STARTING MAIN LOOP.")
    log("👉 ACTION REQUIRED — MANUAL VERIFICATION:")
    log("   After the FIRST POST is logged, open Discord on your main/phone and")
    log("   CONFIRM you can see the ad in the channel. If you can't see it,")
    log("   anti-spam is SHADOW-DELETING it. CANCEL the run immediately. Don't")
    log("   waste the alt by continuing to post into a shadowban.")
    log("")

    # --- Startup notification to log webhook ---
    ch_list = ", ".join("#" + ch_names.get(c, str(c)) for c in active_channels)
    send_log_webhook(
        f"🟢 **STARTUP** `{VERSION}` | mode=`{AD_TYPE.upper()}` | channels=[{ch_list}] "
        f"| interval=~{INTERVAL_MIN}min (±30-45%) | runtime={TOTAL_RUN_MIN:.0f}min | "
        f"variants={len(variations)} | image={'on' if use_img_ever else 'off'} | "
        f"gateway={'on' if ENABLE_GATEWAY and _HAS_WS else 'off'}"
    )
    # --- Startup dashboard embed ---
    if DASHBOARD_WEBHOOK_URL:
        try:
            ch_list_md = "\n".join(f"• #{ch_names.get(c,c)} `{c}`" for c in active_channels)
            send_dashboard(_dashboard_startup_embed(
                VERSION, AD_TYPE.upper(), ch_list_md, INTERVAL_MIN, TOTAL_RUN_MIN,
                len(variations), use_img_ever, len(CHANNEL_IDS), len(active_channels)))
        except Exception as e:
            dbg(f"[DASHBOARD] startup embed failed: {e}")

    try:
        # ================================================================== #
        # INDEPENDENT PER-CHANNEL SCHEDULER (replaces the old global cycle)  #
        #                                                                    #
        # Each channel has its own next_post_time. The main loop picks the   #
        # channel whose next_post_time is soonest, sleeps precisely until    #
        # then (with a tiny human jitter), posts, and recomputes only that  #
        # channel's next_post_time. A slowmode on one channel NEVER blocks   #
        # another channel.                                                  #
        # ================================================================== #

        # Initialise next_post_time for every channel: 12-30s after startup
        # (replaces the "final pre-post pause"), so channels don't all fire
        # at t=0. Staggered by a 3-10s gap so messages aren't simultaneous.
        next_post_time = {}
        stagger = 0.0
        for cid in active_channels:
            next_post_time[cid] = time.time() + random.uniform(12, 30) + stagger
            stagger += random.uniform(3, 10)

        # Schedule the first dashboard summary ~60s after first expected post
        next_dashboard_time = time.time() + 60
        dashboard_interval = 30 * 60  # one dashboard summary every 30 minutes
        posts_since_last_dash = 0

        cycle = 0  # repurposed: increments on every post (not every global pass)
        last_dash_elapsed = 0.0

        # Per-channel working state
        used_variations = set()
        last_msg_id_in_channel = dict(my_last_msg_id)  # thread-local view

        log("")
        log("🧠 SCHEDULER: independent per-channel timing enabled.")
        log("   Each channel posts on its own schedule (slowmode + jitter);")
        log("   slow channels no longer block fast ones. Initial stagger set.")
        log("")

        while time.time() < run_end:
            now = time.time()

            # ---------- AFK handling ----------
            in_afk, afk_left = in_break(breaks, now)
            if in_afk:
                resume_ts = time.time() + afk_left
                log(f"☕ AFK BREAK — stepping away for {afk_left/60:.1f} min.")
                log(f"   Resuming around {datetime.fromtimestamp(resume_ts).strftime('%H:%M:%S')}. All public posting paused (simulating being offline).")
                sleep_with_keepalive(min(60, afk_left), run_end)
                returning_from_afk = True
                # Reset next-post times so we don't spam a burst of overdue
                # posts the instant we come back.
                stagger = 0.0
                for cid in active_channels:
                    next_post_time[cid] = time.time() + random.uniform(15, 45) + stagger
                    stagger += random.uniform(3, 8)
                continue

            if returning_from_afk:
                ret_wait = random.uniform(15, 45)
                log(f"👋 BACK FROM AFK — re-orienting for {ret_wait:.0f}s (catching up on missed messages, simulating reopening Discord)...")
                sleep_chunked(ret_wait, run_end)
                for _cid in active_channels:
                    if time.time() >= run_end:
                        break
                    if public_activity_allowed():
                        _n = ch_names.get(_cid, _cid)
                        dbg(f"[AFK-REORIENT] re-reading #{_n} ({_cid}) after AFK break")
                        read_channel(_cid)
                    sleep_chunked(random.uniform(1.5, 4.0), run_end)
                returning_from_afk = False
                log("   ✅ Re-oriented. Resuming normal activity.")
                continue

            # ---------- DM pause (don't post publicly while buyer is DMing) --
            if not public_activity_allowed():
                with _state_lock:
                    left = max(0, _public_pause_until - time.time())
                log(f"⏸️  BUYER DM PAUSE ACTIVE — {left/60:.1f}m left. Idling (no posts / reactions / typing).")
                sleep_chunked(min(30, left + 5), run_end)
                continue

            # ---------- 10% pre-cycle distraction pause (checking DMs etc.) ---
            if random.random() < 0.10 and total_sent > 0:
                dist = random.uniform(60, 300)
                total_distractions += 1
                log(f"💭 DISTRACTION PAUSE — pausing public activity for {dist:.0f}s (simulating checking DMs / another server / tabbing away).")
                sleep_with_keepalive(dist, run_end)
                if time.time() >= run_end:
                    break
                continue

            # ---------- Pick next channel to post to ------------------------
            # Choose active channel with earliest next_post_time that is not
            # in error-backoff / dead / DM-paused.
            candidates = [c for c in active_channels if c not in dead_channels
                          and channel_errors.get(c, 0) < 3]
            if not candidates:
                log("⚠️  No eligible channels (all dead or in error backoff). Sleeping 60s...")
                sleep_chunked(60, run_end)
                continue
            cid = min(candidates, key=lambda c: next_post_time.get(c, now))
            due = next_post_time[cid]
            ch_tag = f"#{ch_names.get(cid, cid)} ({cid})"

            # ---------- Sleep until that channel is due --------------------
            wait_sec = due - now
            if wait_sec > 0:
                # Human-looking jitter: don't fire on the exact second.
                jitter = random.uniform(2, 5)
                sleep_sec = wait_sec + jitter
                # Cap sleep chunks at 30s so keepalives / interrupts / DM-pause
                # checks keep firing during long waits.
                if sleep_sec > 30:
                    log(f"⏳ Next: {ch_tag} in {wait_sec/60:.1f} min (sleeping with keepalives)...")
                else:
                    dbg(f"[SCHED] sleeping {sleep_sec:.1f}s until {ch_tag} is due")
                sleep_with_keepalive(min(sleep_sec, 30), run_end)
                continue  # loop back to re-check AFK/DM/runtime

            # If we reach here, wait_sec <= 0: the channel is due.
            remaining_min = (run_end - time.time()) / 60
            cycle += 1  # "post attempts" counter

            # ---------- Direction + warmup status header (every ~10 posts) --
            if cycle == 1 or cycle % 10 == 0:
                direction = "💰 SELL" if AD_TYPE == "sell" else "🛒 BUY"
                if use_img_ever and sent_count_global < WARMUP_POSTS:
                    img_status = f"🔰 warmup {sent_count_global}/{WARMUP_POSTS} (text-only until warmup done)"
                elif use_img_ever:
                    img_status = "🖼️ image attach ENABLED (100% after warmup)"
                else:
                    img_status = "💬 text-only mode"
                log("")
                log(f"{'─'*25} Post #{cycle} [{direction}] | {remaining_min:.0f} min remaining | {_ts()} {'─'*25}")
                log(f"   Status: {img_status}")

            log("")
            log(f"🔍 {ch_tag}: channel is DUE — preparing post...")

            # ---------- Re-check slowmode belt-and-braces ------------------
            slow = slowmodes.get(cid, 0)
            if slow > 0 and cid in last_sent:
                elapsed = time.time() - last_sent[cid]
                need_wait = slow - elapsed + random.uniform(2, 5)
                if need_wait > 0:
                    log(f"   ⏳ {ch_tag}: SLOWMODE belt-and-braces wait — {need_wait:.0f}s still needed. Rescheduling.")
                    next_post_time[cid] = time.time() + need_wait
                    continue

            # ---------- Quick glance at recent msgs (for reactions/cooldown)
            try:
                recent = get_last_messages(cid, 20)
            except Exception:
                recent = None
            i_am_last = False
            last_author = "?"
            last_snip = ""
            if recent and len(recent) > 0:
                last2 = recent[0]
                i_am_last = (last2.get("author", {}).get("id") == my_id)
                last_author = last2.get("author", {}).get("username", "?")
                last_snip = (last2.get("content") or "").replace("\n", " ")[:50] or "<embed/image/empty>"

            # ---------- Deletion detection (last 20 msgs) ------------------
            prev_id = my_last_msg_id.get(cid)
            deleted_detected = False
            if prev_id and recent is not None:
                recent_ids = {m.get("id") for m in recent}
                if prev_id not in recent_ids:
                    # Only treat as deleted if it's within 3 min of posting;
                    # otherwise it's just buried by chat velocity.
                    age_s = time.time() - last_sent.get(cid, 0) if cid in last_sent else 999
                    if age_s < 180:
                        deleted_detected = True
                        log(f"   ⚠️  {ch_tag}: PREVIOUS AD VANISHED (sent {age_s:.0f}s ago, not in last 20 msgs). Likely deleted by anti-spam. Force-reposting.")
                    else:
                        dbg(f"[SCHED] {ch_tag}: prev msg {prev_id} not in last 20 but age={age_s:.0f}s (buried, not deleted)")

            # ---------- Safety-net: never go completely silent -------------
            force_post = False
            if cid in last_sent:
                since_last = time.time() - last_sent[cid]
                max_wait = max(INTERVAL_MIN*60*2.5, slowmodes.get(cid,0) + 120)
                if since_last > max_wait:
                    force_post = True
                    log(f"   🔄 {ch_tag}: SAFETY-NET TRIGGERED — last sent {since_last/60:.1f} min ago (>2.5× interval). Force-reposting.")

            # ---------- Optional smart cooldown (skip if we're LATEST) -----
            # In 30-50 msg/min channels this almost never triggers (good),
            # but in quiet channels it saves us from spamming ourselves down.
            if i_am_last and not deleted_detected and not force_post:
                stats[cid]["cooldown"] += 1
                stats[cid]["skipped"] += 1
                total_skip += 1
                log(f"   ⏭️  {ch_tag}: our ad is still the LATEST message (by @{last_author}). Cooldown — rescheduling.")
                dbg(f"      Last msg: \"{last_snip}\"")
                if recent is not None:
                    maybe_react(cid, recent[:5], my_id)
                gaze = random.uniform(4, 10)
                log(f"   👀 {ch_tag}: glancing at chat for {gaze:.0f}s (simulating reading without posting)...")
                sleep_chunked(gaze, run_end)
                # Reschedule: don't check again for a while
                base_wait = max(slow if slow > 0 else INTERVAL_MIN*60, 60)
                next_post_time[cid] = time.time() + base_wait * random.uniform(0.6, 1.1)
                continue

            # ---------- Random skip (8-30% of the time, human-like) -------
            # After warmup only — don't skip warmup posts.
            post_threshold = random.uniform(0.70, 0.92)
            skip_chance_pct = (1 - post_threshold) * 100
            if sent_count_global >= WARMUP_POSTS and random.random() > post_threshold:
                stats[cid]["skipped"] += 1
                total_skip += 1
                log(f"   ↪️  {ch_tag}: RANDOM SKIP ({skip_chance_pct:.0f}% chance rolled). Skipping this pass to look human.")
                sleep_chunked(random.uniform(4, 10), run_end)
                # Reschedule: try again in 30-90s
                next_post_time[cid] = time.time() + random.uniform(30, 90)
                continue

            # ---------- Pick an un-blacklisted variation ------------------
            # Snapshot the blocked set under lock — the background
            # verification thread can add to _blocked_variations at any
            # time, and iterating the live set causes
            #   "RuntimeError: Set changed size during iteration".
            with _state_lock:
                blocked_snapshot = set(_blocked_variations)
            available = [v for v in variations
                         if v not in used_variations and v not in blocked_snapshot]
            if not available:
                used_variations.clear()
                with _state_lock:
                    blocked_snapshot = set(_blocked_variations)
                available = [v for v in variations if v not in blocked_snapshot]
                if not available:
                    log("")
                    log("🛑 CRITICAL: ALL message variations have been blacklisted by auto-learn.")
                    log("   The account/IP is flagged — stopping to protect the alt.")
                    send_log_webhook("🛑 **CRITICAL** all variations blacklisted — aborting.")
                    save_blocked_to_gist(force=True)
                    sys.exit(2)
            msg = random.choice(available)
            used_variations.add(msg)

            # ---------- Image attachment logic ----------------------------
            attach_this_post = False
            if use_img_ever and sent_count_global < WARMUP_POSTS:
                attach_this_post = False
                log(f"   🔰 WARMUP POST ({sent_count_global+1}/{WARMUP_POSTS}) — text-only to age the session before sending images")
            elif use_img_ever and sent_count_global >= WARMUP_POSTS:
                # 100% image attach after warmup (for BOTH SELL and BUY, both
                # simple and detailed styles). Fallback to text-only if image
                # payload build fails.
                attach_this_post = True

            img_payload = None
            if attach_this_post:
                log(f"   🖼️ {ch_tag}: building randomized image payload (EXIF stripped, filename randomized, JPEG q90-96, ±1px jitter)...")
                img_payload = make_image_payload(raw_image, image_name)
                if img_payload is None:
                    log(f"   ⚠️  {ch_tag}: image payload build failed; falling back to text-only.")
                    attach_this_post = False

            snip = msg.replace("\n", " ⏎ ")[:55]
            kind = "📷 IMAGE+TEXT" if attach_this_post else "💬 TEXT-ONLY"
            log(f"   📤 {ch_tag}: SENDING {kind} → \"{snip}{'...' if len(msg) > 55 else ''}\"")

            # Pre-send "thinking" + typing indicator is handled inside send_message
            ok, code, err, new_msg_id, msg_obj = send_message(cid, msg, img_payload)

            # Recompute this channel's next_post_time regardless of success/fail
            if ok:
                last_sent[cid] = time.time()
                channel_errors[cid] = 0  # reset error backoff on success
                # Compute next_post_time based on slowmode + INTERVAL_MIN jitter
                if slow > 0:
                    # Respect slowmode strictly, add INTERVAL_MIN-style jitter
                    # on top (30s minimum extra jitter, up to slowmode/3 extra)
                    extra_jitter = max(30, min(slow / 3, INTERVAL_MIN * 60 * random.uniform(0.2, 0.6)))
                    nxt = time.time() + slow + extra_jitter + random.uniform(0, 20)
                else:
                    # No slowmode: base = INTERVAL_MIN ±30-45%
                    nxt = time.time() + INTERVAL_MIN * 60 * random.uniform(0.70, 1.45)
                next_post_time[cid] = nxt

                total_sent += 1
                sent_count_global += 1
                posts_since_last_dash += 1
                stats[cid]["sent"] += 1
                if attach_this_post:
                    stats[cid]["img"] += 1
                    total_img += 1
                else:
                    stats[cid]["txt"] += 1
                if new_msg_id:
                    my_last_msg_id[cid] = new_msg_id
                channel_errors[cid] = 0
                log(f"   ✅ {ch_tag}: MESSAGE POSTED SUCCESSFULLY — id={new_msg_id} (run total: {total_sent})")
                dbg(f"      Full msg: {msg[:200]}{'...' if len(msg)>200 else ''}")
                log(f"   ⏭️  Next post for {ch_tag} at ~{datetime.fromtimestamp(nxt).strftime('%H:%M:%S')} (in {(nxt-time.time())/60:.1f} min).")
                send_log_webhook(
                    f"✅ **SEND** {ch_tag} | {'📷img' if attach_this_post else '💬txt'} | total=`{total_sent}` | id=`{new_msg_id}`"
                )

                if new_msg_id and random.random() < TYPO_EDIT_CHANCE:
                    t = threading.Thread(
                        target=lambda cid=cid, mid=new_msg_id, mt=msg: (
                            maybe_typo_edit(cid, mid, mt) or None
                        ),
                        daemon=True,
                    )
                    t.start()
                    stats[cid]["edits"] += 1
                    total_edits += 1
                    log(f"   ✏️  {ch_tag}: queued typo-edit for msg {new_msg_id} (18% chance) — will edit 5-22s after post with small natural correction.")
            else:
                total_err += 1
                stats[cid]["errors"] += 1
                channel_errors[cid] += 1
                log(f"   ❌ {ch_tag}: SEND FAILED — HTTP {code}: {err}")
                send_log_webhook(
                    f"❌ **FAIL** {ch_tag} | HTTP `{code}`: {err}"
                )
                # Back off this channel after an error (don't retry immediately).
                # Exponential backoff: 1 err → 2-3 min, 2 errs → 5-8 min, 3+ errs → 10-20 min.
                backoff = [0, 180, 480, 900][min(channel_errors[cid], 3)] + random.uniform(-30, 60)
                next_post_time[cid] = time.time() + max(60, backoff)
                log(f"   ⏳ {ch_tag}: backing off {backoff/60:.1f} min before retrying.")

                if code == 401 or code == 403:
                    recheck, rr = validate_token()
                    if recheck is None and rr == "invalid":
                        log("")
                        log("🛑 CRITICAL: Token invalidated/revoked/banned (HTTP 401/403 and re-auth failed).")
                        log("   Discord has flagged this alt. Stopping immediately.")
                        send_log_webhook(
                            f"🛑 **BANNED?** Token invalidated (HTTP {code}) | sent=`{total_sent}`. Aborting."
                        )
                        send_dashboard(_dashboard_cycle_embed(
                            cycle, (time.time()-start)/60, total_sent, total_img,
                            total_sent-total_img, total_edits, total_err, total_skip,
                            stats, len(active_channels), len(CHANNEL_IDS),
                            set(active_channels), ch_names, slowmodes, last_sent,
                            my_last_msg_id, is_shutdown=True))
                        save_blocked_to_gist(force=True)
                        _print_stats(start, total_sent, total_err, total_skip,
                                     total_distractions, total_img, total_edits, stats)
                        sys.exit(2)
                    elif recheck is None:
                        log(f"   ⚠️  {ch_tag}: got {code} but token re-validation also failed ({rr}). Backing off this channel.")
                    else:
                        log(f"   ⚠️  {ch_tag}: HTTP 403 but token VALID — channel inaccessible (kicked/banned/deleted). Marking DEAD.")
                        dead_channels.add(cid)
                        if cid in active_channels:
                            active_channels.remove(cid)
                        if cid in next_post_time:
                            del next_post_time[cid]
                elif code == 404:
                    log(f"   ⚠️  {ch_tag}: HTTP 404 — channel deleted/no access. Marking DEAD.")
                    dead_channels.add(cid)
                    if cid in active_channels:
                        active_channels.remove(cid)
                    if cid in next_post_time:
                        del next_post_time[cid]

            if time.time() >= run_end:
                log("   ⏱️ Runtime limit reached; exiting scheduler.")
                break

            # Periodically save blocklist
            if time.time() - last_gist_save > 300:
                if save_blocked_to_gist():
                    last_gist_save = time.time()

            if not public_activity_allowed():
                with _state_lock:
                    left = max(0, _public_pause_until - time.time())
                log(f"   📩 {ch_tag}: BUYER DM ARRIVED mid-post — pausing ALL public activity for {left/60:.1f}m.")
                continue

            # ---------- Post-send natural "gaze" behavior -----------------
            # 5% mid-send distraction
            if random.random() < 0.05 and total_sent > 3:
                mid_dist = random.uniform(45, 180)
                total_distractions += 1
                log(f"   💭 {ch_tag}: MID-SESSION DISTRACTION — pausing {mid_dist:.0f}s (DM / phone / app-switch).")
                sleep_with_keepalive(mid_dist, run_end)
                if time.time() >= run_end:
                    break
            elif random.random() < 0.40 and len(active_channels) > 1:
                other = random.choice([c for c in active_channels if c != cid])
                oname = ch_names.get(other, other)
                g1 = random.uniform(3, 7)
                log(f"   👀 {ch_tag}: glancing at post for {g1:.0f}s...")
                sleep_chunked(g1, run_end)
                if public_activity_allowed():
                    log(f"   👀 Switching to #{oname} ({other}) and reading recent messages (browsing other channels after posting)...")
                    read_channel(other)
                g2 = random.uniform(3, 8)
                sleep_chunked(g2, run_end)
            elif random.random() < 0.25:
                g = random.uniform(20, 55)
                log(f"   👀 {ch_tag}: staring at chat for {g:.0f}s after posting (simulating reading responses)...")
                sleep_chunked(g, run_end)
            else:
                g = random.uniform(8, 22)
                log(f"   👀 {ch_tag}: waiting {g:.0f}s before moving on...")
                sleep_chunked(g, run_end)

            # ---------- Periodic dashboard summary -----------------------
            if DASHBOARD_WEBHOOK_URL and time.time() >= next_dashboard_time:
                elapsed_min = (time.time() - start)/60
                in_break_now, afk_l = in_break(breaks, time.time())
                try:
                    send_dashboard(_dashboard_cycle_embed(
                        cycle, elapsed_min, total_sent, total_img,
                        total_sent-total_img, total_edits, total_err, total_skip,
                        stats, len(active_channels), len(CHANNEL_IDS),
                        set(active_channels), ch_names, slowmodes, last_sent,
                        my_last_msg_id, in_afk_flag=in_break_now, afk_left=afk_l))
                    next_dashboard_time = time.time() + dashboard_interval
                    dbg(f"[DASHBOARD] sent periodic summary (interval={dashboard_interval/60:.0f}m)")
                except Exception as e:
                    dbg(f"[DASHBOARD] error sending summary: {e}")
                    next_dashboard_time = time.time() + 300  # retry in 5 min

            # End of per-post iteration — main while-loop continues by
            # picking the next-earliest channel. There is NO global
            # "next cycle wait" because each channel has its own timer.


    except KeyboardInterrupt:
        log("\n🛑 STOPPED BY USER (Ctrl+C / workflow cancel).")
        elapsed_min = (time.time() - start)/60
        send_log_webhook(
            f"🛑 **STOPPED** by user (Ctrl+C/cancel) | sent=`{total_sent}` | elapsed=`{elapsed_min:.1f}min`"
        )
        if DASHBOARD_WEBHOOK_URL:
            try:
                send_dashboard(_dashboard_cycle_embed(
                    cycle, elapsed_min, total_sent, total_img, total_sent-total_img,
                    total_edits, total_err, total_skip, stats,
                    len(active_channels), len(CHANNEL_IDS), set(active_channels),
                    ch_names, slowmodes, last_sent, my_last_msg_id, is_shutdown=True))
                time.sleep(1.5)
            except Exception:
                pass
        save_blocked_to_gist(force=True)
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, total_img, total_edits, stats)
        sys.exit(130)
    except SystemExit:
        save_blocked_to_gist(force=True)
        raise
    except Exception as e:
        elapsed_min = (time.time() - start)/60
        log(f"\n💥 UNHANDLED ERROR (bug?): {type(e).__name__}: {e}")
        log("   Please report this with the full log output so it can be fixed.")
        send_log_webhook(
            f"💥 **CRASH** `{type(e).__name__}`: {str(e)[:200]} | sent=`{total_sent}` | elapsed=`{elapsed_min:.1f}min`"
        )
        if DASHBOARD_WEBHOOK_URL:
            try:
                send_dashboard(_dashboard_cycle_embed(
                    cycle, elapsed_min, total_sent, total_img, total_sent-total_img,
                    total_edits, total_err, total_skip, stats,
                    len(active_channels), len(CHANNEL_IDS), set(active_channels),
                    ch_names, slowmodes, last_sent, my_last_msg_id, is_shutdown=True))
                time.sleep(1.5)
            except Exception:
                pass
        save_blocked_to_gist(force=True)
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, total_img, total_edits, stats)
        raise

    elapsed_min = (time.time() - start)/60
    log("\n🏁 Reached scheduled end time — run complete.")
    send_log_webhook(
        f"🏁 **FINISHED** (scheduled end) | sent=`{total_sent}` "
        f"(💬{total_sent-total_img}/📷{total_img}) | err=`{total_err}` | "
        f"edits=`{total_edits}` | elapsed=`{elapsed_min:.1f}min`"
    )
    if DASHBOARD_WEBHOOK_URL:
        try:
            send_dashboard(_dashboard_cycle_embed(
                cycle, elapsed_min, total_sent, total_img, total_sent-total_img,
                total_edits, total_err, total_skip, stats,
                len(active_channels), len(CHANNEL_IDS), set(active_channels),
                ch_names, slowmodes, last_sent, my_last_msg_id, is_shutdown=True))
            time.sleep(1.5)  # give daemon thread a moment to send
        except Exception:
            pass
    save_blocked_to_gist(force=True)
    _print_stats(start, total_sent, total_err, total_skip,
                 total_distractions, total_img, total_edits, stats)
    if _gw_thread is not None:
        _gw_thread.stop()
    sys.exit(0)


def _print_stats(start_ts, sent, err, skip, distractions, img, edits, per_ch):
    elapsed = (time.time() - start_ts)/60
    log("=" * 66)
    log("📊 FINAL STATS")
    log("=" * 66)
    log(f"⏱️  ELAPSED      : {elapsed:.1f} min ({elapsed/60:.2f}h)")
    log(f"📤 SENT         : {sent}  (💬 text:{sent-img}  📷 image:{img})")
    log(f"✏️  EDITS        : {edits}  (typo-fix edits after post)")
    log(f"❌ ERRORS       : {err}")
    log(f"⏭️  SKIPPED      : {skip}  (cooldown + random skip + error backoff)")
    log(f"💭 DISTRACTIONS : {distractions} random human pauses")
    with _state_lock:
        bl = len(_blocked_variations)
    if bl:
        log(f"🚫 BLACKLISTED  : {bl} message variations (auto-learned as blocked by anti-spam)")
        log("   → These will not be reused in future runs (persisted to gist if configured).")
    if sent > 0 and elapsed > 0:
        log(f"📈 POST RATE    : {sent/(elapsed/60):.1f} msg/hour")
    if err > 0 and sent + err > 0:
        err_pct = err/(sent+err)*100
        warn = "  ⚠️ high — review errors above" if err_pct > 20 else ""
        log(f"⚠️  ERROR RATE   : {err_pct:.1f}%{warn}")
    if sent > 0 and err == 0 and skip > sent * 2:
        log("💡 NOTE: Most cycles were skips (cooldown or random) — this is normal for human-like cadence.")
    log("")
    log("📂 Per-channel breakdown:")
    for cid in CHANNEL_IDS:
        s = per_ch[cid]
        name = ch_names.get(cid, "?") if ch_names else "?"
        log(f"   #{name} ({cid}):")
        log(f"      ✅ sent={s['sent']}  (💬{s['txt']}/📷{s['img']}/✏️{s['edits']})  "
            f"❌err={s['errors']}  ⏭️skip={s['skipped']}  🔁cooldown={s['cooldown']}")
    log("=" * 66)


if __name__ == "__main__":
    if _SELF_TEST:
        CLIENT_BUILD = _DEFAULT_BUILD
        _CHROME_VER = _CHROME_VERSION_FALLBACK
        try:
            SESSION.cookies.set("locale", DISCORD_LOCALE, domain="discord.com")
        except Exception:
            pass
        self_test()
    else:
        main()
