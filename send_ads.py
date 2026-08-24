"""
Discord Marketplace Ad Sender  v4.2  (self-bot / alt account)
==============================================================
Sends ONE ad (SELL or BUY, chosen at workflow start) to marketplace channels
with human-like timing and behavior. Messages stack naturally (no auto-delete),
and smart cooldown only reposts when other people have posted after you.
"""

import os
import sys
import time
import json
import random
import mimetypes
import base64
import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import requests

_SELF_TEST = "--self-test" in sys.argv
if _SELF_TEST:
    os.environ.setdefault("USER_TOKEN", "FAKE_TOKEN_FOR_SELF_TEST")
    os.environ.setdefault("CHANNEL_IDS", "000000000000000000,111111111111111111")
    os.environ.setdefault("AD_TYPE", "sell")
    os.environ.setdefault("MESSAGE", "SELLING BB LF 2.5$/1K DM ME QUICK")
    os.environ.setdefault("ATTACH_IMAGE", "no")

def _ts():
    return datetime.now().strftime("%H:%M:%S")

def log(m):
    print(f"[{_ts()}] {m}", flush=True)

def dbg(m):
    pass

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
DISCORD_LOCALE    = _env("DISCORD_LOCALE", "en-US")
DISCORD_TIMEZONE  = _env("DISCORD_TIMEZONE", "America/New_York")
DEBUG         = _env("DEBUG", "0") in ("1", "true", "yes", "on")

if MIN_AFK_BREAKS < 0: MIN_AFK_BREAKS = 0
if MAX_AFK_BREAKS < MIN_AFK_BREAKS: MAX_AFK_BREAKS = MIN_AFK_BREAKS
if AFK_MIN_MIN < 1: AFK_MIN_MIN = 1
if AFK_MAX_MIN < AFK_MIN_MIN: AFK_MAX_MIN = AFK_MIN_MIN

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

if not CHANNEL_IDS:
    log("❌ No valid CHANNEL_IDS (empty list after parsing)")
    sys.exit(1)

if INTERVAL_MIN < 1:
    log(f"⚠️ INTERVAL_MIN={INTERVAL_MIN} too small, clamping to 1")
    INTERVAL_MIN = 1

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

_DEFAULT_BUILD = 362220
def _fetch_build_number():
    """Attempt to scrape the current stable client_build_number from
    discord.com so X-Super-Properties isn't obviously stale. Done with
    a fresh unauthenticated request (real browsers fetch the app shell
    before auth). Falls back to _DEFAULT_BUILD on any failure."""
    if _SELF_TEST:
        return _DEFAULT_BUILD
    try:
        r = requests.get("https://discord.com/app", timeout=8,
                         headers={"User-Agent": _UA})
        m = re.search(r'"buildNumber"\s*:\s*(\d{5,})', r.text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return _DEFAULT_BUILD

CLIENT_BUILD = _fetch_build_number()
_SUPER_PROPERTIES = base64.b64encode(json.dumps({
    "os": "Windows", "browser": "Chrome", "device": "",
    "system_locale": DISCORD_LOCALE, "browser_user_agent": _UA,
    "browser_version": "128.0.0.0", "os_version": "10",
    "referrer": "", "referring_domain": "",
    "referrer_current": "", "referring_domain_current": "",
    "release_channel": "stable", "client_build_number": CLIENT_BUILD,
    "client_event_source": None, "design_id": 0,
}, separators=(",", ":")).encode()).decode()

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": USER_TOKEN,
    "User-Agent": _UA,
    "Accept": "*/*",
    "Accept-Language": f"{DISCORD_LOCALE},en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Origin": "https://discord.com",
    "Referer": "https://discord.com/channels/@me",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Super-Properties": _SUPER_PROPERTIES,
    "X-Debug-Options": "bugReporterEnabled",
    "X-Discord-Locale": DISCORD_LOCALE,
    "X-Discord-Timezone": DISCORD_TIMEZONE,
})
JSON_HEADERS = {"Content-Type": "application/json"}

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
        dbg(f"   ⏳ Global rate-limit cooldown {wait:.1f}s")
        time.sleep(wait)

def api(method, url, retries=3, **kw):
    """Unified API caller with:
       - network-error exponential backoff (up to `retries` attempts)
       - 429 rate-limit handling: bounded per-call wait, capped at 10 min
         per sleep (very long bans are chunked so logs/keepalive still
         flow), global cooldown tracked across calls
       - 5xx retry with backoff
       - global cooldown respect
    """
    global _global_cooldown_until
    _429_streak = 0
    for attempt in range(1, retries + 1):
        _apply_global_cooldown()
        try:
            r = SESSION.request(method, url, timeout=30, **kw)
        except requests.exceptions.RequestException as e:
            short = url.split("/api/v9/")[-1] if "/api/v9/" in url else url[-40:]
            log(f"   ⚠️ Net error ({method} {short}): {type(e).__name__} "
                f"(attempt {attempt}/{retries})")
            if attempt < retries:
                time.sleep(3 * attempt + random.uniform(0, 1))
                continue
            fr = requests.Response()
            fr.status_code = 0
            fr._content = str(e).encode()
            return fr

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
            # Cap each individual sleep so very long bans (hours) don't
            # freeze the loop — _apply_global_cooldown continues the wait
            # on the next call.
            this_wait = min(raw_wait, 600) + random.uniform(1, 3)
            scope = "GLOBAL" if is_global else f"bucket {d.get('bucket','?')}"
            log(f"   ⚠️ Rate limit ({scope}), waiting {this_wait:.1f}s"
                + (f" (server asked for {raw_wait:.0f}s, chunked)" if raw_wait > 600 else "")
                + f" [streak {_429_streak}]")
            if is_global:
                # Record the FULL server-asked wait (uncapped) so subsequent
                # calls also respect it; don't just record this chunk.
                _global_cooldown_until = max(_global_cooldown_until,
                                             time.time() + raw_wait)
            if _429_streak >= 6:
                log("   ⚠️ Too many consecutive 429s -- returning")
                return r
            time.sleep(this_wait)
            continue

        if 500 <= r.status_code < 600 and attempt < retries:
            log(f"   ⚠️ Server error {r.status_code} (attempt {attempt}/{retries}), retrying")
            time.sleep(3 * attempt + random.uniform(0, 2))
            continue

        return r
    fr = requests.Response()
    fr.status_code = 0
    fr._content = b"max retries exceeded"
    return fr

def typing_duration(text):
    """Humans type longer messages slower. Returns seconds to 'type'.
    Capped at 9s because Discord typing indicators naturally expire after
    ~10s anyway — anything longer looks like a stuck indicator."""
    lines = text.count("\n") + 1
    words = len(text.split())
    if lines > 3 or words > 20:
        d = random.uniform(4.0, 8.0)
    elif words > 10:
        d = random.uniform(2.8, 5.0)
    else:
        d = random.uniform(1.5, 3.2)
    return min(d, 9.0)

def validate_token():
    """Returns (user_dict, None) on success, (None, reason) on failure.
    reason is one of: 'invalid' (401/403 — account banned/logged out),
    'network' (DNS/timeout/connection), 'server' (5xx), 'unknown'."""
    r = api("GET", "https://discord.com/api/v9/users/@me", headers=JSON_HEADERS, retries=2)
    if r.status_code == 200:
        try:
            return r.json(), None
        except Exception:
            return None, "unknown"
    try:
        body = r.json()
        msg = body.get("message", r.text)[:200]
    except Exception:
        msg = (r.text or "")[:200]
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
                headers=JSON_HEADERS, data=json.dumps(payload))
        if r.status_code == 200:
            log(f"🟢 Status: '{STATUS_EMOJI} {CUSTOM_STATUS_TEXT}'")
            return True
        log(f"⚠️ Status set failed ({r.status_code}) -- continuing")
    except Exception as e:
        log(f"⚠️ Status error: {e}")
    return False

def keepalive():
    try:
        api("GET", "https://discord.com/api/v9/users/@me", headers=JSON_HEADERS, retries=1)
        dbg("   💓 keepalive")
    except Exception:
        pass

def get_channel_info(cid):
    r = api("GET", f"https://discord.com/api/v9/channels/{cid}", headers=JSON_HEADERS, retries=2)
    try:
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def get_last_messages(cid, limit=2):
    r = api("GET", f"https://discord.com/api/v9/channels/{cid}/messages?limit={limit}",
            headers=JSON_HEADERS, retries=2)
    try:
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
    except Exception:
        pass
    dbg(f"get_last_messages failed for {cid}: {r.status_code}")
    return None

def am_i_last(cid, my_id):
    """Return True if our message is currently the latest. Fail-safe:
    returns True (don't post) on fetch failure, to avoid spamming."""
    msgs = get_last_messages(cid, 2)
    if msgs is None or len(msgs) == 0:
        return True
    return msgs[0].get("author", {}).get("id") == my_id

def read_channel(cid):
    msgs = get_last_messages(cid, 10)
    if msgs is None:
        msgs = []
    dbg(f"   👁️ read #{cid} ({len(msgs)} msgs)")
    return msgs

def send_typing(cid, text):
    """Simulate thinking about the message, send the typing indicator, then
    sleep for the realistic typing duration.
    - Always sleep the typing duration even if the typing indicator fails
      (humans still type whether or not the network shows the bubble).
    - A short random "thinking" pause before the indicator fires looks more
      human than instant POST-after-read (real users re-read the channel
      briefly before typing)."""
    try:
        # Pre-thinking pause: glance at channel 0.8-2.2s before starting to type
        time.sleep(random.uniform(0.8, 2.2))
        api("POST", f"https://discord.com/api/v9/channels/{cid}/typing",
            headers=JSON_HEADERS, retries=1)
    except Exception:
        pass
    # Always sleep the typing duration regardless of indicator success
    time.sleep(typing_duration(text))

def send_message(cid, text, img=None):
    send_typing(cid, text)
    if img:
        fname, fbytes, fmime = img
        mp = {
            "payload_json": (None, json.dumps({"content": text, "tts": False}),
                             "application/json"),
            "files[0]": (fname, fbytes, fmime),
        }
        hdrs = {k: v for k, v in SESSION.headers.items() if k.lower() != "content-type"}
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
        err = (r.text or "")[:120]
    return False, r.status_code, err

_EMOJIS   = ["🔥", "💸", "⚡", "✅", "💰", "🤑", "📈", "💎"]
_SUFFIXES = ["", " ✅", " ⚡", " 🔥", " dm fast", " online now ✅", " quick reply ⚡"]
_PREFIXES = ["", "💸 ", "⚡ ", "🔥 ", "✅ ", "💰 "]

def build_variations(base):
    is_multiline = "\n" in base.strip()
    out = set()
    if is_multiline:
        lines = base.split("\n")
        header = lines[0]
        rest = "\n".join(lines[1:]) if len(lines) > 1 else ""
        for pre in ("", "⚡ ", "🔥 ", "💸 ", "✅ ", "💰 ", "🤑 ", "📈 "):
            new_header = f"{pre}{header}"
            candidate = new_header + ("\n" + rest if rest else "")
            if len(candidate) <= DISCORD_MSG_LIMIT:
                out.add(candidate)
        # Extra random emoji-prefix combinations (sample without replacement
        # so we don't regenerate the same "e1 header" the deterministic loop
        # above already added).
        random_emojis = random.sample(_EMOJIS, k=min(6, len(_EMOJIS)))
        for e1 in random_emojis:
            new_header = f"{e1} {header}"
            candidate = new_header + ("\n" + rest if rest else "")
            if len(candidate) <= DISCORD_MSG_LIMIT:
                out.add(candidate)
    else:
        for pre in _PREFIXES:
            for suf in _SUFFIXES:
                v = f"{pre}{base}{suf}"
                if len(v) <= DISCORD_MSG_LIMIT:
                    out.add(v)
        for _ in range(8):
            e1, e2 = random.sample(_EMOJIS, 2)
            v = f"{e1} {base} {e2}"
            if len(v) <= DISCORD_MSG_LIMIT:
                out.add(v)
    uniq = [v for v in out if len(v) <= DISCORD_MSG_LIMIT]
    if base in uniq:
        uniq.remove(base)
    uniq.insert(0, base)
    return uniq

def load_image():
    if not IMAGE_PATH or not ATTACH_IMAGE:
        return None
    p = Path(IMAGE_PATH).expanduser()
    if not p.exists():
        log(f"⚠️ Image not found at {IMAGE_PATH} -- text-only")
        return None
    try:
        data = p.read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        size_mb = len(data) / 1024 / 1024
        if size_mb > 8:
            log(f"⚠️ Image {size_mb:.1f}MB exceeds 8MB limit -- text-only")
            return None
        log(f"🖼️ Image cached: {p.name} ({size_mb:.2f}MB, {mime})")
        return (p.name, data, mime)
    except Exception as e:
        log(f"⚠️ Failed to read image: {e} -- text-only")
        return None

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
        placed = False
        for _attempt in range(100):
            bs = time.time() + random.uniform(min_start, max(min_start + 60, usable))
            bd = random.uniform(AFK_MIN_MIN, AFK_MAX_MIN) * 60
            be = bs + bd
            ok = True
            for es, ee in out:
                if not (be + gap < es or bs > ee + gap):
                    ok = False
                    break
            if ok:
                out.append((bs, be))
                placed = True
                break
        if not placed:
            break
    out.sort(key=lambda x: x[0])
    return out

def in_break(breaks, now):
    for s, e in breaks:
        if s <= now < e:
            return True, e - now
    return False, 0

class _KeepaliveSleep:
    """Persists last_ping across calls so AFK breaks (60s chunks) keepalive."""
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
            if time.time() - self.last_ping >= 290:
                keepalive()
                self.last_ping = time.time()

_ksleeper = None
def sleep_with_keepalive(seconds, end_time=None):
    global _ksleeper
    if _ksleeper is None:
        _ksleeper = _KeepaliveSleep()
    _ksleeper.sleep(seconds, end_time)

def self_test():
    print("=" * 60)
    print("🧪 Self-test (no network calls)")
    print("=" * 60)

    vs = build_variations("SELLING BB LF 2.5$/1K DM ME QUICK CAN DO SMALL AND BIG AMOUNTS")
    assert len(vs) >= 40, f"Expected >=40 sell variations, got {len(vs)}"
    assert len(set(vs)) == len(vs), "Duplicate variations"
    for v in vs:
        assert len(v) <= DISCORD_MSG_LIMIT
    assert vs[0] == "SELLING BB LF 2.5$/1K DM ME QUICK CAN DO SMALL AND BIG AMOUNTS"
    print(f"✅ Sell variations: {len(vs)} unique, original first")

    buy_base = "BUYING BLADE BALL:\n\n-TOKENS 2.2/1K\n\n-RAP 1.8$/1K (nlf boosted)\n\nDM me quick"
    vb = build_variations(buy_base)
    assert len(vb) >= 6, f"Expected >=6 buy variations, got {len(vb)}"
    for v in vb:
        assert len(v) <= DISCORD_MSG_LIMIT
        assert "\n" in v
        lines = v.split("\n")
        assert lines[-1].strip() in ("DM me quick", ""), \
            f"Suffix emoji leaked to last line: {lines[-1]!r}"
    assert vb[0] == buy_base
    print(f"✅ Buy variations: {len(vb)} unique (header-only emoji, original first)")

    short_t = typing_duration("hi")
    long_t = typing_duration(buy_base)
    assert long_t > short_t
    print(f"✅ Typing duration scales: short={short_t:.1f}s, long={long_t:.1f}s")

    fake_img = ("ad.png", b"PNGDATA" * 10, "image/png")
    mp = {
        "payload_json": (None, json.dumps({"content": "test", "tts": False}), "application/json"),
        "files[0]": fake_img,
    }
    hdrs = {k: v for k, v in SESSION.headers.items() if k.lower() != "content-type"}
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

    import unittest.mock as mock
    overlaps = 0
    for seed in range(1000):
        random.seed(seed)
        with mock.patch("time.time", return_value=1_000_000):
            br = plan_breaks(6 * 3600)
        for i in range(len(br) - 1):
            if br[i][1] + 15 * 60 > br[i + 1][0]:
                overlaps += 1
    assert overlaps == 0
    print("✅ AFK planner: zero overlaps across 1000 seeds")

    with mock.patch("time.time", return_value=1_000_000):
        br_short = plan_breaks(15 * 60)
    assert br_short == []
    print("✅ AFK planner: short runs return [] (no crash)")

    assert _int("NONEXISTENT", 7) == 7
    assert _float("NONEXISTENT", 1.5) == 1.5
    print("✅ Config parsers")

    assert isinstance(SESSION, requests.Session)
    assert "Authorization" in SESSION.headers
    assert "X-Super-Properties" in SESSION.headers
    assert "Sec-Ch-Ua" in SESSION.headers
    assert "Sec-Fetch-Site" in SESSION.headers
    print("✅ Session: cookie jar, keep-alive, full browser header set")

    with mock.patch.object(sys.modules[__name__], "get_last_messages", return_value=None):
        assert am_i_last("1", "123") == True
    with mock.patch.object(sys.modules[__name__], "get_last_messages", return_value=[]):
        assert am_i_last("1", "123") == True
    print("✅ am_i_last: fails safe (True) on fetch error/empty")

    print()
    print("=" * 60)
    print("🎉 ALL SELF-TESTS PASSED")
    print("=" * 60)

def main():
    global _ksleeper
    _ksleeper = _KeepaliveSleep()

    start = time.time()
    run_end = start + TOTAL_RUN_MIN * 60
    variations = build_variations(MESSAGE)
    image_data = load_image()
    use_img = bool(image_data) and ATTACH_IMAGE

    last_sent = {}
    slowmodes = {}
    channel_errors = defaultdict(int)
    dead_channels = set()
    stats = defaultdict(lambda: {"sent": 0, "errors": 0, "skipped": 0, "cooldown": 0})
    total_sent = total_err = total_skip = total_distractions = 0
    cycle = 0

    log("=" * 66)
    log(f"🎯 Marketplace Ad Sender  v4.2  ({AD_TYPE.upper()})")
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
    log(f"Distraction : ~10% chance per cycle (1-5 min)")
    log(f"Warmup read : YES (reads channels before first post)")
    log(f"AFK breaks  : {MIN_AFK_BREAKS}-{MAX_AFK_BREAKS} ({AFK_MIN_MIN:.0f}-{AFK_MAX_MIN:.0f} min)")
    log(f"Client build: {CLIENT_BUILD}")
    log(f"Debug       : {'ON' if DEBUG else 'OFF'}")
    log("=" * 66)

    startup = random.uniform(25, 70)
    log(f"⏳ Startup delay {startup:.0f}s...")
    sleep_chunked(startup, run_end)

    me, vreason = validate_token()
    if not me:
        log(f"❌ Could not authenticate on startup (reason: {vreason})")
        _print_stats(start, total_sent, total_err, total_skip,
                     total_distractions, stats)
        sys.exit(1)
    my_id = me.get("id")
    username = me.get("username", "???")
    if not my_id:
        log("❌ Could not read user id from /users/@me response")
        sys.exit(1)
    log(f"✅ Logged in  : {username}  (id={my_id})")
    log(f"   Email verified: {me.get('verified', False)} | 2FA: {me.get('mfa_enabled', False)}")
    if not me.get("verified"):
        log("   ⚠️ Email not verified -- higher flag risk")

    set_status()

    log("📡 Checking & reading channels (warmup)...")
    ok_count = 0
    for cid in CHANNEL_IDS:
        info = get_channel_info(cid)
        if not info:
            log(f"   ❌ {cid}: could not fetch info (will skip)")
            dead_channels.add(cid)
            sleep_chunked(random.uniform(1.5, 3.0))
            continue
        name = info.get("name", "?")
        slowmodes[cid] = info.get("rate_limit_per_user", 0)
        # Small human gap between "clicking" a channel (info fetch) and
        # scrolling it (message fetch) — real clients don't fire two GETs
        # back-to-back within the same millisecond.
        sleep_chunked(random.uniform(0.4, 1.0))
        read_channel(cid)
        sleep_chunked(random.uniform(0.8, 2.5))
        log(f"   ✅ {cid} → #{name}  slowmode={slowmodes[cid]}s")
        ok_count += 1

    active_channels = [c for c in CHANNEL_IDS if c not in dead_channels]
    if ok_count == 0:
        log("❌ No accessible channels -- check token and CHANNEL_IDS")
        sys.exit(1)
    if dead_channels:
        log(f"⚠️ {len(dead_channels)}/{len(CHANNEL_IDS)} channels inaccessible; will skip them")

    warmup_wait = random.uniform(15, 40)
    log(f"👀 Simulating reading/scroll for {warmup_wait:.0f}s before first post...")
    sleep_chunked(warmup_wait, run_end)

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
                log(f"☕ AFK break -- {afk_left/60:.1f} min left")
                sleep_with_keepalive(min(60, afk_left), run_end)
                continue

            if cycle > 1 and random.random() < 0.10:
                dist = random.uniform(60, 300)
                total_distractions += 1
                log(f"   💭 Distraction pause -- {dist:.0f}s (like checking a DM)")
                sleep_with_keepalive(dist, run_end)
                if time.time() >= run_end:
                    break

            direction = "💰SELL" if AD_TYPE == "sell" else "🛒BUY"
            log(f"── Cycle {cycle} [{direction}] | {remaining_min:.0f} min left | {_ts()} ──")

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
                    elapsed_s = time.time() - last_sent[cid]
                    need_wait = slow - elapsed_s + random.uniform(1, 3)
                    if need_wait > 0:
                        dbg(f"#{cid}: slowmode wait {need_wait:.1f}s")
                        sleep_chunked(need_wait, run_end)
                        if time.time() >= run_end:
                            break

                if am_i_last(cid, my_id):
                    stats[cid]["cooldown"] += 1
                    stats[cid]["skipped"] += 1
                    total_skip += 1
                    log(f"   ⏭️ #{cid}: our ad still latest, waiting")
                    sleep_chunked(random.uniform(2, 6), run_end)
                    continue

                if random.random() > post_threshold:
                    stats[cid]["skipped"] += 1
                    total_skip += 1
                    log(f"   ↪️ #{cid}: skipped this pass (human-like)")
                    sleep_chunked(random.uniform(3, 8), run_end)
                    continue

                available = [v for v in variations if v not in used_variations]
                if not available:
                    available = variations[:]
                msg = random.choice(available)

                ok, code, err = send_message(cid, msg, image_data if use_img else None)
                if ok:
                    # Only "consume" a variation if the post succeeded, so
                    # we don't run out of variations after errors.
                    used_variations.add(msg)
                    total_sent += 1
                    stats[cid]["sent"] += 1
                    last_sent[cid] = time.time()
                    channel_errors[cid] = 0
                    channels_posted += 1
                    snip = msg.replace("\n", " ⏎ ")[:55]
                    log(f"   ✅ #{cid}: \"{snip}{'...' if len(snip)>=55 else ''}\" (total: {total_sent})")
                else:
                    total_err += 1
                    stats[cid]["errors"] += 1
                    channel_errors[cid] += 1
                    log(f"   ❌ #{cid}: FAILED ({code}: {err})")
                    if code in (401, 403):
                        recheck, recheck_reason = validate_token()
                        if recheck is None and recheck_reason == "invalid":
                            log("\n❌ CRITICAL: Token invalidated -- likely banned. Stopping.")
                            _print_stats(start, total_sent, total_err, total_skip,
                                         total_distractions, stats)
                            sys.exit(2)
                        elif recheck is None:
                            # Couldn't re-validate due to network/server hiccup.
                            # Back off from this channel but don't abort run.
                            log(f"   ⚠️ #{cid}: got {code} but re-validation failed ({recheck_reason}); "
                                "treating as channel error, not ban.")
                            # will go through channel_errors backoff below
                        else:
                            log(f"   ⚠️ #{cid}: channel 403 but token valid; marking dead")
                            dead_channels.add(cid)
                            if cid in active_channels:
                                active_channels.remove(cid)
                    elif code == 404:
                        log(f"   ⚠️ #{cid}: 404 -- channel may be deleted; marking dead")
                        dead_channels.add(cid)
                        if cid in active_channels:
                            active_channels.remove(cid)

                if time.time() >= run_end:
                    break
                if random.random() < 0.15:
                    sleep_chunked(random.uniform(20, 45), run_end)
                else:
                    sleep_chunked(random.uniform(4, 14), run_end)

            if channels_posted == 0:
                log("   (no posts this cycle -- all channels waiting / skipped)")

            if time.time() >= run_end:
                break
            wait_s = INTERVAL_MIN * 60 * random.uniform(0.8, 1.25)
            nt = datetime.fromtimestamp(time.time() + wait_s).strftime("%H:%M")
            log(f"   ⏳ Next ~{nt} (in {wait_s/60:.1f} min)\n")
            sleep_with_keepalive(wait_s, run_end)

    except KeyboardInterrupt:
        log("\n🛑 Stopped by user")
        _print_stats(start, total_sent, total_err, total_skip, total_distractions, stats)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        log(f"\n💥 Unhandled error: {type(e).__name__}: {e}")
        _print_stats(start, total_sent, total_err, total_skip, total_distractions, stats)
        raise

    log("\n🏁 Reached scheduled end time.")
    _print_stats(start, total_sent, total_err, total_skip, total_distractions, stats)
    sys.exit(0)


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
        log(f"   {cid}: ✅{s['sent']}  ❌{s['errors']}  ⏭️{s['skipped']} (cooldown {s['cooldown']})")
    log("=" * 66)


if __name__ == "__main__":
    if _SELF_TEST:
        self_test()
    else:
        main()
