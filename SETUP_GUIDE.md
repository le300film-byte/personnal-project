# 📨 Marketplace Discord Ad Sender — Setup Guide (v5.2 final)

Posts one ad at a time (SELL or BUY) to your Discord marketplace channels,
running entirely on **GitHub Actions cloud** (no PC/phone required). The bot
mimics a real person using Discord in Chrome: real browser TLS fingerprint,
active WebSocket (account shows "online"), natural typing, AFK breaks,
"typo-fix" edits, occasional reactions, **📌 Cloudflare WARP IP masking built
in** (no proxy/credit card/signup needed), **📌 DM forwarding** to a private
webhook on your main account, **📌 auto-learn blacklist** for blocked message
variations, **📌 geo-country safety check**, and smart cooldown (only reposts
when someone else has posted after you). Messages stack naturally — nothing is
auto-deleted.

There are two files:

- `send_ads.py` — the Python self-bot (~2,270 lines).
- `.github/workflows/send_ads.yml` — the GitHub Actions workflow that installs
  dependencies, connects WARP, verifies the outbound IP is NOT Azure, then
  runs the bot. Runs are split into chained 6-hour chunks (max 48h).

---

## ⚠️ Risk / honest talk first

This is a **self-bot**: it uses a normal user token to automate actions. That
violates Discord's Terms of Service. Your alt **can** be banned. v5.2 is built
to be hard to detect, but nothing is 100% ban-proof.

Three non-negotiable rules:

1. **Never use your main account.** Only an alt you are okay losing.
2. **Age the alt 3+ days** before the first run (see §3). Brand-new alts + ads
   = instant shadowban.
3. **Verify the first message manually** every run — open Discord and actually
   look at the channel. If you can't see your own ad, anti-spam deleted it.
   Cancel the run immediately.

If you follow these rules, run reasonable 6h sessions, and let WARP do its job
(or use a residential proxy), the risk is low. People run similar bots on
Blade Ball / Roblox trading servers daily.

---

## 📌 1. Overview

### What it does
- Posts ONE ad type per run (SELL BB **or** BUY Blade Ball — no "both" mode).
- Targets 1 or more marketplace channels (`CHANNEL_IDS` secret or per-run
  `channel_1`/`channel_2` overrides).
- Builds 100+ message variations (emoji prefixes/suffixes, extra phrases,
  typos, casing) so every post looks different.
- Simulates human behavior throughout: browser warmup, channel reading,
  typing indicator, post length proportional typing speed, random skips,
  reactions, typo edits, 1–5 min "distraction" pauses, and 10–30 minute AFK
  breaks 2–4 times per 6h run.
- Routes traffic through **Cloudflare WARP** by default — a free, anonymous
  WireGuard VPN that exits via Cloudflare's network instead of raw Azure
  datacenter IPs (which caused the v4.2 shadow-deletes).
- Stops automatically if: the token is banned, outbound IP is still Azure
  after retries, WARP routes to a country outside your allowlist, or 5
  different message variations get deleted in a row (meaning the account/IP
  is flagged, not the text).

### Anti-detection stack (the short version)
1. **curl_cffi** impersonating Chrome — real TLS/JA3/HTTP2 fingerprint.
2. **WebSocket gateway** — IDENTIFY, heartbeats, presence (account appears online).
3. **Cookie + X-Fingerprint warmup** (visits `/`, `/app`, `/experiments`, `/science`
   before attaching the Authorization token, exactly like a browser).
4. **Complete browser headers** — X-Super-Properties (b64 JSON with real build
   number), X-Discord-Locale/Timezone, per-channel Referer,
   X-Discord-Idempotency-Key, snowflake `nonce`, allowed_mentions (blocks
   @everyone/@here).
5. **Behavioral humanization** — pre-thinking pause, length-scaled typing,
   shuffled channel order, AFK breaks, distractions, 8–30% per-channel skip
   rate, 18% typo-edit chance, 10% reaction chance.
6. **Image anti-fingerprint** — EXIF strip, random filename, JPEG quality
   jitter (90–96), ±1px RGB jitter on 30 random pixels → unique hash every upload.
7. **📌 First 3 posts are text-only** (warmup) even when `attach_image=yes`.
8. **Smart cooldown** — only reposts when others have posted after you;
   safety-net force-post at 2.5× interval.
9. **📌 Triple IP verification** — WARP connected + org is not Azure + country
   allowlist checked BEFORE the bot ever sends a message.

---

## 2. Prerequisites

You need:

1. **A Discord alt account** (never your main).
   - Email verified, avatar set, short bio, ideally phone verified and 2FA
     enabled (big trust boost — see §3).
2. **The alt's token** (how to get it below).
3. **Channel IDs** for the marketplace channels you want to post in.
4. **A GitHub account** (free) and a private repository to host the code.
5. **(Optional)** A residential proxy URL in the form
   `http://user:pass@host:port` if you want better-than-WARP IP trust.
   WARP is free and works for most servers; a $1–4/GB residential proxy is
   the belt-and-suspenders option for hard-to-crack anti-spam.

### Getting your alt's token (desktop browser)
1. Open Chrome (regular window, not Incognito — tokens last longer there).
2. Log into the alt at `discord.com/login`. Complete any new-location/email
   verification prompts.
3. Press **F12** to open DevTools → **Network** tab.
4. Send any message (like "test") in any channel.
5. Click the `messages` network request → **Headers** → scroll to **Request
   Headers** → copy the value of `authorization` (a ~70-char alphanumeric
   string, sometimes with dots).

**Token lifetime:** days-to-weeks unless you log out, change password, or get
banned. Closing the browser does NOT invalidate it.
🔒 Paste this only into the GitHub secret `USER_TOKEN`. Never commit it.

### Getting channel IDs
1. Discord → User Settings (gear icon) → **Advanced** → toggle **Developer Mode** ON.
2. Right-click each target channel → **Copy Channel ID**.
3. Combine with commas, e.g. `1541658382015135817,1103759996468080752`.

---

## 3. Aging the alt (DO THIS BEFORE RUNNING)

This is the #1 thing people skip and then wonder why messages get deleted.

**Minimum:**
- ✅ Account age **3+ days** (7+ is much safer).
- ✅ Email verified.
- ✅ Custom avatar + username (not default pfp).
- ✅ Short bio (anything — "trader" works).

**Strongly recommended:**
- 📱 **Phone verify** (massive trust boost; cheap prepaid SIMs work).
- 🔒 **Enable 2FA** (raises account trust score visibly).
- 💬 Spend 10–15 min **manually chatting** in the target servers before the
  first run (say hi, react to a few messages, scroll — generates normal
  telemetry).
- 🤝 Join servers gradually; don't mass-join 10 servers in 1 minute (raid signal).
- 👥 Add 1–2 friends (your main, a friend) — 0-friend accounts look like throwaways.

Running a brand-new email-only alt straight from cloud IP sending image ads =
almost guaranteed shadow-delete by Wick/Beemo/Double Counter. **Age the alt.**

---

## 4. Installation (GitHub Actions)

### Step 1 — Create the repository
1. Create a new **private** GitHub repo (e.g. `bb-ads`).
2. Upload these files to the repo root:
   - `send_ads.py`
   - `.github/workflows/send_ads.yml` (note the folder name starts with a dot)
   - _(optional)_ your ad image — default name `ad.png` (PNG/JPG/WEBP/GIF, <8MB)

### Step 2 — Add GitHub Secrets
Go to repo → **Settings → Secrets and variables → Actions → New repository
secret**. Add these:

#### Required
| Secret | Value |
|---|---|
| `USER_TOKEN` | Your alt's Discord token (from §2). |
| `CHANNEL_IDS` | Comma-separated channel IDs (from §2). You can override per-run with `channel_1`/`channel_2`. |

#### 📌 WARP is automatic — no secret required
Cloudflare WARP installs and connects automatically in the workflow. It is
**skipped automatically** when you set `HTTPS_PROXY` (residential proxy beats
WARP), or when you set secret `WARP_ENABLED=0` (not recommended — raw Azure IP
is flagged by every anti-spam bot).

#### Recommended (optional but strongly suggested)
| Secret | Default | What it does |
|---|---|---|
| `IMAGE_FILENAME` | `ad.png` | Image filename at repo root (used when `attach_image=yes`). |
| `CUSTOM_STATUS_TEXT` | `Trading` | Custom status shown on your alt's profile. |
| `STATUS_EMOJI` | `💰` | Emoji prefix for the custom status. |
| `DISCORD_LOCALE` | `en-US` | Locale header. Set to `fr-FR`, `es-ES`, etc. to match you. |
| `DISCORD_TIMEZONE` | `America/New_York` | Timezone header (e.g. `Europe/Madrid` for Granada/Spain, `Africa/Casablanca` for Morocco). |
| `HTTPS_PROXY` | _(none)_ | Residential proxy URL `http://user:pass@host:port` (see §9). If set, WARP auto-skips and all traffic (including WebSocket and webhook DMs) routes through the proxy. |
| `WARP_ENABLED` | `1` | Set to `0` to disable WARP (only use if you have HTTPS_PROXY or you're on a self-hosted runner). |
| `DEBUG` | `0` | Set to `1` for verbose debug logs. |

**Recommended for Granada/Spain:**
```
DISCORD_LOCALE = fr-FR
DISCORD_TIMEZONE = Europe/Madrid
```

#### 📌 Anti-detection tuning (only change if you know what you're doing)
| Secret | Default | What it does |
|---|---|---|
| `WARMUP_POSTS` | `3` | Text-only posts before images (raises session trust). |
| `STRIP_EXIF` | `1` | Strip EXIF/metadata from images. |
| `IMAGE_JITTER` | `1` | Apply ±1-pixel jitter to images (unique hash per upload). |
| `RANDOM_REACT` | `1` | Occasionally react to others' messages. |
| `IDLE_REACT_CHANCE` | `0.10` | Per-cycle reaction probability (0–1). |
| `TYPO_EDIT_CHANCE` | `0.18` | Probability of post-send typo-fix edit (0–1). |
| `ENABLE_GATEWAY` | `1` | Open WebSocket (turning off is highly suspicious). |
| `SUPPRESS_EMBEDS` | `0` | Sometimes suppress link previews. |
| `PROXY_CHECK` | `1` | Log outbound IP/org/country on startup. |
| `MIN_AFK_BREAKS` | `2` | Minimum AFK breaks per 6h chunk. |
| `MAX_AFK_BREAKS` | `4` | Maximum AFK breaks per 6h chunk. |
| `AFK_MIN_MIN` | `10` | Shortest AFK (minutes). |
| `AFK_MAX_MIN` | `30` | Longest AFK (minutes). |

#### 📌 DM forwarding — see buyer DMs on your main account
| Secret | Default | What it does |
|---|---|---|
| `DM_WEBHOOK_URL` | _(none)_ | Discord webhook URL for a private channel. Forwards ALL DMs (both sides) there: buyer username/avatar, message text, attachment links, timestamp, and a clickable "**Open DM**" deep link that opens Discord straight to the conversation on the alt. |
| `DM_PAUSE_MINUTES` | `2` | When a buyer DMs, the bot **automatically pauses ALL public activity** (posts, reactions, typing) for N minutes. This guarantees there's never simultaneous "bot posting in #trading" + "you replying to a buyer" activity (a major fingerprint). |
| `FORWARD_OWN_DMS` | `1` | Also forward messages the alt sends (complete two-sided log). Set to `0` for incoming-only. |

Setup (30 seconds):
1. On your **main** account, create a private server and a channel like `#alt-dms`.
2. Channel Settings → Integrations → Webhooks → New Webhook → name it "Alt DMs",
   bind to `#alt-dms` → **Copy Webhook URL**.
3. Paste as secret `DM_WEBHOOK_URL`.
4. When a buyer DMs you get a push notification on your main instantly. Tap
   "Open DM" → Discord switches to the alt and opens the conversation. Reply
   normally via the account switcher.

#### 📌 Auto-learn — remembers which messages got blocked
| Secret | Default | What it does |
|---|---|---|
| `BLOCKED_STRIKES` | `2` | How many times a message variation must be deleted before being permanently blacklisted. |
| `BLOCKED_SAFETY_STOP` | `5` | If 5+ DIFFERENT variations die back-to-back → stop (account/IP is flagged, not the text). |
| `GIST_TOKEN` | _(none)_ | Optional GitHub Personal Access Token (only the `gist` scope). Without it the blocklist resets each run. |
| `GIST_ID` | _(none)_ | Optional ID of a secret gist (create once at gist.github.com). The bot persists `blocked_variations.json` there so memory survives across runs and chunk boundaries. |

How it works: ~35 seconds after each post the bot re-fetches the message. If
it's gone (404/403), that variation gets a strike. After `BLOCKED_STRIKES`
strikes it is blacklisted forever (persisted to the gist). If 5 in a row die,
the bot exits with code 2, canceling all remaining chunks (fail-fast).

#### 📌 Geo-country check
| Secret | Default | What it does |
|---|---|---|
| `ALLOWED_COUNTRIES` | _(off)_ | Comma-separated ISO codes, e.g. `ES,FR,NL,DE,IE,GB,PT,MA,IT`. If WARP routes you to an unexpected country the run aborts before any message is posted (prevents sudden location jumps that trigger email verification). Leave empty to skip. |

---

## 5. Running the Bot

### Triggering a run
1. Push your files to GitHub.
2. Go to the repo's **Actions** tab → enable Actions if prompted.
3. Click **Marketplace Ad Sender** in the left sidebar.
4. Click **Run workflow** (top right, on the `main` branch).
5. Fill in the form:

### 📌 Workflow inputs (the dropdown form)

| Input | Options | Meaning |
|---|---|---|
| `ad_type` | `sell` / `buy` | SELL BB or BUY BB. |
| `sell_rate` | _text, default `2.5$`_ | [SELL] Rate → "SELLING BB LF X/1K ..." |
| `sell_extra` | _text, default `DM ME QUICK CAN DO SMALL AND BIG AMOUNTS`_ | [SELL] Extra text after the rate. |
| `buy_style` | `detailed` / `simple` | [BUY] `detailed` = multi-line bullets (always text-only, image flag ignored), `simple` = single line (image allowed). |
| `buy_rate` | _text, default `2.2`_ | [BUY detailed] Tokens rate. |
| `buy_rate_rap` | _text, default `1.8`_ | [BUY detailed] RAP rate. |
| `buy_simple_text` | _text, default `BUYING ALL BLADE BALL DM ME QUICK`_ | [BUY simple] Full message text. |
| `channel_1` | _text, empty_ | Optional: post ONLY to this channel for this run (overrides `CHANNEL_IDS`). |
| `channel_2` | _text, empty_ | Optional: post to channel_1 + channel_2. |
| `interval_min` | `3` / `5` | Minutes between posts per channel. **5 = recommended** (safer); 3 = more aggressive. |
| `total_hours` | `6` / `12` / `18` / `24` / `48` | Runtime. Auto-split into chained 6-hour chunks with 2–5 min gaps (simulating app restart). `max-parallel: 1` and `fail-fast: true` — any chunk that exits 2 (ban/flagged/WARP failure) cancels all later chunks. |
| `attach_image` | `yes` / `no` | Attach image? (First 3 posts after image is enabled are still text-only warmup; BUY detailed is always text-only.) |

### First-run recommendation
```
ad_type:       sell (or buy)
interval_min:  5
total_hours:   6
attach_image:  no     ← text-only test first!
channel_1/2:   empty (uses CHANNEL_IDS)
```

After you have confirmed (by opening Discord on your main and seeing your own
ad in the channel) that messages are visible, re-run with `attach_image: yes`.

---

## 6. What Happens During a Run

Each chunk runs for up to 6 hours. Here's the play-by-play:

### Phase 1 — IP safety (before any Discord traffic)
1. Workflow installs Python + dependencies (`curl-cffi`, `Pillow`, `websocket-client`).
2. **📌 WARP connects** (if `HTTPS_PROXY` is empty and `WARP_ENABLED != 0`).
   Uses `sebst/actions-warp@v1` (official `cloudflare-warp` package,
   `warp-cli registration new` is anonymous/ephemeral, no account/email/CB
   needed), system-level WireGuard tunnel routes all traffic.
3. **📌 Triple IP verification** — retries for up to ~20 seconds (WARP can take
   a moment to switch routes):
   - Checks `https://www.cloudflare.com/cdn-cgi/trace` for `warp=on`.
   - Fetches `https://api.ipify.org` and resolves org via `ipapi.co`.
   - If HTTPS_PROXY is set, aborts if org is still Microsoft/Azure.
   - If WARP expected but not active, aborts with exit 1.
   - If ALLOWED_COUNTRIES is set and country doesn't match, aborts with exit 2.

### Phase 2 — Browser warmup (≈ 1–2 min)
4. Cookie/x-fingerprint warmup: `GET /`, `GET /app`, `GET /api/v9/experiments`
   (captures `X-Fingerprint`), `POST /api/v9/science` (telemetry ping).
5. Scrapes the real `buildNumber` from Discord's JS assets; falls back to
   `_DEFAULT_BUILD = 387211` if the scrape fails.
6. Builds the full Chrome header set (X-Super-Properties b64, X-Debug-Options,
   Origin, Referer, locale/timezone, Authorization token added last).

### Phase 3 — Auth + gateway
7. Boot delay: 8–20 seconds (simulates Discord app launch).
8. Validates token via `GET /users/@me`. Logs alt username, email verification,
   2FA status.
9. **WebSocket gateway connects** (if enabled):
   - `wss://gateway.discord.gg/?v=9&encoding=json` → HELLO (op10).
   - IDENTIFY (op2) with Chrome browser properties, capabilities 114681,
     custom status presence, client_state.
   - Heartbeat thread starts (interval from server, default ~41.25s).
   - READY (op0 t=READY) → sets `connected`, caches private channels, starts
     processing MESSAGE_CREATE for DMs.
   - On disconnect: reconnects after 3–7s (daemon thread, automatic).
10. Sets custom status via REST PATCH.

### Phase 4 — Channel browsing (≈ 1–2 min)
11. For each channel: `GET /channels/{cid}` (reads name, slowmode, guild_id for
    Referer construction), `read_channel` (fetches last 15 messages, ACKs the
    latest), "gaze" sleep 3–9s per channel. Inaccessible channels are added to
    `dead_channels` and skipped for the rest of the run.
12. Reading-chat warmup: 40–90s staring at channels.
13. Re-reads all channels, 8–20s final pause before first post.

### Phase 5 — AFK plan
14. Schedules 2–4 AFK breaks, each 10–30 min long, with ≥15 min gaps between
    them. The break schedule is logged so you can see when the bot is "away".

### Phase 6 — Main loop (repeats until run_end)
15. At the top of each cycle:
    - If currently in an AFK break, sleeps in 60s chunks firing keepalive
      pings every ~4.5 min.
    - After AFK ends: waits 15–45s "re-orienting" delay, re-reads channels.
    - 10% chance of a 1–5 min "distraction" pause (checking DMs/other servers).
    - 5% mid-cycle distraction chance after a post (45–180s).
16. Channels are sorted by ascending slowmode wait (so ready channels are
    processed first), with a 0–2s random jitter in the sort key.
17. For each channel:
    - Skips dead channels, paused periods, and channels with ≥3 recent errors
      (exponential backoff).
    - If slowmode is active and we've already posted to ≥1 channel this cycle,
      defers to next cycle (doesn't block other channels). If no channels are
      ready, waits the minimum slowmode remaining.
    - Fetches last 5 messages (cache-busted). Checks whether our last ad is
      still the latest; if so, skips (cooldown).
    - Checks if our previous message ID has dropped out of the last 5 →
      `⚠️ previous ad appears DELETED` → repost.
    - Safety-net force-post if > 2.5× INTERVAL_MIN has elapsed since last send.
    - Per-channel skip: 8–30% (random per cycle, post_threshold sampled 0.70–0.92).
    - Picks a random variation not yet used this cycle and not in the
      auto-learn blacklist. If all variations are blacklisted → critical stop.
    - **Text-only warmup:** first 3 posts are text-only regardless of
      `attach_image` (images trigger stronger anti-spam from new sessions).
    - After warmup: ~60% chance to attach the processed image (filename
      randomized, EXIF stripped, pixel-jittered, quality jittered).
    - Sends typing indicator (1.8–4.5s pre-thinking + length-scaled typing
      duration with 5% hesitation pause + 8% mid-typing pause).
    - POSTs the message (idempotency key, snowflake nonce, allowed_mentions).
    - On success: schedules a post-send verification daemon thread (~35s
      later, re-fetches the message, records a strike if gone → auto-learn).
    - 18% chance of a typo-fix edit 5–22s after posting (with new typing
      indicator). Fired on a daemon thread.
    - After posting: 40% chance to "glance at" another channel (read + ACK),
      25% chance of a 20–55s long stare, else 8–22s normal pause.
    - 10% chance (while on cooldown) to react 🔥/💯/👀/✅/👌/💸/🤑/💎 to a
      random non-self, non-command message from another user.
    - If a buyer DMs mid-cycle, breaks out and respects the public-activity
      pause.
18. After all channels are processed (or skipped): sleeps INTERVAL_MIN ×
    uniform(0.70, 1.45) seconds (~3.5–7.5 min at default interval 5), firing
    REST keepalives every ~4.5 min so the session never looks idle.

### Phase 7 — End of chunk
19. Logs final stats (sent, errors, skipped, edits, per-channel breakdown).
20. Saves the auto-learn blocklist to the gist (if configured).
21. Stops the WebSocket. Exits 0.
22. Next chunk (if any) waits 120–300s (random), then starts fresh.

---

## 7. Monitoring & Troubleshooting

### How to read the logs
Open the running workflow in the Actions tab. Key lines:

| Log line | Meaning |
|---|---|
| `✅ Cloudflare/WARP is ACTIVE — traffic leaves via Cloudflare, NOT Azure` | 📌 WARP is working (this comes from the hard IP-verification step BEFORE the bot starts). |
| `🌐 Outbound IP: 104.x.x.x (Cloudflare, Inc.) [FR]` | Outbound IP is Cloudflare (good). |
| `🌐 Outbound IP: x.x.x.x (Orange S.A.) [ES]` | Residential proxy working (good). |
| `⚠️ Datacenter IP detected!` | You're on Azure/AWS/OVH/Hetzner/etc without a working WARP/proxy — expect shadow-deletes. |
| `🔑 Warming up browser session` | Cookie/fingerprint warmup starting. |
| `UA: Chrome NNN / Build: NNNNNN / Fingerprint: OK` | Browser fingerprint built. |
| `✅ Logged in : username (id=NNN)` | Token works. |
| `🟢 Gateway online as username (session abcdef12)` | WebSocket connected (account shows online). |
| `📡 Browsing channels (warmup reads)...` | Pre-post browsing. |
| `👀 Reading chat for Ns before first post...` | Gaze time before first post. |
| `☕ AFK breaks : N scheduled` + break time list | AFK plan logged. |
| `── Cycle N [...] ──` | Start of a posting cycle. |
| `✅ #channel: 💬 "message..." (total: N, id=...)` | Text message sent successfully. |
| `✅ #channel: 📷 "message..."` | Image message sent. |
| `⏭️ #channel: our ad still latest (by user), waiting` | Our ad is still newest; skipped (normal). |
| `↪️ #channel: skipped this pass (human-like)` | Random 8–30% skip (normal human behavior). |
| `⚠️ #channel: previous ad appears DELETED (anti-spam) -- reposting` | Anti-spam removed our last message. If this happens 2+ times in a row, check visibility manually. |
| `🔰 warmup post (X/3) -- text-only` | In text-only warmup phase. |
| `👌 reacted 🔥 to "..."` | Reacted to someone else's message. |
| `✏️ edited msg to "..."` | Typo-fix edit applied. |
| `☕ AFK break -- N min left` | In an AFK break (keepalives still fire). |
| `💭 Distraction -- Ns` / `💭 Mid-cycle distraction` | Short random pause. |
| `👋 Back from AFK — catching up for Ns...` | 📌 Post-AFK re-orientation pause + channel re-read. |
| `⏳ Next ~HH:MM (in N min)` | Waiting between cycles. |
| `⏸️ Public activity paused for N min (buyer DM)` | 📌 Buyer just DMed — bot paused public activity so you can reply. |
| `💌 DM from Username: ...` | Incoming DM forwarded to webhook. |
| `📚 Loaded N blocked variations from gist` | Auto-learn memory loaded from persisted gist. |
| `🚫 Blacklisted variation: "..."` | That variation is now permanently blocked. |
| `🛑 SAFETY STOP: N different variations deleted in a row` | Account/IP is hard-flagged; bot stopped to protect the alt. |
| `❌ CRITICAL: Token invalidated -- likely banned. Stopping.` | Token died (ban/logout). Exit code 2 → all chunks cancel. |
| `❌ ABORT: IP is in XX which is NOT in ALLOWED_COUNTRIES` | 📌 Geo-check triggered. |

### What to do if messages aren't visible (shadow-delete)
1. Open Discord on your main/phone and **look for your ad** in the channel.
   Don't trust the API response — shadow-deleted messages return 200 OK.
2. If the first message isn't visible:
   - Wait 1 minute (slow-mode delay can make messages appear late).
   - Still not visible? Cancel the run.
3. Options, in order of increasing effort/cost:
   - Age the alt more (chat manually for a day, enable 2FA, verify phone).
   - Make sure WARP is active (look for the "WARP is ACTIVE" line). If WARP
     connects but messages are still deleted, WARP's Cloudflare IPs may also
     be flagged on that specific server — move to a residential proxy.
   - Set `HTTPS_PROXY` to a residential proxy (Proxyma free 500MB, Decodo,
     DataImpulse — see §9 for options).
   - Use the self-hosted runner (§8) on your home PC, ideally with proxy set.

### Common problems

| Problem | Likely cause | Fix |
|---|---|---|
| Log says "sent" but message invisible | Shadow-deleted by Wick/Carl/Beemo | Use residential proxy; age alt; run text-only first |
| `⚠️ Datacenter IP detected!` | WARP disabled or proxy not set | Don't set `WARP_ENABLED=0`; verify proxy URL starts with `http://` (not `https://`) |
| `❌ ABORT: IP is still Microsoft/Azure after 20s of retries` | WARP failed to connect | Retry workflow; if it fails repeatedly add a residential proxy |
| `❌ CRITICAL: Token invalidated` | Token banned/expired | Re-copy token; if alt can't log in, use a new alt |
| `❌ 403 ...` on a channel | Alt kicked/lost access | Check the alt is still in the server and can post |
| `❌ 404 ...` on a channel | Channel deleted or wrong ID | Re-copy the channel ID in Developer Mode |
| `❌ 400 ... blocked/automod/flagged` | Server AutoMod blocked the text | Change your MESSAGE / lower-case / remove flagged words |
| `⚠️ Rate limit (GLOBAL)` | Hit global rate limit | Bot auto-waits (chunked, keepalives preserved); usually 30–120s |
| Workflow fails at WARP step | `sebst/actions-warp` rate limit or geo-block | Wait 10 minutes and retry; if it persists, set `HTTPS_PROXY` and WARP auto-skips |
| Webhook DMs not appearing | Wrong webhook URL or missing permissions | Verify webhook URL; DMs still appear normally on the alt |
| Stuck on "our ad still latest" despite busy chat | Shadow-deleted (message visible only to you) | Cancel and switch proxy/IP; age alt more |
| Gateway never shows "🟢 Gateway online" | WebSocket blocked by proxy | Check `HTTPS_PROXY` format; verify proxy allows CONNECT to port 443 |

---

## 8. Risk & Safety

- **Discord ToS violation:** Self-bots violate Discord ToS. Use an alt only.
- **One instance at a time:** The workflow has `concurrency: ad-sender-global`
  with `cancel-in-progress: true` — if you trigger a new run while one is
  active, the old one is cancelled automatically. Don't run multiple repos
  or multiple copies of the workflow simultaneously.
- **Recommended interval:** 5 minutes for normal use; 3 minutes is the minimum
  in the dropdown and is noticeably more aggressive.
- **Session length:** 6-hour sessions, with 2–4 AFK breaks totaling 20–120
  minutes of idle time per session, match real usage. Two 6h runs/day is
  safer than one 48h marathon.
- **If banned:** Discord bans are silent (no email, no warning). If the run
  exits 401 and you can't log into the alt, it's banned. Create a new alt,
  age it, update `USER_TOKEN`.
- **Don't delete messages:** The bot never auto-deletes (messages stack
  naturally). Don't manually mass-delete your ads after a run either —
  bulk self-deletion is a known account-scoring signal.
- **Phone 2FA:** Enable it on the alt. It raises the trust score substantially.

---

## 9. 📌 Advanced: Self-Hosted Runner (optional fallback)

If Cloudflare WARP gets flagged on your target server and you don't want to
pay for a residential proxy, you can run the bot on your own PC (real
residential ISP IP — Orange/Movistar/Free/etc.) using a GitHub self-hosted
runner. Your PC's IP shows as a normal residential customer, which is the
highest-trust IP type Discord sees.

### Setup (5 minutes)
1. Your PC must stay on, awake, and connected to the internet.
2. GitHub repo → **Settings → Actions → Runners → New self-hosted runner**.
3. Follow the OS-specific copy-paste commands (Windows/macOS/Linux).
4. Once the runner is connected ("Idle" in the runners list), edit
   `.github/workflows/send_ads.yml` and change:
   ```yaml
   runs-on: ubuntu-latest
   ```
   to:
   ```yaml
   runs-on: self-hosted
   ```
5. Install Python 3.10+ on your PC. The runner auto-pip-installs dependencies
   at the start of each chunk.
6. **Recommended:** still set `HTTPS_PROXY` to a residential proxy so your
   home IP isn't exposed to Discord as a self-bot IP. If you don't set a proxy,
   WARP will still attempt to connect but on a home PC you're already on a
   residential IP — WARP is unnecessary on self-hosted (set `WARP_ENABLED=0`).

### Running without GitHub Actions (direct CLI)
You can also run the bot directly with environment variables (useful for
testing on a PC or Termux phone):

```bash
export USER_TOKEN="your_token_here"
export CHANNEL_IDS="1541658382015135817,1103759996468080752"
export AD_TYPE="sell"
export MESSAGE="SELLING BB LF 2.5\$/1K DM ME QUICK CAN DO SMALL AND BIG AMOUNTS"
export ATTACH_IMAGE="no"
export IMAGE_PATH="ad.png"
export INTERVAL_MIN=5
export TOTAL_RUN_MIN=360
export CUSTOM_STATUS_TEXT="Trading"
export STATUS_EMOJI="💰"
export DISCORD_LOCALE=fr-FR
export DISCORD_TIMEZONE=Europe/Madrid
export WARMUP_POSTS=3
python -u send_ads.py
```

**For BUY simple:** `export MESSAGE="BUYING ALL BLADE BALL DM ME QUICK"`
**For BUY detailed (bash):**
```bash
export MESSAGE=$'BUYING BLADE BALL:\n\n-TOKENS 2.2/1K\n\n-RAP 1.8$/1K (nlf boosted)\n\nDM me quick'
```

A helper script `setup_selfhosted_runner.sh` is included in the repo for
one-command runner setup on Linux.

---

## 10. Frequently Asked Questions

**Q: How do I get my token?**
A: See §2. Desktop Chrome: F12 → Network → send a message → click the
`messages` request → copy the `authorization` header. Tokens are ~70 chars.
Never share it or commit it.

**Q: Why are my messages being deleted?**
A: Three possibilities in order of likelihood:
1. **IP reputation:** Azure/datacenter IP (v4.2 problem) — WARP didn't connect
   or your proxy isn't working. Check for `⚠️ Datacenter IP detected!`.
2. **Account age/trust:** New alt, no phone, default pfp, no prior chat
   history. Age the alt more (§3).
3. **Content flag:** Server AutoMod is blocking specific words/phrases. Try
   simpler message text, remove special characters, lower-case. If the bot
   logs `400 ... blocked/automod/flagged`, auto-learn immediately blacklists
   that variation.

**Q: Why is the bot skipping channels?**
A: Three reasons, all intentional:
- **Smart cooldown:** our ad is still the latest message in that channel
   (logged as `⏭️ our ad still latest`). Normal — we don't spam.
- **Random human-like skip:** 8–30% per channel per cycle (logged as `↪️
   skipped this pass`). Real users don't post every single time they open
   a channel.
- **Channel errors/dead:** 3 consecutive errors on a channel → skip + back
   off (logged as `too many errors, backing off`); 404 → channel marked dead.

**Q: Can I post to more channels?**
A: Yes. Add more channel IDs (comma-separated) to `CHANNEL_IDS`, or fill in
`channel_1`/`channel_2` per run. There's no hard limit, but posting to 4+
channels increases post volume and detection risk. Stick to 1–3 for safety.

**Q: Can I attach multiple images?**
A: No — one image per post (Discord limit per message without nitro is also
one file for non-nitro users; matches human behavior).

**Q: Will WARP get me shadow-banned?**
A: Cloudflare WARP is a consumer VPN used by millions of real 1.1.1.1 app
users. It's not a residential IP, but it's much less flagged than Azure. It
works on most servers but a few high-security servers (with Wick/Custom
Anti-Raid) may flag it. If you see deletions with WARP on, move to a
residential proxy or self-hosted runner.

**Q: How do I pause/stop a run?**
A: Go to the Actions tab, click the running workflow, click **Cancel workflow**.
The `concurrency: ad-sender-global` group ensures triggering a new run also
cancels the previous one.

**Q: Can the bot reply to DMs automatically?**
A: No — by design. Auto-replying to DMs is a strong bot signal. You reply
manually via Discord's account switcher. The bot only forwards DMs to your
webhook and pauses public activity so you have time to reply.

**Q: What if WARP registration fails?**
A: The `sebst/actions-warp` action occasionally hits Cloudflare rate limits
or geo-blocks (the consumer registration endpoint may refuse some cloud IP
ranges). Wait 10 minutes and retry. If it consistently fails, use a
residential proxy (set `HTTPS_PROXY`; WARP auto-skips when proxy is set).

**Q: How do I run from my phone?**
A: Use Termux on Android (F-Droid version) over 4G/5G. See the longer guide
(`docs/TERMUX.md` if included, or the Termux section in the full RESEARCH_FINDINGS).
Mobile carrier IPs are the most trusted IP type.

---

## Changelog

**v5.2 (final):** Cloudflare WARP auto-install with triple IP verification,
DM forwarding to webhook with buyer pause, auto-learn blacklist with gist
persistence, geo-country allowlist, gateway fixes (heartbeat timeout/zombie
detection, thread-offloaded DM handling), widened timing jitter (±30-45%),
post-AFK re-orientation pause, mid-cycle distractions, image attach rate
lowered to 60%, typo-edit rate raised to 18%, typing hesitation pauses,
multipart retry fix (retries=1), session resume prep, webhook routes via
proxy, sys.exit → os._exit in daemon verification thread for real safety
stop, per-channel slowmode deferral (no blocking other channels), idempotency
key tightened to only POST /messages.

**v5.1:** WebSocket gateway, snowflake nonce, typo edits, pixel jitter,
browser capabilities bitmask, science telemetry, cookie forwarding on WS.

**v5.0:** curl_cffi TLS fingerprint, cookie warmup, X-Super-Properties,
channel ACKs, EXIF strip, text-only warmup, per-channel Referer, random
reactions, IP check.

**v4.x:** requests baseline (detected on first run due to Azure datacenter IP
+ missing TLS fingerprint + no WebSocket).
