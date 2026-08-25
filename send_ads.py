"""
Discord Marketplace Ad Sender  v5.1  (self-bot / alt account)
==============================================================
Sends ONE ad (SELL or BUY, chosen at workflow start) to marketplace channels
with human-like timing, browser-grade TLS/HTTP2 fingerprint (curl_cffi
impersonating Chrome), WebSocket gateway connection (real online presence),
cookie+fingerprint warmup, smart cooldown (only reposts when others have
posted after you), image EXIF strip + hash randomization, and post-send
typo-fix edits for extra realism.

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
  15. Outbound IP check on startup (warns on Azure/datacenter IPs)
  16. Channel randomization order; inter-post "glance elsewhere" reads
  17. Proper 429 rate-limit handling (global cooldown, bounded backoff)
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
from datetime import datetime
from pathlib import Path
from collections import defaultdict

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
        print(f"[{_ts()}] ❌ Required env var '{name}' not set.", file=sys.stderr)
        sys.exit(1)
    return v

def _int(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log(f"⚠️ '{name}'='{raw}' invalid, using {default}")
        return default

def _float(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log(f"⚠️ '{name}'='{raw}' invalid, using {default}")
        return default

def _bool(name, default=False):
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "yes", "true", "on", "y")

# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
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
TYPO_EDIT_CHANCE  = _float("TYPO_EDIT_CHANCE", 0.12)
SUPPRESS_EMBEDS   = _bool("SUPPRESS_EMBEDS", False)
IMAGE_JITTER      = _bool("IMAGE_JITTER", True)

if MIN_AFK_BREAKS < 0: MIN_AFK_BREAKS = 0
if MAX_AFK_BREAKS < MIN_AFK_BREAKS: MAX_AFK_BREAKS = MIN_AFK_BREAKS
if AFK_MIN_MIN < 1: AFK_MIN_MIN = 1
if AFK_MAX_MIN < AFK_MIN_MIN: AFK_MAX_MIN = AFK_MIN_MIN
if INTERVAL_MIN < 2:
    log(f"⚠️ INTERVAL_MIN={INTERVAL_MIN} too small, clamping to 2")
    INTERVAL_MIN = 2

if AD_TYPE not in ("sell", "buy"):
    log(f"❌ AD_TYPE must be 'sell' or 'buy', got '{AD_TYPE}'")
    sys.exit(1)

DISCORD_MSG_LIMIT = 2000
if len(MESSAGE) > DISCORD_MSG_LIMIT:
    log(f"❌ Message is {len(MESSAGE)} chars (limit {DISCORD_MSG_LIMIT})")
    sys.exit(1)

if not CHANNEL_IDS:
    log("❌ No valid CHANNEL_IDS (empty list after parsing)")
    sys.exit(1)

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
    """Visit discord.com like a fresh browser, collect cookies + fingerprint,
    then attach Discord auth + browser headers."""
    global _X_FINGERPRINT, _UA, CLIENT_BUILD, _CHROME_VER
    log("🔑 Warming up browser session (cookies + fingerprint)...")
    try:
        # Step 1: hit discord.com/ (like the browser first navigating)
        SESSION.get("https://discord.com/", timeout=15)
        time.sleep(random.uniform(0.5, 1.2))
        # Step 2: hit /app (SPA bootstrap; sets more cookies)
        r = SESSION.get("https://discord.com/app", timeout=15)
        time.sleep(random.uniform(0.6, 1.3))
        # Step 3: scrape build + real UA from the app response
        CLIENT_BUILD, _CHROME_VER = _scrape_build_number_and_ua(SESSION)
        # Step 4: hit experiments endpoint (gives x-fingerprint)
        r2 = SESSION.get("https://discord.com/api/v9/experiments", timeout=10)
        if r2.status_code == 200:
            try:
                _X_FINGERPRINT = r2.json().get("fingerprint")
            except Exception:
                pass
        # Step 5: also hit science (browser telemetry endpoint, harmless)
        try:
            SESSION.post("https://discord.com/api/v9/science",
                         json={"events": [], "client_track_timestamp": int(time.time()*1000)},
                         timeout=5)
        except Exception:
            pass
        # Step 6: set locale cookie if not already
        has_locale = any(c.name == "locale" for c in SESSION.cookies.jar)
        if not has_locale:
            try:
                SESSION.cookies.set("locale", DISCORD_LOCALE, domain="discord.com")
            except Exception:
                pass
    except Exception as e:
        log(f"   ⚠️ Warmup error ({type(e).__name__}) -- continuing anyway")
        CLIENT_BUILD, _CHROME_VER = _DEFAULT_BUILD, _CHROME_VERSION_FALLBACK

    # Attach the Discord-specific request headers on top of curl_cffi defaults
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
    log(f"   UA: Chrome {cv_major}")
    log(f"   Build: {CLIENT_BUILD}")
    log(f"   Fingerprint: {'OK' if _X_FINGERPRINT else 'not received'}")
    log(f"   Cookies: {len(list(SESSION.cookies))}")

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
    now = time.time()
    if _global_cooldown_until > now:
        wait = _global_cooldown_until - now + random.uniform(0.5, 2.0)
        dbg(f"   ⏳ Global cooldown {wait:.1f}s")
        time.sleep(wait)

def _make_nonce():
    """Nonce: Discord web uses snowflake-ish integers or short random hex.
    Use a snowflake derived from time to look plausible."""
    # Discord snowflake = (millis - DISCORD_EPOCH) << 22 | worker | pid | incr
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
    # Idempotency key for message POSTs (and PUT reactions)
    if method.upper() == "POST" and ("/messages" in url or "/typing" in url) and not url.endswith("/typing"):
        if "/reactions/" not in url and "/ack" not in url:
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
            # For multipart (image) uploads, don't retry with same mime body
            # because curl has consumed it. Close after first attempt.
            is_multipart = files_mp is not None
            if is_multipart:
                try:
                    files_mp.close()
                except Exception:
                    pass
                files_mp = None
        except Exception as e:
            short = url.split("/api/")[-1][:60] if "/api/" in url else url[-40:]
            log(f"   ⚠️ Net error ({method} {short}): {type(e).__name__} (attempt {attempt}/{retries})")
            if attempt < retries:
                time.sleep(3 * attempt + random.uniform(0, 1))
                continue
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
            scope = "GLOBAL" if is_global else f"bucket {d.get('bucket','?')}"
            log(f"   ⚠️ Rate limit ({scope}), waiting {this_wait:.1f}s [streak {_429_streak}]")
            if is_global:
                _global_cooldown_until = max(_global_cooldown_until, time.time() + raw_wait)
            if _429_streak >= 6:
                log("   ⚠️ Too many 429s -- returning")
                return r
            time.sleep(this_wait)
            continue

        if 500 <= r.status_code < 600 and attempt < retries:
            log(f"   ⚠️ Server error {r.status_code} (attempt {attempt}/{retries}), retrying")
            time.sleep(3 * attempt + random.uniform(0, 2))
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
    """Return (filename, bytes, mime). Strips EXIF, randomizes filename,
    jitters JPEG quality, applies tiny pixel-level perturbation so each
    upload has a unique content hash (defeats hash-based spam filters)."""
    ext = Path(original_name).suffix.lower() or ".png"
    mime = _IMG_EXTS.get(ext, "image/png")
    fname = _random_img_name(original_name)

    if not _HAS_PIL or not STRIP_EXIF:
        return fname, raw_bytes, mime

    try:
        im = Image.open(io.BytesIO(raw_bytes))
        out = io.BytesIO()

        # Tiny pixel jitter: ±1 on a few random pixels so content hash differs
        # but the image is visually identical (only if IMAGE_JITTER enabled
        # and image isn't huge — skip for GIF to preserve animation)
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
    r = api("GET", "https://discord.com/api/v9/users/@me", retries=2)
    if r.status_code == 200:
        try:
            return r.json(), None
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
    log(f"❌ Token validation failed (status {r.status_code}, reason={reason}): {msg}")
    return None, reason

def set_status():
    if not CUSTOM_STATUS_TEXT:
        return False
    payload = {"custom_status": {"text": CUSTOM_STATUS_TEXT}}
    if STATUS_EMOJI:
        payload["custom_status"]["emoji_name"] = STATUS_EMOJI
    try:
        r = api("PATCH", "https://discord.com/api/v9/users/@me/settings",
                json_body=payload, retries=2)
        if r.status_code == 200:
            log(f"🟢 Custom status: '{STATUS_EMOJI} {CUSTOM_STATUS_TEXT}'")
            return True
        log(f"⚠️ Status set failed ({r.status_code})")
    except Exception as e:
        log(f"⚠️ Status error: {e}")
    return False

def keepalive():
    try:
        api("GET", "https://discord.com/api/v9/users/@me", retries=1)
        dbg("💓 REST keepalive")
    except Exception:
        pass

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
    msgs = get_last_messages(cid, 5)
    if msgs is None or len(msgs) == 0:
        return True, "?", "?", None
    last = msgs[0]
    last_author = last.get("author", {}).get("username", "?")
    last_author_id = last.get("author", {}).get("id")
    snip = (last.get("content") or "").replace("\n", " ")[:40] or "<embed/image/empty>"
    return (last_author_id == my_id), last_author, snip, msgs

my_last_msg_id = {}

def read_channel(cid):
    msgs = get_last_messages(cid, 10)
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
# WebSocket gateway (lightweight presence connection)                         #
# --------------------------------------------------------------------------- #
class GatewayThread(threading.Thread):
    """Connects to the Discord gateway, IDENTIFYs as the user, sends
    heartbeats, and updates presence. Makes the account appear online
    to other users (and to anti-bot systems checking active sessions).

    Runs in a daemon thread; failures don't crash the main loop.
    """
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
        cv = _CHROME_VER
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
                # Capabilities bitmask matching modern Discord web client
                # (LAZY_USER_NOTES | VERSIONED_READ_STATES | VERSIONED_USER_GUILD_SETTINGS
                #  | DEDUPE_USER_OBJECTS | PRIORITIZED_READY_PAYLOAD | MULTIPLE_GUILD_EXPERIMENT_POPULATIONS
                #  | NON_CHANNEL_READ_STATES | AUTH_TOKEN_REFRESH | USER_SETTINGS_PROTO | CLIENT_STATE_V2
                #  | PASSIVE_GUILD_UPDATE | DEBOUNCE_MESSAGE_REACTIONS | PASSIVE_GUILD_UPDATE_V2
                #  | CHANNEL_OBFUSCATION | AUTO_LOBBY_CONNECT)
                "capabilities": 16381 | 32768 | 65536,
                "presence": {
                    "status": "online",
                    "since": 0,
                    "activities": [{
                        "type": 4,  # CUSTOM_STATUS
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

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            time.sleep(self._hb_interval / 1000.0)
            if self._stop.is_set():
                break
            self._send({"op": 1, "d": self._seq})
            self.dbg(f"💓 WS heartbeat seq={self._seq}")

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

        # Parse proxy from HTTPS_PROXY for websocket-client
        ws_kwargs = {"timeout": 30}
        if HTTPS_PROXY:
            try:
                from urllib.parse import urlparse
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

        # Add browser headers + cookies so the WS upgrade looks like a real client
        cookie_str = "; ".join(
            f"{c.name}={c.value}" for c in SESSION.cookies.jar if c.domain and "discord" in c.domain
        )
        ws_kwargs["header"] = [
            f"User-Agent: {_UA}",
            "Origin: https://discord.com",
            f"Cookie: {cookie_str}",
        ]

        self._ws = _ws.create_connection(url, **ws_kwargs)
        self._ws.settimeout(30)

        # Read HELLO (op 10)
        hello = json.loads(self._ws.recv())
        if hello.get("op") != 10:
            raise RuntimeError(f"Expected HELLO, got op={hello.get('op')}")
        self._hb_interval = hello["d"].get("heartbeat_interval", 41250)
        self.dbg(f"WS hello: hb_interval={self._hb_interval}")

        # Send IDENTIFY
        self._identify()
        self._last_hb_ack = time.time()

        # Start heartbeat thread
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

            if op == 11:  # HEARTBEAT_ACK
                self._last_hb_ack = time.time()
            elif op == 9:  # INVALID_SESSION
                self.dbg("WS invalid session; will reconnect fresh")
                self._session_id = None
                break
            elif op == 7:  # RECONNECT
                self.dbg("WS requested reconnect")
                break
            elif t == "READY":
                self._session_id = d.get("session_id")
                self._resume_url = d.get("resume_gateway_url")
                if not got_ready:
                    got_ready = True
                    self.connected.set()
                    user = d.get("user", {})
                    self.log(f"🟢 Gateway online as {user.get('username','?')} "
                             f"(session {self._session_id[:8] if self._session_id else '?'})")
            # Other events (MESSAGE_CREATE etc) are ignored — we use REST
            # for posting. We just need the presence connection alive.

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
        log("🌐 Gateway: disabled by config")
        return
    if not _HAS_WS:
        log("⚠️ websocket-client not installed; gateway disabled. "
            "pip install websocket-client to enable.")
        return
    try:
        _gw_thread = GatewayThread(USER_TOKEN, CUSTOM_STATUS_TEXT, STATUS_EMOJI, log, dbg)
        _gw_thread.start()
        # Wait up to 15s for READY
        if _gw_thread.connected.wait(timeout=15):
            log("🟢 Gateway presence connected (account appears online)")
        else:
            log("⚠️ Gateway connecting in background (may take a moment)")
    except Exception as e:
        log(f"⚠️ Gateway thread failed to start: {e}")

# --------------------------------------------------------------------------- #
# Typing / sending / editing                                                  #
# --------------------------------------------------------------------------- #
def typing_duration(text):
    words = len(text.split())
    chars = len(text)
    lines = text.count("\n") + 1
    cpm = random.uniform(210, 350)  # slightly more human variance
    d = (chars / cpm) * 60
    if d < 1.3:
        d = random.uniform(1.2, 2.2)
    if d > 9.0:
        d = random.uniform(6.5, 9.0)
    if lines > 2:
        d += random.uniform(1.0, 3.0)
    # Occasional "distracted mid-type" pause
    if random.random() < 0.15:
        d += random.uniform(1.0, 3.0)
    return d

def send_typing(cid, text):
    try:
        time.sleep(random.uniform(1.8, 4.5))
        ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
        api("POST", f"https://discord.com/api/v9/channels/{cid}/typing",
            referer=ref, json_body={}, retries=1)
    except Exception:
        pass
    time.sleep(typing_duration(text))

def _make_message_payload(text, nonce, with_image=False):
    """Build the JSON body for POST /messages."""
    payload = {
        "content": text,
        "tts": False,
        "nonce": nonce,
        "allowed_mentions": {
            "parse": ["users", "roles"],  # deliberately excludes "everyone"
            "replied_user": False,
        },
    }
    flags = 0
    if SUPPRESS_EMBEDS and random.random() < 0.4:
        flags |= 4  # SUPPRESS_EMBEDS
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
    """img = (fname, fbytes, fmime) or None."""
    send_typing(cid, text)
    nonce = _make_nonce()
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    payload = _make_message_payload(text, nonce, with_image=bool(img))
    if img:
        fname, fbytes, fmime = img
        mp = _build_multipart(payload, fname, fbytes, fmime)
        r = api("POST", f"https://discord.com/api/v9/channels/{cid}/messages",
                referer=ref, files_mp=mp, retries=3)
    else:
        r = api("POST", f"https://discord.com/api/v9/channels/{cid}/messages",
                referer=ref, json_body=payload, retries=3)
    if r.status_code == 200:
        try:
            msg = r.json()
            return True, 200, "", msg.get("id"), msg
        except Exception:
            return True, 200, "", None, None
    try:
        err = r.json().get("message", getattr(r, "text", ""))[:120]
    except Exception:
        err = str(getattr(r, "status_code", "?"))
    return False, r.status_code, err, None, None

def edit_message(cid, msg_id, new_text):
    """Simulate a 'typo correction' edit 5-25s after posting."""
    ref = f"https://discord.com/channels/{_guild_id_cache.get(cid,'@me')}/{cid}"
    payload = {"content": new_text}
    try:
        # Small "thinking" gap before editing (like noticing a typo)
        time.sleep(random.uniform(5, 22))
        # Fire typing briefly to show we noticed
        api("POST", f"https://discord.com/api/v9/channels/{cid}/typing",
            referer=ref, json_body={}, retries=1)
        time.sleep(random.uniform(1.0, 2.5))
        r = api("PATCH", f"https://discord.com/api/v9/channels/{cid}/messages/{msg_id}",
                referer=ref, json_body=payload, retries=2)
        return r.status_code in (200, 204)
    except Exception:
        return False

def maybe_typo_edit(cid, msg_id, original_text):
    """Occasionally apply a tiny realistic edit to our own just-posted message."""
    if not msg_id:
        return
    if random.random() > TYPO_EDIT_CHANCE:
        return
    if "\n" in original_text or len(original_text) < 8:
        return
    new_text = original_text
    # 1. Add/remove a trailing period
    if random.random() < 0.35:
        if new_text.endswith((".", "!", "?")):
            pass
        elif random.random() < 0.5:
            new_text = new_text.rstrip() + "."
    # 2. Fix a "double space" by removing one, or vice versa
    if random.random() < 0.25 and "  " in new_text:
        new_text = new_text.replace("  ", " ", 1)
    # 3. Swap a common abbreviation case
    swaps = [("DM", "dm"), ("dm", "DM"), ("LF", "lf"), ("lf", "LF"),
             ("BB", "bb"), ("bb", "BB"), ("QUICK", "quick"), ("quick", "QUICK")]
    if random.random() < 0.3:
        a, b = random.choice(swaps)
        if a in new_text:
            new_text = new_text.replace(a, b, 1)
    # 4. Add an extra emoji (looks like "oh right, add flair")
    if random.random() < 0.25 and len(new_text) < DISCORD_MSG_LIMIT - 5:
        new_text = new_text.rstrip() + random.choice([" 🔥", " ⚡", " 💸", " ✅"])
    if new_text == original_text or len(new_text) > DISCORD_MSG_LIMIT:
        return
    ok = edit_message(cid, msg_id, new_text)
    if ok:
        snip = new_text.replace("\n", " ⏎ ")[:40]
        log(f"   ✏️  edited msg to \"{snip}...\"")

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
        # No-emoji variants (look very human)
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
    # De-dupe and put base first
    if base in uniq:
        uniq.remove(base)
    uniq.insert(0, base)
    return uniq

# --------------------------------------------------------------------------- #
# Image loading                                                               #
# --------------------------------------------------------------------------- #
def load_image():
    if not IMAGE_PATH or not ATTACH_IMAGE:
        return None, None
    p = Path(IMAGE_PATH).expanduser()
    if not p.exists():
        log(f"⚠️ Image not found at {IMAGE_PATH} -- text-only")
        return None, None
    try:
        data = p.read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        size_mb = len(data) / 1024 / 1024
        if size_mb > 8:
            log(f"⚠️ Image {size_mb:.2f}MB exceeds 8MB -- text-only")
            return None, None
        log(f"🖼️  Image loaded: {p.name} ({size_mb:.2f}MB, {mime})")
        if _HAS_PIL and STRIP_EXIF:
            log("   (EXIF stripped, filename + bytes randomized per post)")
        return data, p.name
    except Exception as e:
        log(f"⚠️ Failed to read image: {e} -- text-only")
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
            if time.time() - self.last_ping >= 270:
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
            log(f"   👌 reacted {emo} to \"{snip}...\"")
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# IP check                                                                    #
# --------------------------------------------------------------------------- #
def check_proxy_ip():
    if not PROXY_CHECK:
        return
    try:
        r = SESSION.get("https://api.ipify.org?format=json", timeout=10)
        if r.status_code == 200:
            ip = r.json().get("ip", "?")
            org = "?"
            for host in ("ipinfo.io", "ipapi.co"):
                try:
                    r2 = SESSION.get(f"https://{host}/{ip}/json", timeout=8)
                    if r2.status_code == 200:
                        j = r2.json()
                        org = j.get("org") or j.get("asn") or "?"
                        break
                except Exception:
                    continue
            log(f"🌐 Outbound IP: {ip}  ({org})")
            if not HTTPS_PROXY:
                o = str(org).lower()
                if any(kw in o for kw in ("microsoft", "azure", "amazon", "aws",
                                         "google", "ovh", "digitalocean",
                                         "hetzner", "oracle", "linode",
                                         "digital ocean", "github")):
                    log("   ⚠️ Datacenter IP detected! Anti-spam bots (Wick/Carl/Beemo)")
                    log("      may shadow-delete your messages. Use a residential proxy")
                    log("      (HTTPS_PROXY secret) or a self-hosted GitHub runner.")
    except Exception as e:
        log(f"   (IP check failed: {type(e).__name__})")

# --------------------------------------------------------------------------- #
# Self-tests                                                                  #
# --------------------------------------------------------------------------- #
def self_test():
    print("=" * 60)
    print("🧪 Self-test (v5.1, no network calls)")
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

    print()
    print("=" * 60)
    print("🎉 ALL SELF-TESTS PASSED (v5.1)")
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

    last_sent = {}
    slowmodes = {}
    channel_errors = defaultdict(int)
    dead_channels = set()
    stats = defaultdict(lambda: {"sent": 0, "errors": 0, "skipped": 0,
                                 "cooldown": 0, "img": 0, "txt": 0, "edits": 0})
    total_sent = total_err = total_skip = total_distractions = 0
    total_img = total_edits = 0
    cycle = 0
    sent_count_global = 0

    log("=" * 66)
    log(f"🎯 Marketplace Ad Sender  v5.1  ({AD_TYPE.upper()})")
    log("=" * 66)
    log(f"Channels    : {len(CHANNEL_IDS)}")
    for c in CHANNEL_IDS:
        log(f"   • {c}")
    log(f"Interval    : ~{INTERVAL_MIN} min/channel (±25% jitter)")
    log(f"Runtime     : {TOTAL_RUN_MIN:.0f} min ({TOTAL_RUN_MIN/60:.1f}h)")
    log(f"End time    : {datetime.fromtimestamp(run_end).strftime('%Y-%m-%d %H:%M:%S')}")
    first_line = MESSAGE.split('\n')[0]
    log(f"Message     : \"{first_line[:70]}{'...' if len(first_line)>70 else ''}\"")
    log(f"              ({len(MESSAGE)} chars, {len(variations)} variations)")
    log(f"Image       : {'yes' if use_img_ever else 'no'}"
        + (f" (text-only warmup: first {WARMUP_POSTS} posts)" if use_img_ever else ""))
    log(f"Auto-delete : NO (messages stack naturally)")
    log(f"Smart wait  : YES (repost only when others have posted)")
    log(f"Gateway WS  : {'enabled' if ENABLE_GATEWAY and _HAS_WS else 'disabled'}")
    log(f"Typo edits  : {TYPO_EDIT_CHANCE*100:.0f}% chance after post")
    log(f"Reactions   : {'on' if RANDOM_REACT else 'off'} (~{IDLE_REACT_CHANCE*100:.0f}%/cycle)")
    log(f"AFK breaks  : {MIN_AFK_BREAKS}-{MAX_AFK_BREAKS} ({AFK_MIN_MIN:.0f}-{AFK_MAX_MIN:.0f} min)")
    log(f"TLS/HTTP2   : curl_cffi → Chrome impersonation")
    log(f"Debug       : {'ON' if DEBUG else 'OFF'}")
    if HTTPS_PROXY:
        log("Proxy       : ON (HTTPS_PROXY set, creds hidden)")
    log("=" * 66)

    check_proxy_ip()

    # --- Bootup: extended human-like startup sequence ---
    startup_phase1 = random.uniform(8, 20)
    log(f"⏳ Boot delay {startup_phase1:.0f}s (like opening Discord)...")
    sleep_chunked(startup_phase1, run_end)

    me, vreason = validate_token()
    if not me:
        log(f"❌ Could not authenticate (reason: {vreason})")
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, total_img, total_edits, stats)
        sys.exit(1)
    my_id = me.get("id")
    username = me.get("username", "???")
    if not my_id:
        log("❌ Could not read user id")
        sys.exit(1)
    log(f"✅ Logged in  : {username}  (id={my_id})")
    log(f"   Email verified: {me.get('verified',False)} | 2FA: {me.get('mfa_enabled',False)}")
    if not me.get("verified"):
        log("   ⚠️ Email not verified -- higher flag risk")
    if not me.get("mfa_enabled"):
        log("   💡 Tip: enabling 2FA raises account trust score.")

    # Start gateway BEFORE status set (the WS presence is the real "online" signal)
    start_gateway()
    time.sleep(random.uniform(2, 5))
    set_status()

    # Browse/channel-fetch warmup
    log("📡 Browsing channels (warmup reads)...")
    ok_count = 0
    for cid in CHANNEL_IDS:
        info = get_channel_info(cid)
        if not info:
            log(f"   ❌ {cid}: could not fetch info (will skip)")
            dead_channels.add(cid)
            sleep_chunked(random.uniform(2.0, 4.0))
            continue
        name = info.get("name", "?")
        slowmodes[cid] = info.get("rate_limit_per_user", 0)
        sleep_chunked(random.uniform(0.8, 1.8))
        # "Click" channel, read, ACK — with a small gaze time
        read_channel(cid)
        gaze = random.uniform(3.0, 9.0)
        dbg(f"   👀 gazing #{name} for {gaze:.1f}s")
        sleep_chunked(gaze, run_end)
        log(f"   ✅ {cid} → #{name}  slowmode={slowmodes[cid]}s  guild={_guild_id_cache.get(cid,'?')}")
        ok_count += 1

    active_channels = [c for c in CHANNEL_IDS if c not in dead_channels]
    if ok_count == 0:
        log("❌ No accessible channels -- check token and CHANNEL_IDS")
        sys.exit(1)
    if dead_channels:
        log(f"⚠️ {len(dead_channels)}/{len(CHANNEL_IDS)} channels inaccessible; skipping them")

    # Longer "reading chat" warmup before first post
    warmup_wait = random.uniform(40, 90)
    log(f"👀 Reading chat for {warmup_wait:.0f}s before first post...")
    sleep_chunked(warmup_wait, run_end)
    # One more read pass so it looks like we're scrolling
    for cid in active_channels:
        read_channel(cid)
        sleep_chunked(random.uniform(2.0, 6.0))
    sleep_chunked(random.uniform(8, 20), run_end)

    breaks = plan_breaks(TOTAL_RUN_MIN * 60)
    log(f"☕ AFK breaks : {len(breaks)} scheduled")
    for s, e in breaks:
        log(f"   • {datetime.fromtimestamp(s).strftime('%H:%M')} → "
            f"{datetime.fromtimestamp(e).strftime('%H:%M')} ({(e-s)/60:.0f} min)")

    log("🚀 Starting.")
    log("👉 VERIFY manually: after first post, open Discord and confirm you")
    log("   can see your ad. If you can't see it, anti-spam is deleting it --")
    log("   cancel, verify phone on the alt, and/or use a residential proxy.")
    log("")

    try:
        while time.time() < run_end:
            cycle += 1
            now = time.time()
            remaining_min = (run_end - now) / 60

            in_afk, afk_left = in_break(breaks, now)
            if in_afk:
                log(f"☕ AFK break -- {afk_left/60:.1f} min left")
                sleep_with_keepalive(min(60, afk_left), run_end)
                continue

            if cycle > 1 and random.random() < 0.10:
                dist = random.uniform(60, 300)
                total_distractions += 1
                log(f"💭 Distraction -- {dist:.0f}s (checking DMs/other servers)")
                sleep_with_keepalive(dist, run_end)
                if time.time() >= run_end:
                    break

            direction = "💰SELL" if AD_TYPE == "sell" else "🛒BUY"
            img_status = f"| image after {WARMUP_POSTS - sent_count_global} warmup posts" \
                         if use_img_ever and sent_count_global < WARMUP_POSTS else ""
            log(f"── Cycle {cycle} [{direction}] | {remaining_min:.0f} min left | {_ts()} {img_status} ──")

            order = active_channels.copy()
            random.shuffle(order)
            channels_posted = 0
            used_variations = set()
            post_threshold = random.uniform(0.85, 0.95)

            for cid in order:
                if time.time() >= run_end:
                    break
                if in_break(breaks, time.time())[0]:
                    break
                if cid in dead_channels:
                    continue

                if channel_errors[cid] >= 3:
                    dbg(f"#{cid}: too many errors ({channel_errors[cid]}), backing off")
                    stats[cid]["skipped"] += 1
                    total_skip += 1
                    channel_errors[cid] = max(0, channel_errors[cid] - 1)
                    continue

                slow = slowmodes.get(cid, 0)
                if slow > 0 and cid in last_sent:
                    elapsed = time.time() - last_sent[cid]
                    need_wait = slow - elapsed + random.uniform(2, 5)
                    if need_wait > 0:
                        dbg(f"#{cid}: slowmode wait {need_wait:.1f}s")
                        sleep_chunked(need_wait, run_end)
                        if time.time() >= run_end:
                            break

                # Read with cache-buster if we have a previous msg here
                do_refresh = (cid in my_last_msg_id)
                if do_refresh:
                    recent = get_last_messages(cid, 5, force_refresh=True)
                    if recent is not None and len(recent) > 0:
                        last2 = recent[0]
                        i_am_last = (last2.get("author", {}).get("id") == my_id)
                        last_author = last2.get("author", {}).get("username", "?")
                        last_snip = (last2.get("content") or "").replace("\n", " ")[:40] \
                                    or "<embed/image/empty>"
                    else:
                        i_am_last, last_author, last_snip, recent = True, "?", "?", None
                else:
                    i_am_last, last_author, last_snip, recent = am_i_last(cid, my_id)

                # Delete detection
                prev_id = my_last_msg_id.get(cid)
                deleted_detected = False
                if prev_id and recent is not None:
                    recent_ids = {m.get("id") for m in recent}
                    if prev_id not in recent_ids:
                        deleted_detected = True
                        log(f"   ⚠️ #{cid}: previous ad appears DELETED (anti-spam) -- reposting")

                # Safety-net force-post
                force_post = False
                if cid in last_sent:
                    since_last = time.time() - last_sent[cid]
                    max_wait = max(INTERVAL_MIN*60*2.5, slowmodes.get(cid,0) + 120)
                    if since_last > max_wait:
                        force_post = True
                        dbg(f"#{cid}: safety net force-post (last sent {since_last:.0f}s ago)")

                if i_am_last and not deleted_detected and not force_post:
                    stats[cid]["cooldown"] += 1
                    stats[cid]["skipped"] += 1
                    total_skip += 1
                    log(f"   ⏭️  #{cid}: our ad still latest (by {last_author}), waiting")
                    dbg(f"      last msg: \"{last_snip}\"")
                    if recent is not None:
                        maybe_react(cid, recent[:5], my_id)
                    sleep_chunked(random.uniform(4, 10), run_end)
                    continue

                if random.random() > post_threshold:
                    stats[cid]["skipped"] += 1
                    total_skip += 1
                    log(f"   ↪️ #{cid}: skipped this pass (human-like)")
                    sleep_chunked(random.uniform(4, 10), run_end)
                    continue

                available = [v for v in variations if v not in used_variations]
                if not available:
                    used_variations.clear()
                    available = variations[:]
                msg = random.choice(available)

                attach_this_post = False
                if use_img_ever and sent_count_global >= WARMUP_POSTS:
                    attach_this_post = random.random() < 0.80
                elif use_img_ever:
                    log(f"   🔰 warmup post ({sent_count_global+1}/{WARMUP_POSTS}) -- text-only")

                img_payload = None
                if attach_this_post:
                    img_payload = make_image_payload(raw_image, image_name)

                ok, code, err, new_msg_id, msg_obj = send_message(cid, msg, img_payload)
                if ok:
                    used_variations.add(msg)
                    total_sent += 1
                    sent_count_global += 1
                    stats[cid]["sent"] += 1
                    if attach_this_post:
                        stats[cid]["img"] += 1
                        total_img += 1
                    else:
                        stats[cid]["txt"] += 1
                    last_sent[cid] = time.time()
                    if new_msg_id:
                        my_last_msg_id[cid] = new_msg_id
                    channel_errors[cid] = 0
                    channels_posted += 1
                    snip = msg.replace("\n", " ⏎ ")[:55]
                    tag = "📷" if attach_this_post else "💬"
                    log(f"   ✅ #{cid}: {tag} \"{snip}{'...' if len(snip)>=55 else ''}\" "
                        f"(total: {total_sent}, id={new_msg_id})")

                    # Post-send: typo edit in background thread (so we don't block loop)
                    if new_msg_id and random.random() < TYPO_EDIT_CHANCE:
                        t = threading.Thread(
                            target=lambda: (
                                maybe_typo_edit(cid, new_msg_id, msg)
                                or None
                            ),
                            daemon=True,
                        )
                        t.start()
                        stats[cid]["edits"] += 1
                        total_edits += 1
                else:
                    total_err += 1
                    stats[cid]["errors"] += 1
                    channel_errors[cid] += 1
                    log(f"   ❌ #{cid}: FAILED ({code}: {err})")
                    if code in (401, 403):
                        recheck, rr = validate_token()
                        if recheck is None and rr == "invalid":
                            log("\n❌ CRITICAL: Token invalidated -- likely banned. Stopping.")
                            _print_stats(start, total_sent, total_err, total_skip,
                                         total_distractions, total_img, total_edits, stats)
                            sys.exit(2)
                        elif recheck is None:
                            log(f"   ⚠️ #{cid}: {code} but re-validation failed ({rr}); channel error.")
                        else:
                            log(f"   ⚠️ #{cid}: channel 403 but token valid; marking dead")
                            dead_channels.add(cid)
                            if cid in active_channels:
                                active_channels.remove(cid)
                    elif code == 404:
                        log(f"   ⚠️ #{cid}: 404 -- channel deleted; marking dead")
                        dead_channels.add(cid)
                        if cid in active_channels:
                            active_channels.remove(cid)

                if time.time() >= run_end:
                    break
                # Post-send "glance elsewhere" or short linger
                if random.random() < 0.40 and len(active_channels) > 1:
                    other = random.choice([c for c in active_channels if c != cid])
                    sleep_chunked(random.uniform(3, 7), run_end)
                    read_channel(other)
                    sleep_chunked(random.uniform(3, 8), run_end)
                elif random.random() < 0.25:
                    sleep_chunked(random.uniform(20, 55), run_end)
                else:
                    sleep_chunked(random.uniform(8, 22), run_end)

            if channels_posted == 0:
                log("   (no posts this cycle)")

            if time.time() >= run_end:
                break
            wait_s = INTERVAL_MIN*60 * random.uniform(0.85, 1.35)
            nt = datetime.fromtimestamp(time.time() + wait_s).strftime("%H:%M")
            log(f"   ⏳ Next ~{nt} (in {wait_s/60:.1f} min)")
            sleep_with_keepalive(wait_s, run_end)

    except KeyboardInterrupt:
        log("\n🛑 Stopped by user")
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, total_img, total_edits, stats)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        log(f"\n💥 Unhandled error: {type(e).__name__}: {e}")
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, total_img, total_edits, stats)
        raise

    log("\n🏁 Reached scheduled end time.")
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
    log(f"Elapsed     : {elapsed:.1f} min ({elapsed/60:.2f}h)")
    log(f"Sent        : {sent}  (💬 text:{sent-img}, 📷 img:{img})")
    log(f"Edits       : {edits}  (typo-fixes)")
    log(f"Errors      : {err}")
    log(f"Skipped     : {skip}  (cooldown + random)")
    log(f"Distractions: {distractions} random pauses")
    if sent > 0 and elapsed > 0:
        log(f"Rate        : {sent/(elapsed/60):.1f} msg/hour")
    if err > 0 and sent + err > 0:
        log(f"Err rate    : {err/(sent+err)*100:.1f}%")
    log("Per channel:")
    for cid in CHANNEL_IDS:
        s = per_ch[cid]
        log(f"   {cid}: ✅{s['sent']} (💬{s['txt']}/📷{s['img']}/✏️{s['edits']})  "
            f"❌{s['errors']}  ⏭️{s['skipped']} (cooldown {s['cooldown']})")
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
