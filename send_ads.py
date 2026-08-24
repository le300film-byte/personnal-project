"""
Discord Marketplace Ad Sender  v4.1  (self-bot / alt account)
==============================================================
Sends ONE ad (SELL or BUY, chosen at workflow start) to marketplace channels
with human-like timing and behavior. Messages stack naturally (no auto-delete),
and smart cooldown only reposts when other people have posted after you.

ANTI-DETECTION / HUMAN-BEHAVIOR LAYERS (v4.1):

  TIMING:
    - Random ±25% jitter on main cycle interval
    - Startup delay 25-70s before doing anything (simulates opening Discord)
    - Warmup phase after startup: reads all channels (lightweight fetches),
      waits 15-40s (simulates scrolling through servers), THEN starts posting
    - Gap between channels 4-14s, with 15% chance of a longer 20-45s "reading"
      pause (like a human stopping to read a thread)
    - 10% chance per cycle of a 1-5 minute "distraction" pause mid-cycle
      (simulates getting a DM, reacting to something, getting a drink)
    - Scheduled AFK breaks 10-30 minutes long (simulates meals/trades/afk)
    - 2-5 minute gap between 6h chunks (simulates a reconnect/restart)
    - Typing indicator duration scales with message length (short = 1.5-3s,
      multi-line bullets = 4-8s) — humans take longer to type longer messages

  MESSAGES:
    - 47+ variations per ad (emoji prefix/suffix for single-line; light
      emoji-only prefixes for multi-line bullet ads to preserve formatting)
    - Each channel per cycle gets a DIFFERENT variation (not all same copy)
    - 85-95% probability per channel per cycle (occasionally skip a channel
      — humans don't post to every single channel every single pass)
    - Original unmodified message always included as one of the variations

  NETWORK / FINGERPRINT:
    - Persistent requests.Session() with cookie jar (cookies set by Discord
      persist across requests, connections reused via keep-alive)
    - Real Chrome User-Agent + X-Super-Properties (matching build number)
    - Accept/Accept-Language/Origin/Referer headers set correctly
    - X-Discord-Locale and X-Discord-Timezone headers (minor fingerprints)
    - During long AFK breaks, tiny keepalive every ~5 minutes (doesn't go
      radio-silent for 30 minutes which looks like a dead TCP connection)
    - Exponential backoff on network errors (3s, 6s, 9s)
    - Proper rate-limit handling (reads retry_after from Discord response)

  PRESENCE:
    - Custom text status set via HTTP (e.g. "💰 Trading")
    - Smart cooldown: only post when your previous ad isn't the last message
      (no stacking, but no deleting either)
    - Slowmode detection and respect (per-channel, tracked by timestamp)
    - Ban detection (stops immediately on 401/403, does NOT keep retrying)

ENVIRONMENT VARIABLES:
  USER_TOKEN     (REQUIRED) Alt account token
  CHANNEL_IDS    (REQUIRED) Comma-separated channel IDs
  AD_TYPE        (REQUIRED) "sell" or "buy"
  MESSAGE        (REQUIRED) Exact ad text (can be multi-line)
  ATTACH_IMAGE   "yes" or "no" — attach IMAGE_PATH?
  INTERVAL_MIN   Minutes between posts per channel (default: 5)
  TOTAL_RUN_MIN  Runtime minutes (default: 360 = 6h)
  IMAGE_PATH     Path to image file to attach (optional)
  CUSTOM_STATUS_TEXT  Custom status (default: "💰 Trading")
  STATUS_EMOJI        Status emoji (default: "💰")
  MIN_AFK_BREAKS / MAX_AFK_BREAKS  AFK count (default 2-4 per 6h)
  AFK_MIN_MIN / AFK_MAX_MIN        AFK duration (default 10-30 min)
  DEBUG          "1" for verbose logs
"""

import os
import sys
import time
import json
import random
import mimetypes
import base64
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import requests

# -----------------------------------------------------------------------
# Self-test dummy env
# -----------------------------------------------------------------------
_SELF_TEST = "--self-test" in sys.argv
if _SELF_TEST:
    os.environ.setdefault("USER_TOKEN", "FAKE_TOKEN_FOR_SELF_TEST")
    os.environ.setdefault("CHANNEL_IDS", "000000000000000000,111111111111111111")
    os.environ.setdefault("AD_TYPE", "sell")
    os.environ.setdefault("MESSAGE", "SELLING BB LF 2.5$/1K DM ME QUICK")
    os.environ.setdefault("ATTACH_IMAGE", "no")

# -----------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------
def _ts():
    return datetime.now().strftime("%H:%M:%S")

def log(m):
    print(f"[{_ts()}] {m}", flush=True)

def dbg(m):
    pass  # rebound after DEBUG loads

# -----------------------------------------------------------------------
# Config helpers
# -----------------------------------------------------------------------
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

# ---- Load config ----
USER_TOKEN    = _required("USER_TOKEN")
CHANNEL_IDS   = [c.strip() for c in _required("CHANNEL_IDS").split(",") if c.strip()]
AD_TYPE       = _required("AD_TYPE").lower()
MESSAGE       = _required("MESSAGE")
ATTACH_IMAGE  = _env("ATTACH_IMAGE", "no").lower() in ("1", "yes", "true", "on")
INTERVAL_MIN  = _float("INTERVAL_MIN", 5)
TOTAL_RUN_MIN = _float("TOTAL_RUN_MIN", 360)
IMAGE_PATH    = _env("IMAGE_PATH")
CUSTOM_STATUS_TEXT = _env("CUSTOM_STATUS_TEXT", "💰 Trading")
STATUS_EMOJI       = _env("STATUS_EMOJI", "💰")
MIN_AFK_BREAKS = _int("MIN_AFK_BREAKS", 2)
MAX_AFK_BREAKS = _int("MAX_AFK_BREAKS", 4)
AFK_MIN_MIN   = _float("AFK_MIN_MIN", 10)
AFK_MAX_MIN   = _float("AFK_MAX_MIN", 30)
DEBUG         = _env("DEBUG", "0") in ("1", "true", "yes", "on")
DISCORD_LOCALE    = _env("DISCORD_LOCALE", "en-US")
DISCORD_TIMEZONE  = _env("DISCORD_TIMEZONE", "America/New_York")

def dbg(m):
    if DEBUG:
        print(f"[{_ts()}] [DEBUG] {m}", flush=True)

if AD_TYPE not in ("sell", "buy"):
    log(f"❌ AD_TYPE must be 'sell' or 'buy', got '{AD_TYPE}'")
    sys.exit(1)

DISCORD_MSG_LIMIT = 2000
if len(MESSAGE) > DISCORD_MSG_LIMIT:
    log(f"❌ Message is {len(MESSAGE)} chars (limit {DISCORD_MSG_LIMIT})")
    sys.exit(1)

# -----------------------------------------------------------------------
# Persistent HTTP session (cookies + keep-alive, like a real browser)
# -----------------------------------------------------------------------
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
_SUPER_PROPERTIES = base64.b64encode(json.dumps({
    "os": "Windows", "browser": "Chrome", "device": "",
    "system_locale": DISCORD_LOCALE, "browser_user_agent": _UA,
    "browser_version": "127.0.0.0", "os_version": "10",
    "referrer": "", "referring_domain": "",
    "referrer_current": "", "referring_domain_current": "",
    "release_channel": "stable", "client_build_number": 321520,
    "client_event_source": None,
}, separators=(",", ":")).encode()).decode()

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": USER_TOKEN,
    "User-Agent": _UA,
    "Accept": "*/*",
    "Accept-Language": f"{DISCORD_LOCALE},en;q=0.9",
    "Origin": "https://discord.com",
    "Referer": "https://discord.com/channels/@me",
    "X-Super-Properties": _SUPER_PROPERTIES,
    "X-Debug-Options": "bugReporterEnabled",
    "X-Discord-Locale": DISCORD_LOCALE,
    "X-Discord-Timezone": DISCORD_TIMEZONE,
})
JSON_HEADERS = {"Content-Type": "application/json"}

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
def sleep_chunked(seconds, end_time=None):
    stop = time.time() + seconds
    while time.time() < stop:
        if end_time and time.time() >= end_time:
            return
        time.sleep(min(5, stop - time.time()))

def api(method, url, retries=3, **kw):
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.request(method, url, timeout=20, **kw)
        except requests.exceptions.RequestException as e:
            log(f"   ⚠️ Net error ({method} {url.split('/api/v9/')[-1]}): {type(e).__name__} "
                f"(attempt {attempt}/{retries})")
            if attempt < retries:
                time.sleep(3 * attempt)
                continue
            fr = requests.Response()
            fr.status_code = 0
            fr._content = str(e).encode()
            return fr
        if r.status_code == 429:
            try:
                d = r.json()
            except Exception:
                d = {}
            wait = float(d.get("retry_after", 8)) + random.uniform(1, 3)
            scope = "GLOBAL" if d.get("global") else f"bucket {d.get('bucket','?')}"
            log(f"   ⚠️ Rate limit ({scope}), waiting {wait:.1f}s")
            time.sleep(wait)
            continue
        return r
    return r

def typing_duration(text):
    """Humans type longer messages slower. Returns seconds to 'type'."""
    lines = text.count("\n") + 1
    words = len(text.split())
    if lines > 3 or words > 20:
        # Long / multi-line message (buy ad with bullets)
        return random.uniform(4.0, 8.0)
    elif words > 10:
        return random.uniform(2.8, 5.0)
    else:
        return random.uniform(1.5, 3.2)

# -----------------------------------------------------------------------
# Discord actions
# -----------------------------------------------------------------------
def validate_token():
    r = api("GET", "https://discord.com/api/v9/users/@me", headers=JSON_HEADERS)
    if r.status_code == 200:
        return r.json()
    log(f"❌ Token invalid (status {r.status_code}): {r.text[:200]}")
    return None

def set_status():
    if not CUSTOM_STATUS_TEXT:
        return False
    payload = {"custom_status": {"text": CUSTOM_STATUS_TEXT}}
    if STATUS_EMOJI:
        payload["custom_status"]["emoji_name"] = STATUS_EMOJI
    try:
        r = api("PATCH", "https://discord.com/api/v9/users/@me/settings",
                headers=JSON_HEADERS, data=json.dumps(payload))
        if r.status_code == 200:
            log(f"🟢 Status: '{STATUS_EMOJI} {CUSTOM_STATUS_TEXT}'")
            return True
        log(f"⚠️ Status set failed ({r.status_code}) — continuing")
    except Exception as e:
        log(f"⚠️ Status error: {e}")
    return False

def keepalive():
    """Tiny request to keep session alive during long silences (like a
    background client ping). GET /users/@me is cheap and looks normal."""
    try:
        api("GET", "https://discord.com/api/v9/users/@me", headers=JSON_HEADERS, retries=1)
        dbg("   💓 keepalive")
    except Exception:
        pass

def get_channel_info(cid):
    r = api("GET", f"https://discord.com/api/v9/channels/{cid}", headers=JSON_HEADERS)
    return r.json() if r.status_code == 200 else {}

def get_last_messages(cid, limit=2):
    r = api("GET", f"https://discord.com/api/v9/channels/{cid}/messages?limit={limit}",
            headers=JSON_HEADERS)
    if r.status_code == 200:
        return r.json()
    dbg(f"get_last_messages failed for {cid}: {r.status_code}")
    return []

def am_i_last(cid, my_id):
    msgs = get_last_messages(cid, 2)
    return bool(msgs) and msgs[0].get("author", {}).get("id") == my_id

def read_channel(cid):
    """Simulate 'opening' a channel: fetches recent messages. This is what
    the client does when you click a channel, and it leaves normal access
    patterns in Discord's logs."""
    msgs = get_last_messages(cid, 10)
    dbg(f"   👁️ read #{cid} ({len(msgs)} msgs)")
    return msgs

def send_typing(cid, text):
    """Send typing indicator, then sleep for a realistic duration based on
    how long the message is."""
    try:
        r = api("POST", f"https://discord.com/api/v9/channels/{cid}/typing",
                headers=JSON_HEADERS, retries=1)
        if r.status_code == 204:
            time.sleep(typing_duration(text))
    except Exception:
        pass

def send_message(cid, text, img=None):
    send_typing(cid, text)
    if img:
        fname, fbytes, fmime = img
        mp = {
            "payload_json": (None, json.dumps({"content": text, "tts": False}),
                             "application/json"),
            "files[0]": (fname, fbytes, fmime),
        }
        hdrs = {k: v for k, v in SESSION.headers.items() if k != "Content-Type"}
        r = api("POST", f"https://discord.com/api/v9/channels/{cid}/messages",
                headers=hdrs, files=mp)
    else:
        r = api("POST", f"https://discord.com/api/v9/channels/{cid}/messages",
                headers=JSON_HEADERS, data=json.dumps({"content": text, "tts": False}))
    if r.status_code == 200:
        return True, 200, ""
    try:
        err = r.json().get("message", r.text)[:120]
    except Exception:
        err = r.text[:120]
    return False, r.status_code, err

# -----------------------------------------------------------------------
# Message variations
# -----------------------------------------------------------------------
_EMOJIS   = ["🔥", "💸", "⚡", "✅", "💰", "🤑", "📈", "💎"]
_SUFFIXES = ["", " ✅", " ⚡", " 🔥", " dm fast", " online now ✅", " quick reply ⚡"]
_PREFIXES = ["", "💸 ", "⚡ ", "🔥 ", "✅ ", "💰 "]

def build_variations(base):
    is_multiline = "\n" in base.strip()
    out = set()
    if is_multiline:
        # Multi-line (buy ad with bullets): only emoji prefixes at top
        for pre in ("", "⚡ ", "🔥 ", "💸 ", "✅ ", "💰 "):
            out.add(f"{pre}{base}")
        for _ in range(6):
            e1, e2 = random.sample(_EMOJIS, 2)
            out.add(f"{e1} {base} {e2}")
    else:
        for pre in _PREFIXES:
            for suf in _SUFFIXES:
                v = f"{pre}{base}{suf}"
                if len(v) <= DISCORD_MSG_LIMIT:
                    out.add(v)
        for _ in range(6):
            e1, e2 = random.sample(_EMOJIS, 2)
            v = f"{e1} {base} {e2}"
            if len(v) <= DISCORD_MSG_LIMIT:
                out.add(v)
    uniq = [v for v in out if len(v) <= DISCORD_MSG_LIMIT]
    if base not in uniq:
        uniq.insert(0, base)
    return uniq

# -----------------------------------------------------------------------
# Image cache
# -----------------------------------------------------------------------
def load_image():
    if not IMAGE_PATH or not ATTACH_IMAGE:
        return None
    p = Path(IMAGE_PATH).expanduser()
    if not p.exists():
        log(f"⚠️ Image not found at {IMAGE_PATH} — text-only")
        return None
    try:
        data = p.read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        size_mb = len(data) / 1024 / 1024
        if size_mb > 8:
            log(f"⚠️ Image {size_mb:.1f}MB exceeds 8MB limit — text-only")
            return None
        log(f"🖼️ Image cached: {p.name} ({size_mb:.2f}MB, {mime})")
        return (p.name, data, mime)
    except Exception as e:
        log(f"⚠️ Failed to read image: {e} — text-only")
        return None

# -----------------------------------------------------------------------
# AFK planner
# -----------------------------------------------------------------------
def plan_breaks(run_seconds):
    n = random.randint(MIN_AFK_BREAKS, MAX_AFK_BREAKS)
    out = []
    min_start = 20 * 60
    margin_end = AFK_MAX_MIN * 60
    gap = 15 * 60
    usable = run_seconds - margin_end
    if usable < min_start + AFK_MIN_MIN * 60:
        return []
    for _ in range(n):
        for _attempt in range(50):
            bs = time.time() + random.uniform(min_start, max(min_start + 1, usable))
            bd = random.uniform(AFK_MIN_MIN, AFK_MAX_MIN) * 60
            be = bs + bd
            ok = True
            for es, ee in out:
                if not (be + gap < es or bs > ee + gap):
                    ok = False
                    break
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

def sleep_with_keepalive(seconds, end_time=None):
    """Like sleep_chunked but sends a tiny keepalive every ~5 min during long
    sleeps so the session doesn't look completely dead (real clients don't
    go radio-silent for 30 minutes straight)."""
    stop = time.time() + seconds
    last_ping = time.time()
    while time.time() < stop:
        if end_time and time.time() >= end_time:
            return
        chunk = min(5, stop - time.time())
        time.sleep(chunk)
        if time.time() - last_ping > 300:  # 5 min
            keepalive()
            last_ping = time.time()

# -----------------------------------------------------------------------
# Self-test
# -----------------------------------------------------------------------
def self_test():
    print("=" * 60)
    print("🧪 Self-test (no network calls)")
    print("=" * 60)

    vs = build_variations("SELLING BB LF 2.5$/1K DM ME QUICK CAN DO SMALL AND BIG AMOUNTS")
    assert len(vs) >= 40
    assert len(set(vs)) == len(vs)
    for v in vs:
        assert len(v) <= DISCORD_MSG_LIMIT
    print(f"✅ Sell variations: {len(vs)} unique")
    assert len(vs) >= 40

    vb = build_variations("BUYING BLADE BALL:\n\n-TOKENS 2.2/1K\n\n-RAP 1.8$/1K (nlf boosted)\n\nDM me quick")
    assert len(vb) >= 6
    for v in vb:
        assert len(v) <= DISCORD_MSG_LIMIT
        assert "\n" in v
    print(f"✅ Buy variations: {len(vb)} unique (multi-line preserved)")

    # Typing duration scales with length
    short = typing_duration("hi")
    long = typing_duration("BUYING BLADE BALL:\n\n-TOKENS 2.2/1K\n\n-TOKENS\n\n-RAP\n\nDM me quick")
    assert long > short, "Long messages should take longer to type"
    print(f"✅ Typing duration scales with length: short={short:.1f}s, long={long:.1f}s")

    # Multipart structure
    fake_img = ("ad.png", b"PNGDATA" * 10, "image/png")
    mp = {
        "payload_json": (None, json.dumps({"content": "test", "tts": False}), "application/json"),
        "files[0]": fake_img,
    }
    hdrs = {k: v for k, v in SESSION.headers.items() if k != "Content-Type"}
    req = requests.Request("POST", "https://discord.com/api/v9/channels/1/messages",
                           headers=hdrs, files=mp).prepare()
    assert req.headers["Content-Type"].startswith("multipart/form-data")
    assert b'name="files[0]"' in req.body
    assert b'filename="ad.png"' in req.body
    print("✅ Multipart image upload: correct structure")

    r2 = requests.Request("POST", "https://discord.com/api/v9/channels/1/messages",
                          headers={**dict(SESSION.headers), **JSON_HEADERS},
                          data=json.dumps({"content": "hi", "tts": False})).prepare()
    assert r2.headers["Content-Type"] == "application/json"
    assert json.loads(r2.body)["content"] == "hi"
    print("✅ JSON text send: correct")

    overlaps = 0
    for seed in range(1000):
        random.seed(seed)
        import unittest.mock as mock
        with mock.patch("time.time", return_value=1_000_000):
            br = plan_breaks(6 * 3600)
        for i in range(len(br) - 1):
            if br[i][1] + 15 * 60 > br[i + 1][0]:
                overlaps += 1
    assert overlaps == 0
    print("✅ AFK planner: zero overlaps across 1000 seeds")

    # Config parsers
    assert _int("NONEXISTENT", 7) == 7
    assert _float("NONEXISTENT", 1.5) == 1.5
    print("✅ Config parsers")

    # Session is a requests.Session
    assert isinstance(SESSION, requests.Session), "Should use requests.Session for cookies"
    assert "Authorization" in SESSION.headers
    assert "X-Super-Properties" in SESSION.headers
    assert "X-Discord-Locale" in SESSION.headers
    assert "X-Discord-Timezone" in SESSION.headers
    print("✅ Session configured with cookie jar, keep-alive, full headers")

    print()
    print("=" * 60)
    print("🎉 ALL SELF-TESTS PASSED")
    print("=" * 60)

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
def main():
    start = time.time()
    run_end = start + TOTAL_RUN_MIN * 60
    variations = build_variations(MESSAGE)
    image_data = load_image()
    use_img = bool(image_data) and ATTACH_IMAGE

    last_sent = {}
    slowmodes = {}
    stats = defaultdict(lambda: {"sent": 0, "errors": 0, "skipped": 0})
    total_sent = total_err = total_skip = total_distractions = 0
    cycle = 0
    warmup_done = False

    log("=" * 66)
    log(f"🎯 Marketplace Ad Sender  v4.1  ({AD_TYPE.upper()})")
    log("=" * 66)
    log(f"Channels    : {len(CHANNEL_IDS)}")
    for c in CHANNEL_IDS:
        log(f"   • {c}")
    log(f"Interval    : ~{INTERVAL_MIN} min per channel (±25% jitter)")
    log(f"Runtime     : {TOTAL_RUN_MIN:.0f} min ({TOTAL_RUN_MIN/60:.1f}h)")
    log(f"End time    : {datetime.fromtimestamp(run_end).strftime('%Y-%m-%d %H:%M:%S')}")
    first_line = MESSAGE.split('\n')[0]
    log(f"Message     : \"{first_line[:70]}{'...' if len(first_line)>70 else ''}\"")
    log(f"              ({len(MESSAGE)} chars, {len(variations)} variations)")
    log(f"Image       : {'yes' if use_img else 'no'}")
    log(f"Auto-delete : NO (stack naturally)")
    log(f"Smart wait  : YES (repost only when others have posted)")
    log(f"Per-channel : 85-95% post prob (occasional skip = human)")
    log(f"Distraction pauses: ~10% chance per cycle (1-5 min)")
    log(f"Warmup read : YES (reads channels before first post)")
    log(f"AFK breaks  : {MIN_AFK_BREAKS}-{MAX_AFK_BREAKS} ({AFK_MIN_MIN:.0f}-{AFK_MAX_MIN:.0f} min)")
    log(f"Debug       : {'ON' if DEBUG else 'OFF'}")
    log("=" * 66)

    if not CHANNEL_IDS:
        log("❌ No CHANNEL_IDS"); sys.exit(1)

    me = validate_token()
    if not me:
        sys.exit(1)
    my_id = me["id"]
    log(f"✅ Logged in  : {me.get('username','???')}  (id={my_id})")
    log(f"   Email verified: {me.get('verified', False)} | 2FA: {me.get('mfa_enabled', False)}")
    if not me.get("verified"):
        log("   ⚠️ Email not verified — higher flag risk")

    set_status()

    log("📡 Checking & reading channels (warmup)...")
    ok_count = 0
    for cid in CHANNEL_IDS:
        info = get_channel_info(cid)
        if not info:
            continue
        slowmodes[cid] = info.get("rate_limit_per_user", 0)
        name = info.get("name", "?")
        read_channel(cid)  # simulate opening the channel
        time.sleep(random.uniform(0.8, 2.5))
        log(f"   ✅ {cid} → #{name}  slowmode={slowmodes[cid]}s")
        ok_count += 1
    if ok_count == 0:
        log("❌ No accessible channels"); sys.exit(1)
    if ok_count < len(CHANNEL_IDS):
        log(f"⚠️ Only {ok_count}/{len(CHANNEL_IDS)} channels accessible")

    # Warmup "reading" pause
    warmup_wait = random.uniform(15, 40)
    log(f"👀 Simulating reading/scroll for {warmup_wait:.0f}s before first post...")
    sleep_chunked(warmup_wait, run_end)

    # Plan AFK breaks after warmup so they're relative to actual first post time
    breaks = plan_breaks(TOTAL_RUN_MIN * 60)
    log(f"☕ AFK breaks : {len(breaks)} scheduled")
    for s, e in breaks:
        log(f"   • {datetime.fromtimestamp(s).strftime('%H:%M')} → "
            f"{datetime.fromtimestamp(e).strftime('%H:%M')} ({(e-s)/60:.0f} min)")

    log("🚀 Starting.\n")

    try:
        while time.time() < run_end:
            cycle += 1
            now = time.time()
            remaining_min = (run_end - now) / 60

            in_afk, afk_left = in_break(breaks, now)
            if in_afk:
                log(f"☕ AFK break — {afk_left/60:.1f} min left")
                sleep_with_keepalive(min(60, afk_left), run_end)
                continue

            # Random distraction pause (10% chance) — like getting a DM or afk briefly
            if cycle > 1 and random.random() < 0.10:
                dist = random.uniform(60, 300)  # 1-5 min
                total_distractions += 1
                log(f"   💭 Distraction pause — {dist:.0f}s (like checking a DM)")
                sleep_with_keepalive(dist, run_end)

            use_img_cycle = use_img
            direction = "💰SELL" if use_img_cycle or AD_TYPE == "sell" else "🛒BUY"
            log(f"── Cycle {cycle} [{direction}] | {remaining_min:.0f} min left | {_ts()} ──")

            order = CHANNEL_IDS.copy()
            random.shuffle(order)
            channels_posted = 0

            for cid in order:
                if time.time() >= run_end:
                    break
                if in_break(breaks, time.time())[0]:
                    break

                # Slowmode respect
                slow = slowmodes.get(cid, 0)
                if slow > 0 and cid in last_sent:
                    elapsed_s = time.time() - last_sent[cid]
                    need_wait = slow - elapsed_s + random.uniform(1, 3)
                    if need_wait > 0:
                        dbg(f"#{cid}: slowmode wait {need_wait:.1f}s")
                        sleep_chunked(need_wait, run_end)

                # Smart cooldown: don't stack on our own last message
                if am_i_last(cid, my_id):
                    stats[cid]["skipped"] += 1
                    total_skip += 1
                    log(f"   ⏭️ #{cid}: our ad still latest, waiting")
                    time.sleep(random.uniform(2, 6))
                    continue

                # Per-channel skip probability (humans don't hit every channel
                # on every single pass — 85-95% chance to post)
                if random.random() > random.uniform(0.85, 0.95):
                    stats[cid]["skipped"] += 1
                    total_skip += 1
                    log(f"   ↪️ #{cid}: skipped this pass (human-like)")
                    time.sleep(random.uniform(3, 8))
                    continue

                msg = random.choice(variations)
                ok, code, err = send_message(cid, msg, image_data if use_img_cycle else None)
                if ok:
                    total_sent += 1
                    stats[cid]["sent"] += 1
                    last_sent[cid] = time.time()
                    channels_posted += 1
                    snip = msg.replace("\n", " ")[:55]
                    log(f"   ✅ #{cid}: \"{snip}{'...' if len(snip)>=55 else ''}\" (total: {total_sent})")
                else:
                    total_err += 1
                    stats[cid]["errors"] += 1
                    log(f"   ❌ #{cid}: FAILED ({code}: {err})")
                    if code in (401, 403):
                        if not validate_token():
                            log("\n❌ CRITICAL: Token invalidated — likely banned. Stopping.")
                            _print_stats(start, total_sent, total_err, total_skip,
                                         total_distractions, stats)
                            return

                # Gap between channels: mostly 4-14s, occasionally longer "reading"
                if random.random() < 0.15:
                    time.sleep(random.uniform(20, 45))  # stopped to read
                else:
                    time.sleep(random.uniform(4, 14))

            if channels_posted == 0:
                log("   (no posts this cycle — all channels waiting / skipped)")

            if time.time() >= run_end:
                break
            wait_s = INTERVAL_MIN * 60 * random.uniform(0.8, 1.25)
            nt = datetime.fromtimestamp(time.time() + wait_s).strftime("%H:%M")
            log(f"   ⏳ Next ~{nt} (in {wait_s/60:.1f} min)\n")
            sleep_with_keepalive(wait_s, run_end)

    except KeyboardInterrupt:
        log("\n🛑 Stopped by user")
        _print_stats(start, total_sent, total_err, total_skip, total_distractions, stats)
        return

    log("\n🏁 Reached scheduled end time.")
    _print_stats(start, total_sent, total_err, total_skip, total_distractions, stats)


def _print_stats(start_ts, sent, err, skip, distractions, per_ch):
    elapsed = (time.time() - start_ts) / 60
    log("=" * 66)
    log("📊 FINAL STATS")
    log("=" * 66)
    log(f"Elapsed   : {elapsed:.1f} min ({elapsed/60:.2f}h)")
    log(f"Sent      : {sent} | Errors: {err} | Skipped (cooldown+random): {skip}")
    log(f"Distractions: {distractions} random pauses")
    if sent > 0 and elapsed > 0:
        log(f"Rate      : {sent/(elapsed/60):.1f} msg/hour")
    if err > 0 and sent + err > 0:
        log(f"Err rate  : {err/(sent+err)*100:.1f}%")
    log("Per channel:")
    for cid in CHANNEL_IDS:
        s = per_ch[cid]
        log(f"   {cid}: ✅{s['sent']}  ❌{s['errors']}  ⏭️{s['skipped']}")
    log("=" * 66)


if __name__ == "__main__":
    if _SELF_TEST:
        self_test()
    else:
        main()
