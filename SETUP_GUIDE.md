# 📨 Marketplace Discord Ad Sender — Complete Guide (v5.1)

Posts one ad at a time (SELL or BUY) to your Discord marketplace channels, running
on GitHub Actions cloud. The bot mimics a real person using Discord in Chrome:
real browser TLS fingerprint, active WebSocket (account shows "online"), natural
typing, AFK breaks, "typo-fix" edits, occasional reactions, and smart cooldown
(only reposts when someone else has posted after you). Messages stack naturally
— nothing is auto-deleted.

---

## ⚠️ Risk / honest talk first

This is a **self-bot**: it uses a normal user token to automate actions. That
violates Discord ToS. Your alt **can** be banned. v5.1 is built to be hard to
detect, but nothing is 100% ban-proof.

Three ground rules:
1. **Never use your main account.** Only an alt you're okay losing.
2. **Age the alt** before running (see §3). Brand-new alts + ads = instant shadowban.
3. **Verify the first message manually** every run — open Discord and actually
   look at the channel. If you can't see your ad, anti-spam deleted it. Stop.

If you follow these rules, run reasonable 6h sessions, and use a residential
proxy, the risk is low. People run similar bots on Blade Ball / Pet Sim / Roblox
trading servers daily.

---

## 🛡️ How it hides (the anti-detection stack, explained)

Every layer matters. If you skip the proxy or run on a 0-day alt, the other
layers can't save you.

### Layer 1 — TLS/HTTP2 fingerprint (curl_cffi)
Python's default `requests` library has a unique TLS "handshake signature"
(cipher order, extensions, GREASE, HTTP2 frame settings) that Discord and
Cloudflare immediately see as "script, not browser". We use `curl_cffi` set to
`impersonate="chrome"`, which copies a real Chrome browser's exact TLS/JA3/HTTP2
fingerprint. This alone fixes ~70% of why v4.2 got flagged.

### Layer 2 — WebSocket gateway
When you use Discord normally, your browser keeps a WebSocket open to
`wss://gateway.discord.gg` that shows you online, sends your typing events in
real time, receives messages, etc. v4.2 only made REST calls with no gateway —
that's like being invisible in the sidebar while somehow sending messages. v5.1
opens a real gateway, IDENTIFYs with the same properties as Chrome web, sends
heartbeats, sets your custom status through the gateway, and stays online.

### Layer 3 — Browser warmup (cookies + fingerprint)
Before sending anything authenticated:
1. Visits `https://discord.com/` (picks up Cloudflare cookies `__dcfduid`/`__sdcfduid`/`_cfuvid`)
2. Visits `https://discord.com/app` (SPA bootstrap, more cookies, scrapes the real build number + UA)
3. Calls `GET /api/v9/experiments` (gets `x-fingerprint` header)
4. Sends one `POST /api/v9/science` telemetry ping (what real clients do)
5. Sets `locale` cookie
6. Only then attaches the Authorization token.

This is exactly the sequence your browser does when you open Discord.

### Layer 4 — Browser-complete headers
Every request sends:
- `User-Agent`: real Chrome UA (from the curl_cffi impersonation)
- `X-Super-Properties`: base64 JSON with os/browser/build/locale (required header)
- `X-Fingerprint`: session fingerprint from /experiments
- `X-Discord-Locale`, `X-Discord-Timezone`: match your configured locale
- `X-Debug-Options: bugReporterEnabled` (Chrome web client always sends this)
- `Origin: https://discord.com`, `Referer: <per-channel URL>` (not a static `/@me`)
- `Sec-Ch-Ua`, `Sec-Ch-Ua-Mobile`, `Sec-Ch-Ua-Platform`, `Sec-Fetch-*`: added by curl_cffi
- `X-Discord-Idempotency-Key: <uuid>` on every POST (prevents duplicate-send issues)
- `nonce`: Discord snowflake on every message (optimistic send dedup)
- `allowed_mentions`: only parses users/roles — never @everyone/@here
- Cookies: forwarded from the warmup (CF cookies + locale)

### Layer 5 — Behavioral humanization
- 8-20s initial boot delay, then 40-90s "reading chat" before first post
- Each channel is browsed to with 3-9s "gaze time" + ACKs (read receipts)
- Pre-typing pause 1.8-4.5s, then typing indicator for a message-length-scaled
  duration (1.3-9s, average ~3-6s, like actually typing)
- Posts at random intervals averaging INTERVAL_MIN, ±25% jitter, plus random
  distraction pauses (10% of cycles, 1-5 min, like checking DMs)
- 2-4 AFK breaks per 6h run (10-30 min each, no posting, just heartbeats)
- Channel order is shuffled each cycle (not ID-order iteration)
- 85-95% post probability per channel (occasional "meh, later" skip)
- After posting: 8-22s pause, 40% chance to "glance at" another random channel
- 10%/cycle chance to react 🔥/💯/👀/etc. to someone else's message (looks engaged)
- ~12% chance of a "typo edit" 5-22s after posting (add/remove period, fix case on
  DM/LF/BB, add an extra emoji) with a new typing indicator
- Messages have 100+ variations: emoji prefixes/suffixes, extra phrases ("still
  going", "online rn", "prices firm", "fast replies"), rare typos (u→you,
  pls→please, etc.), occasional all-lowercase, no-emoji "plain" versions.

### Layer 6 — Image anti-fingerprint
When an image is attached:
- EXIF/XMP metadata is stripped (re-encoded via Pillow)
- Filename is randomized (e.g. `pic_48291.png`, not `ad.png` every time)
- JPEG quality is randomized 90-96 each post
- ±1 pixel RGB jitter on ~30 random pixels (visual difference is invisible, but
  the file hash changes every single time — defeats hash-blacklist spam filters)
- First WARMUP_POSTS (default 3) are text-only, to let the session age in before
  sending attachments (attachments from new accounts are heavily scrutinized)

### Layer 7 — Smart repost logic
- After every post we remember the message ID
- Before posting, we fetch the last 5 messages (cache-busted with `?_=timestamp`)
- If our last message is still the latest → skip (don't spam)
- If someone else posted after us → repost
- If our previous message ID is NOT in the last 5 messages → an anti-spam bot
  likely deleted it (we log a ⚠️ warning and force-repost once; if it keeps
  happening, the channel/server is actively deleting our messages → stop)
- Safety-net force-repost if > 2.5× INTERVAL_MIN has passed (catches edge cases)
- Per-channel slowmode is respected with 2-5s extra buffer

### Layer 8 — Error handling
- Exponential backoff on network errors
- Proper 429 rate-limit handling (respects `retry_after`, global cooldowns,
  capped at 600s per sleep so logs keep flowing)
- 401/403 triggers token re-validation; if the token is actually invalid, exits
  with code 2 (workflow `fail-fast: true` cancels all future chunks)
- 404 marks a channel dead (deleted/inaccessible)
- 5xx retries with backoff
- 3-strike per-channel error backoff

### Layer 9 — IP awareness
- Logs outbound IP + ISP/org on startup (via ipify + ipinfo)
- If running from a known datacenter (Azure/AWS/GCP/OVH/Hetzner/Oracle/etc.)
  without a proxy, prints a big warning (Wick/Carl shadow-delete datacenter IPs)

---

## 1. Getting your alt's token

1. Open Chrome (regular window, not Incognito — tokens last longer there).
2. Log into the alt at `discord.com/login`. Complete any new-location/email
   verification prompts. If you get "Essaie de te reconnecter" (reconnect
   prompt), that's just a new-IP check — sign back in.
3. Press **F12** to open DevTools → **Network** tab.
4. Send any message (e.g. "test") in any channel.
5. Click the `messages` network request that appears → **Headers** → scroll to
   **Request Headers** → copy the value of `authorization` (a long ~70-char
   string starting with letters/numbers, sometimes with dots).

**Token lifetime:** stays valid for days-to-weeks unless you click "Log Out",
change password, get banned, or Discord forces a password reset. Closing the
browser does NOT invalidate it.

🔒 Paste this only into the GitHub secret `USER_TOKEN`. Never commit it.

---

## 2. Getting channel IDs

1. Discord → User Settings (gear) → **Advanced** → toggle **Developer Mode** ON.
2. Right-click each target channel → **Copy Channel ID**.
3. Combine with commas, e.g. `1541658382015135817,1103759996468080752`.
4. Put this in the `CHANNEL_IDS` secret (or use the per-run `channel_1`/`channel_2`
   inputs to override for a single run).

---

## 3. Aging the alt (DO THIS BEFORE RUNNING)

This is the single most important thing people skip and then wonder why messages
get deleted.

Minimum:
- ✅ Account age **3+ days** (7+ is much better)
- ✅ Email verified (you already did this if you logged in)
- ✅ Set a custom avatar + username (not default pfp)
- ✅ Set a short bio ("trader" or whatever, doesn't matter)

Strongly recommended:
- 📱 Phone verify the alt (massive trust boost; cheap prepaid SIMs work)
- 🔒 Enable 2FA (raises account trust score)
- 💬 Spend 10-15 min manually chatting in the target servers BEFORE the bot run
  (say hi in general, react to a few messages, scroll channels — this generates
  normal account activity and telemetry)
- 🤝 Join the servers naturally, don't join 10 servers in 1 minute (join velocity
  is a raid signal)
- 👥 Add 1-2 friends (your main, a friend) — accounts with 0 friends look like throwaways

Running a brand-new, email-only, default-avatar alt straight from a cloud IP
sending image ads = almost guaranteed shadow-delete by Wick/Beemo/Double Counter.

---

## 4. Uploading your ad image (optional)

If you want images on SELL / simple-BUY ads:
1. Drop an image file at the repo root (default name `ad.png`).
2. Or set the `IMAGE_FILENAME` secret to whatever filename you used.
3. Format: PNG / JPG / WEBP / GIF. Under 8MB.
4. At runtime the bot will: strip EXIF, randomize filename, slightly re-compress,
   and jitter a few pixels → each upload has a unique file hash.

You can also select `attach_image: no` in the Run Workflow dropdown for text-only
runs (recommended for first test).

---

## 5. GitHub repository setup

1. Create a new **private** GitHub repo.
2. Upload these files to the repo root:
   - `send_ads.py`
   - `.github/workflows/send_ads.yml`
   - (optional) your ad image (e.g. `ad.png`)
3. Go to repo → **Settings → Secrets and variables → Actions → New repository secret**.
4. Add the required secrets (see §6).
5. Go to the **Actions** tab → enable Actions if prompted.
6. Click **Marketplace Ad Sender** → **Run workflow** → fill in the dropdowns (§7).

---

## 6. Secrets reference

Set these at **Settings → Secrets and variables → Actions**.

### Required

| Secret       | Description                                                                 |
|--------------|-----------------------------------------------------------------------------|
| `USER_TOKEN` | Your alt's Discord token (§1)                                               |
| `CHANNEL_IDS`| Comma-separated channel IDs (§2). You can override per-run with channel_1/2 |

### Recommended

| Secret            | Default      | What it does                                                                 |
|-------------------|--------------|------------------------------------------------------------------------------|
| `HTTPS_PROXY`     | _(none)_     | Residential proxy URL `http://user:pass@host:port` (see §9)                   |
| `IMAGE_FILENAME`  | `ad.png`     | Image filename at repo root (only used if attach_image=yes)                  |
| `CUSTOM_STATUS_TEXT` | `Trading` | Custom status shown on your profile                                          |
| `STATUS_EMOJI`    | `💰`         | Emoji prefix for the custom status                                           |

### Anti-detection tuning (only change if you know what you're doing)

| Secret               | Default             | What it does                                                                 |
|----------------------|---------------------|------------------------------------------------------------------------------|
| `WARMUP_POSTS`       | `3`                 | Text-only posts before images are attached (raises session trust)            |
| `STRIP_EXIF`         | `1` (on)            | Strip EXIF/metadata from images                                              |
| `IMAGE_JITTER`       | `1` (on)            | Apply tiny ±1-pixel jitter to images so each upload hashes differently        |
| `RANDOM_REACT`       | `1` (on)            | Occasionally react 🔥/💯/👀 to other people's messages                       |
| `IDLE_REACT_CHANCE`  | `0.10`              | Per-cycle probability of a reaction (0-1)                                    |
| `TYPO_EDIT_CHANCE`   | `0.12`              | Probability a post gets a post-send typo-fix edit (0-1)                      |
| `ENABLE_GATEWAY`     | `1` (on)            | Open WebSocket gateway for "online" presence (turning this off is suspicious) |
| `SUPPRESS_EMBEDS`   | `0` (off)           | Sometimes set SUPPRESS_EMBEDS flag (prevents link previews)                   |
| `PROXY_CHECK`        | `1` (on)            | Log outbound IP on startup                                                   |
| `MIN_AFK_BREAKS`     | `2`                 | Minimum AFK breaks per 6h chunk                                              |
| `MAX_AFK_BREAKS`     | `4`                 | Maximum AFK breaks per 6h chunk                                              |
| `AFK_MIN_MIN`        | `10`                | Shortest AFK (minutes)                                                       |
| `AFK_MAX_MIN`        | `30`                | Longest AFK (minutes)                                                        |
| `DISCORD_LOCALE`     | `en-US`             | Locale header (`fr-FR`, `es-ES`, etc.)                                       |
| `DISCORD_TIMEZONE`   | `America/New_York`  | Timezone header (e.g. `Europe/Paris`, `Africa/Casablanca`)                    |
| `DEBUG`              | `0` (off)           | `1` = verbose debug logs (useful if something breaks)                        |

**Tip:** set `DISCORD_LOCALE=fr-FR` and `DISCORD_TIMEZONE=Europe/Paris` if your alt
"lives" in France/Granada (matches where your IP is → less fingerprint mismatch).

---

## 7. Run Workflow inputs (the dropdown when you click "Run workflow")

| Input          | Options         | Meaning                                                                        |
|----------------|-----------------|--------------------------------------------------------------------------------|
| `ad_type`      | `sell` / `buy`  | SELL BB or BUY BB                                                              |
| `sell_rate`    | _(text)_        | [SELL] Rate like `2.5$` → "SELLING BB LF 2.5$/1K ..."                          |
| `sell_extra`   | _(text)_        | [SELL] Extra text after rate, e.g. `DM ME QUICK CAN DO BIG AMOUNTS`            |
| `buy_style`    | `detailed` / `simple` | [BUY] Multi-line bullets (text-only) or single line (image OK)            |
| `buy_rate`     | _(text)_        | [BUY detailed] Tokens rate (e.g. `2.2`)                                        |
| `buy_rate_rap` | _(text)_        | [BUY detailed] RAP rate (e.g. `1.8`)                                           |
| `buy_simple_text` | _(text)_     | [BUY simple] Full message, e.g. `BUYING ALL BLADE BALL DM ME QUICK`            |
| `channel_1`    | _(text)_        | Optional: post only to this channel (overrides CHANNEL_IDS for this run)       |
| `channel_2`    | _(text)_        | Optional: post to channel_1 + channel_2 only                                  |
| `interval_min` | `3` / `5`       | Minutes between posts per channel (5 = safer, 3 = slightly more aggressive)    |
| `total_hours`  | `6/12/18/24/48` | Runtime. 6h = safest first run. 48h = chained 8 × 6h chunks.                 |
| `attach_image` | `yes` / `no`    | Attach image? (Still subject to WARMUP_POSTS text-only warmup phase)           |

**First run recommendation:**
- `ad_type`: your choice
- `interval_min`: **5**
- `total_hours`: **6**
- `attach_image`: **no** (text-only test first!)
- leave channel_1/channel_2 empty (uses CHANNEL_IDS secret)

---

## 8. Understanding the logs

| Log line | Meaning |
|---|---|
| `🔑 Warming up browser session` | Visiting discord.com, collecting cookies |
| `UA: Chrome NNN / Build: NNNNNN / Fingerprint: OK` | Browser fingerprint built |
| `🌐 Outbound IP: x.x.x.x (ASN/Org)` | What the internet sees as your IP; check it's not Microsoft/Azure if you're proxying |
| `⏳ Boot delay Ns...` | Initial random wait before login |
| `✅ Logged in : user (id=NNN)` | Token works |
| `🟢 Gateway online as user (session abcdef12)` | WebSocket connected (account appears online) |
| `🟢 Custom status: '💰 Trading'` | Status set |
| `📡 Browsing channels (warmup reads)...` | Pre-post channel browsing |
| `👀 Reading chat for Ns before first post...` | Gaze time |
| `☕ AFK breaks: N scheduled` | Break times planned |
| `── Cycle N [...] ──` | Start of a posting cycle |
| `✅ #channel: 💬 "message..." (total: N, id=...)` | Message sent successfully (💬=text, 📷=image) |
| `✅ #channel: 📷 "message..."` | Image message sent |
| `⏭️ #channel: our ad still latest (by user), waiting` | Our ad is still newest; skipped (normal) |
| `↪️ #channel: skipped this pass (human-like)` | Random skip (normal) |
| `⚠️ #channel: previous ad appears DELETED` | Anti-spam deleted our last message — reposting |
| `🔰 warmup post (1/3) -- text-only` | In text-only warmup phase |
| `👌 reacted 🔥 to "..."` | Posted a reaction to someone else |
| `✏️ edited msg to "..."` | Post-send typo-fix edit |
| `☕ AFK break -- N min left` | In an AFK break |
| `💭 Distraction -- Ns` | Short random pause |
| `⏳ Next ~HH:MM (in N min)` | Waiting between cycles |
| `❌ CRITICAL: Token invalidated -- likely banned. Stopping.` | Token died (ban/logout). Run stopped. |
| `🌐 ⚠️ Datacenter IP detected!` | You're on Azure/AWS/etc without a proxy — expect deletions |

**What "my ad was deleted" looks like:**
- First post shows `✅` with a message ID
- Next cycle you see `⚠️ previous ad appears DELETED (anti-spam) -- reposting`
- The message never shows up in Discord when you check manually
- **Fix:** cancel run, check proxy is working, age the alt longer, try text-only first

---

## 9. Proxies — don't get your home IP flagged

### 9.0 Does my home IP ever touch Discord?

**Short answer: only if you run WITHOUT a proxy.**

- **GitHub Actions cloud (default, `runs-on: ubuntu-latest`) WITHOUT `HTTPS_PROXY`:**
  Discord sees a **Microsoft Azure** datacenter IP. Your home IP is NOT exposed
  to Discord at all (the code runs on Microsoft's servers, not your computer).
  The problem: Azure IPs are heavily shadow-banned by Wick/Carl/Beemo — messages
  get silently deleted. That's what happened on your v4.2 run.
- **GitHub Actions cloud WITH `HTTPS_PROXY` set to a residential proxy:**
  Discord sees the **proxy IP** (a real home ISP). Your home IP is NOT exposed
  to Discord or Discord's anti-spam. The proxy provider can see your home IP
  when you connect to them, but that connection is over TLS (encrypted HTTPS
  CONNECT tunnel), and proxy providers don't share that with Discord.
- **Self-hosted runner on your home PC WITHOUT proxy:** Discord sees YOUR
  **real home IP** (Orange/Free/SFR/etc.). This works (residential IP = good
  for anti-detection), but your home IP is now associated with a self-bot alt.
  If Discord bans the alt, they *could* link the IP to other accounts. Safer
  to also set `HTTPS_PROXY` even on self-hosted.
- **Self-hosted runner on your home PC WITH proxy:** Discord sees proxy IP;
  your home IP stays hidden from Discord. Best of both worlds if you want to
  use your PC but keep home IP clean.
- **Phone (Termux) on 4G/5G WITHOUT proxy:** Discord sees your **mobile carrier
  IP** (Orange Mobile, Free Mobile, etc.). Mobile IPs are the MOST trusted
  type (harder to blacklist, millions of users per IP). Your home internet IP
  is NEVER involved — turn off WiFi and use mobile data only. This is the
  best free option for both anti-detection AND home IP safety.

### 9.1 Why a proxy is mandatory on cloud runners

GitHub Actions runs on **Microsoft Azure** datacenter IPs. Discord anti-spam
(Wick, Carl-bot, Beemo, Double Counter, and Discord's own systems) score
datacenter IPs as very low-trust. A brand-new alt posting ads from an Azure IP
gets **shadow-deleted** within milliseconds: the API returns 200 OK, logs say
"sent", but the message is invisible to everyone except sometimes you (client
caching). That's exactly what happened on your first v4.2 run — one post
"succeeded" then every cycle showed "still latest" because the server had
discarded it.

### 9.2 What kind of proxy / connection works?

| Type | Works? | Home IP safe? | Notes |
|------|--------|---------------|-------|
| **Phone on 4G/5G (Termux)** | ⭐ Best | ✅ Yes (not used) | Mobile carrier IPs most trusted; zero cost; zero signup. See §9.5 |
| **Residential proxy + GHA cloud** | ✅ Great | ✅ Yes (hidden) | Real home IPs; Discord sees normal ISP. Costs $1-4/GB |
| **Self-hosted runner + proxy** | ✅ Great | ✅ Yes (hidden) | PC runs at home but routes through proxy; residential exit |
| **Self-hosted runner, no proxy** | ✅ Works | ⚠️ Exposed to Discord | Free, residential, but your home IP is associated with the alt |
| **Sticky/ISP residential proxy** | ✅ Good | ✅ Yes | Same IP all session; good for long runs |
| **Rotating residential proxy** | ✅ Good | ✅ Yes | New IP per request/some interval; use sticky/long-rotation if possible |
| **Datacenter proxies** | ❌ No | — | Same problem as Azure (different flagged IPs) |
| **Free VPNs (ProtonVPN free, etc.)** | ❌ No | — | Exit IPs widely blacklisted |
| **Tor** | ❌ No | — | Exit nodes all flagged |
| **Cloudflare WARP** | ⚠️ Unreliable | — | Sometimes works, sometimes flagged. Don't rely on it |
| **Webshare free 10-pack** | ❌ Datacenter | — | Backup only; better than raw Azure but still likely deletions |

### 9.3 Bandwidth math (why even tiny free trials work)

The bot is extremely light on bandwidth. It sends small JSON messages, tiny
WebSocket frames, and one image (~1.6MB) every few minutes per channel:
- **Text-only run:** ~5-10 MB/hour (mostly WebSocket heartbeats + small JSON)
- **With images:** ~15-25 MB/hour (one image per channel per interval)
- **A 6h session:** ~50-150 MB total
- **A 500 MB free trial:** 25-100 hours of bot runtime (multiple sessions)
- **5 GB on DataImpulse:** 250-500+ hours (months of casual use)

So even small free tiers are enough for many sessions.

### 9.4 🆓 Free proxy services (no credit card)

These give you a residential proxy URL without paying. Data limits are small
but enough for several bot sessions (see math above).

| Provider | What you get | How |
|----------|-------------|-----|
| **Proxyma.io** ⭐ | **500 MB free, 30 days, no card** | Email signup → click "Get 500MB for free" → they send a code to a Telegram bot → join their Telegram channel → instant residential proxy. Best free proxy service right now. |
| **Decodo (Smartproxy)** | 3-day trial, 100 MB, no card | Instant signup, residential + mobile proxies. 100 MB ≈ 5-15h of bot. Search "Decodo free trial". |
| **IPRoyal** | $1 credit (≈140 MB) on signup | Email signup, $1 ≈ 140MB at their ~$7/GB residential rate. No card for the free credit. |
| **Dexodata** | Free trial, no card | Residential proxies, instant activation. Check dexodata.com. |
| **AstroProxy** | $3 credit via support chat | No card required. Open their live chat and ask about a test balance — they usually credit $3. That's 1-3 GB at their rates. |

⚠️ **Proxy URL format: use `http://` not `https://`!** Discord connections
themselves are TLS (wss:// and https://), but the proxy CONNECT tunnel is
established over plain HTTP to the proxy server. All providers give you URLs
starting with `http://user:pass@host:port` — don't change the scheme. If you
put `https://` the proxy connection will fail and you'll leak Azure.

Once you get a proxy URL it looks like:
```
http://customer-xxx:[email protected]:7777
```
or
```
http://user:[email protected]:823
```
Paste that as the `HTTPS_PROXY` secret. The bot automatically sets `HTTP_PROXY`
too and routes both HTTP and WebSocket traffic through it.

### 9.5 🆓 Best free option: phone on 4G/5G (Termux)

Android only (no iOS equivalent that works well). Your phone's mobile data IP
is a carrier-grade NAT address shared with thousands of real users — this is
the most trusted IP type you can get, better than most residential proxies.
Your home internet is never touched.

Setup (~5 min):
1. Install **Termux** from **F-Droid** (not the Play Store — that version is
   outdated and broken). Search "Termux F-Droid APK" on your phone browser.
2. Turn **OFF WiFi** — use mobile data only (4G/5G).
3. Open Termux and run:
   ```bash
   pkg update && pkg install -y python3 git libjpeg-turbo libpng rust
   pip install 'curl-cffi>=0.10' 'Pillow>=10.0' 'websocket-client>=1.6'
   git clone YOUR_REPO_URL
   cd your-repo
   ```
4. Copy your ad image into the repo root if you use one (you can push to GitHub
   and `git pull`, or `pkg install termux-tools` and use `termux-setup-storage`
   to access phone files).
5. Set env vars and run. The simplest way is to create a small `run.sh`:
   ```bash
   #!/data/data/com.termux/files/usr/bin/bash
   export USER_TOKEN="your_token_here"
   export CHANNEL_IDS="1541658382015135817,1103759996468080752"
   export AD_TYPE="sell"            # or "buy"
   export MESSAGE="SELLING BB LF 2.5\$/1K DM ME QUICK CAN DO BIG AMOUNTS"
   # BUY simple:   export MESSAGE="BUYING ALL BLADE BALL DM ME QUICK"
   # BUY detailed: export MESSAGE=$'BUYING BLADE BALL:\n\n-TOKENS 2.2/1K\n\n-RAP 1.8\$/1K (nlf boosted)\n\nDM me quick'
   export ATTACH_IMAGE="no"         # first run: no; then "yes" with ad.png in repo
   export IMAGE_PATH="ad.png"       # only used if ATTACH_IMAGE=yes
   export INTERVAL_MIN=5
   export TOTAL_RUN_MIN=360
   export CUSTOM_STATUS_TEXT="Trading"
   export STATUS_EMOJI="💰"
   export DISCORD_LOCALE=fr-FR
   export DISCORD_TIMEZONE=Europe/Paris
   export WARMUP_POSTS=3
   # No HTTPS_PROXY needed — phone 4G is already the best "proxy"
   python -u send_ads.py
   ```
6. `chmod +x run.sh && ./run.sh`
7. Keep phone plugged in, Termux open (don't swipe it away). Disable battery
   optimization for Termux in Android settings so it doesn't get killed.

**Pros:** 100% free, best possible IP reputation, no signup for any proxy
service, home IP completely out of the picture.
**Cons:** Android only; phone must stay on and plugged in; if you get a call
and data drops briefly the bot will reconnect.

### 9.6 🆓 Self-hosted GitHub Actions runner (PC at home)

Runs the workflow on your own computer (real residential ISP like Orange/Free/
SFR/Bouygues) instead of Azure.

Setup (~5 min):
1. Your PC must stay on, awake, and connected to the internet while it runs.
2. GitHub repo → **Settings → Actions → Runners → New self-hosted runner**.
3. Follow the OS-specific copy-paste commands (Windows/macOS/Linux).
4. Edit `.github/workflows/send_ads.yml` and change:
   ```yaml
   runs-on: ubuntu-latest
   ```
   to:
   ```yaml
   runs-on: self-hosted
   ```
5. Install Python 3.10+ on your PC. The runner auto-pip-installs dependencies.
6. (Recommended) Still set `HTTPS_PROXY` to a residential proxy so your home
   IP isn't exposed to Discord as a self-bot IP. Without proxy it works for
   anti-detection but ties your home IP to the alt.

**Pros:** Free, fast, no bandwidth limits. With proxy = home IP safe too.
**Cons:** PC must stay on; without proxy = home IP associated with the alt.

### 9.7 💸 Cheapest paid option: DataImpulse

If the free trials run out and you don't want to run on phone/PC:
- **DataImpulse:** $5 for 5 GB, no KYC, no subscription, credit never expires.
  That's ~$1/GB — the cheapest real residential proxy I've found. 5 GB =
  250-500+ bot hours. Search "DataImpulse residential proxy".
- **IPRoyal:** ~$7/GB pay-as-you-go, no minimum, easy dashboard.
- **Decodo (Smartproxy):** ~$4/GB after the free trial; good quality.

Format is always: `http://user:[email protected]:port`

### 9.8 Verifying the proxy works

Every run logs on line ~3:
```
🌐 Outbound IP: 1.2.3.4 (Orange S.A. / French ISP)
```
✅ Good: shows your country + a normal consumer ISP (Orange, Free, SFR,
Bouygues, Vodafone, Movistar, etc.) or a mobile carrier.
❌ Bad: says "Microsoft Corporation/Azure", "Google Cloud", "Amazon AWS",
"OVH", "Hetzner", "DigitalOcean", or any hosting/datacenter name — your proxy
is NOT working (or you forgot to set `HTTPS_PROXY`, or used `https://`
instead of `http://` in the URL).

If you see the datacenter warning:
```
🌐 ⚠️  Datacenter IP detected! (AS12345 — Microsoft Azure)
```
…cancel the run immediately and fix the proxy before you get the alt flagged.

---

## 10. Monitoring DMs while the bot runs

- **Don't log into the alt from another device while the bot is running** — wait,
  actually you can! Use Discord's account switcher (your profile pic top-left
  → "Switch accounts" on mobile, or the account switcher on desktop).
- If you get an "Essaie de te reconnecter" / "new location" prompt in French:
  that's normal when your IP changes (proxy vs home vs phone). Just re-verify
  via email/phone.
- **The bot doesn't reply to DMs.** It only posts ads. You reply manually via
  the account switcher on your phone/main PC.
- If the bot token gets invalidated (401), the run stops with code 2 — you'll
  need to recopy the token.

---

## 11. Best practices / strategy for staying unbanned

1. **First run:** 6h, text-only (`attach_image: no`), interval 5min, with proxy
   or self-hosted runner. Verify you can SEE your ads in the channel. If yes →
   next run you can turn on image.
2. **Run 6h sessions**, not 48h straight. Two 6h runs/day is safer than one 48h run.
3. **Don't run every single day** — rotate between running 1-2 days, skipping
   a day. Real traders aren't spamming 24/7.
4. **Mix it up:** manually chat in the servers sometimes on the alt, post the
   ad manually once or twice per session (the bot isn't the only thing posting).
5. **Phone-verify the alt** if you can — $1-2 prepaid SIM is worth it for the
   trust boost.
6. **If you see multiple "deleted" warnings in a row** — CANCEL. The server's
   anti-spam is actively onto you. Wait 24h, age the alt more, or change proxy.
7. **Don't spam the same exact text.** The bot already makes 100+ variations;
   if you change the MESSAGE input every few days that's even better.
8. **If one channel deletes your messages but the other doesn't** — that
   channel has stricter anti-spam. Focus on the one that lets you post.
9. **Discord bans are permanent and silent** (no email, no warning). If the
   bot stops with 401 and you can't log into the alt anymore, it's banned.
   That's why we use alts.

---

## 12. Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| Log says "sent" but I can't see the message in Discord | Shadow-deleted by anti-spam (Wick/Carl) | Use residential proxy; age the alt; try text-only first |
| `⚠️ Datacenter IP detected!` warning | You're on Azure without a proxy | Set HTTPS_PROXY secret or use self-hosted runner |
| `❌ CRITICAL: Token invalidated` | Token died (ban, logout, password change) | Re-copy token; if alt can't log in, alt is banned → use new alt |
| `❌ 403 ...` on a channel | Channel inaccessible / kicked / no perms | Check the alt is still in the server and can post there |
| `❌ 404 ...` on a channel | Channel deleted or ID wrong | Verify channel IDs in Developer Mode |
| `⚠️ Rate limit ... GLOBAL` | Hit global rate limit | Bot auto-waits; usually resolves in 30-120s |
| Stuck on "our ad still latest" for many cycles even though chat is busy | Message was shadow-deleted; fetch cache says you're last | Cancel and check proxy + account age |
| WebSocket "Gateway connecting in background" never connects | WebSocket blocked or proxy issue | If you don't see "🟢 Gateway online" after 20s, check HTTPS_PROXY format |
| IP check says Microsoft/Azure but I set HTTPS_PROXY | Wrong proxy format | Make sure it starts with `http://` not `https://`; verify user:pass |
| `pip install` fails on self-hosted runner | Missing Python dev libraries | On Ubuntu: `apt install python3-dev libjpeg-dev zlib1g-dev` |
| Image messages always get deleted, text works | Anti-spam flags images from new/suspicious accounts | Age the alt more; go more WARMUP_POSTS (e.g. 8-10); use proxy |

---

## 13. If you want to push it further (not included but possible)

- **Voice channel AFK:** join a random voice channel silently during AFK breaks
- **Friend requests:** auto-accept friend requests so people can add you
- **DM auto-reply:** automatic "hey, what's your offer?" response to DMs (risky!)
- **Rotate proxies:** use a different IP per cycle (needs rotating proxy endpoint)
- **Discord nitro/activities:** set a listening-to-spotify activity for extra realism
- **Multiple alt rotation:** switch alt accounts between runs

These aren't included because they add complexity without huge benefit. The
current setup is already well beyond what most trading bots use.

---

## Changelog

**v5.1:** WebSocket gateway (online presence), snowflake nonce, post-send typo
edits, allowed_mentions blocks @everyone, WebSocket proxy support, image pixel
jitter, extended bootup/warmup, browser capabilities bitmask, science
telemetry ping, cookie forwarding on WS, multipart close-after-send bugfix.

**v5.0:** curl_cffi (TLS/HTTP2 fingerprint), cookie + x-fingerprint warmup,
channel ACKs, image EXIF strip + hash randomization, text-only warmup, IP check,
idempotency keys, per-channel Referer, random reactions.

**v4.x:** requests-based baseline with typing, AFK breaks, cooldown logic, rate
limit handling (used in your first run; got detected due to lack of TLS
fingerprint and proxy).
