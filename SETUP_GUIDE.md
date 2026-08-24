# 📨 Marketplace Discord Ad Sender — Setup Guide (v4.2)

Posts ONE ad at a time (SELL or BUY) to your marketplace channels, running on
GitHub Actions cloud. Messages stack naturally (no auto-delete), smart cooldown
waits until others post, and behavior is modeled after a real human trader
opening Discord, reading channels, and posting ads.

---

## ⚠️ Honest risk disclosure

This uses a **Discord user token** (self-bot). It violates Discord ToS. Your
alt **can** be banned. The behavioral anti-detection in v4.2 is thorough but
nothing eliminates risk. Use an aged alt, keep sessions reasonable (a few
hours at a time), and stop immediately if you see global rate limits.

**Datacenter IP note:** GitHub Actions runs on Microsoft Azure IPs. Residential
IPs are safer, but behavioral patterns matter more than IP. Short daily sessions
(1-6h) on an aged alt with human-like timing are generally fine. If you ever
get banned for IP reasons, you can later move to a cheap residential proxy or
VPS, but for now this setup is appropriate for starting out.

---

## Step 1 — Get your alt's token (Chrome, regular or Incognito)

1. Open Chrome (Incognito optional, see note below) and log into the alt at
   discord.com/login. Complete 2FA / captcha normally.
2. Press **F12** → **Network** tab.
3. Send a message like "test" in any channel.
4. Click the **`messages`** request that appears → **Headers** → scroll to
   **Request Headers** → copy the **`authorization`** value.

**Token lifetime:**
- Token stays valid days-to-weeks unless you: log out, change password, get
  banned, or Discord forces re-auth.
- Closing Incognito does NOT immediately invalidate the token (Discord keeps
  sessions server-side), but you can't re-open dev tools on that session once
  closed — copy the token while the window is open.
- For longest-lived tokens, use a regular Chrome profile (not Incognito).

🔒 Never share or commit this token. Only paste it into GitHub Secrets.

---

## Step 2 — Copy channel IDs

1. Discord → User Settings (gear) → **Advanced** → Developer Mode ON.
2. Right-click each ad channel → **Copy Channel ID**.
3. Combine with commas:
   ```
   123456789012345678,987654321098765432
   ```

---

## Step 3 — Prepare `ad.png` (for SELL ads only)
Save a rate sheet/screenshot as `ad.png` (under 8MB). BUY ads are text-only.

---

## Step 4 — Create private GitHub repo

1. github.com → **+** → **New repository**.
2. Name it something boring (e.g. `market-notes`). Set **Private**. UNCHECK
   "Add a README". Create.
3. On the empty repo page, click **"uploading an existing file"** and upload:
   - `send_ads.py`
   - The `.github/` folder (with `workflows/send_ads.yml` inside)
   - `ad.png` (your sell image, optional)
4. **Commit changes**.

---

## Step 5 — Add Secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

**Required (2):**

| Secret | Value |
|---|---|
| `USER_TOKEN` | Token from Step 1 |
| `CHANNEL_IDS` | Comma-separated IDs from Step 2 |

**Optional (sane defaults set):**

| Secret | Default | Purpose |
|---|---|---|
| `CUSTOM_STATUS_TEXT` | `💰 Trading` | Custom status |
| `STATUS_EMOJI` | `💰` | Status emoji |
| `IMAGE_FILENAME` | `ad.png` | Image filename if different |
| `MIN_AFK_BREAKS` | `2` | Min AFK pauses per 6h |
| `MAX_AFK_BREAKS` | `4` | Max AFK pauses per 6h |
| `AFK_MIN_MIN` | `10` | Shortest AFK (min) |
| `AFK_MAX_MIN` | `30` | Longest AFK (min) |
| `DISCORD_LOCALE` | `en-US` | Locale header |
| `DISCORD_TIMEZONE` | `America/New_York` | Timezone header |
| `DEBUG` | off | Set to `1` for verbose logs |

---

## Step 6 — Run it

1. **Actions** tab → **Marketplace Ad Sender** → **Run workflow**.
2. Fill in the form:

   | Field | Default | What it does |
   |---|---|---|
   | **Ad type** | `sell` | `sell` or `buy` |
   | **SELL rate** | `2.5$` | Fills "SELLING BB LF X/1K" |
   | **BUY tokens rate** | `2.2` | Fills "-TOKENS X/1K" |
   | **BUY RAP rate** | `1.8` | Fills "-RAP X$/1K (nlf boosted)" |
   | **SELL extra text** | `DM ME QUICK CAN DO SMALL AND BIG AMOUNTS` | After sell rate |
   | **Minutes between posts** | `5` | `3` or `5` (5 safer) |
   | **Total runtime** | `6` | `6/12/18/24/48` hours |
   | **Attach image** | `yes` | Only for SELL ads |

3. Click **Run workflow**. A run title appears, e.g.:
   ```
   💰 SELL BB @ 2.5$/1K | 5min × 6h | 📷img
   ```
4. Click into the run → **send-chunk** → **"Run ad sender"** to see live logs.
5. **Close the tab.** It runs on GitHub's servers.

To stop: Actions → running workflow → **Cancel workflow**.

Starting a new run **automatically cancels** any existing run.

---

## 🧠 Anti-detection / human behavior (v4.2 details)

What you'll see in logs and what it means:

- `⏳ Startup delay 47s...` — simulates waiting after opening Discord
- `📡 Checking & reading channels (warmup)...` — fetches recent messages from
  every channel (simulates scrolling/reading), with small gaps, before posting
- `👀 Simulating reading/scroll for 23s before first post...` — extra warmup pause
- `🟢 Status: '💰 Trading'` — sets custom profile status
- `☕ AFK breaks: 3 scheduled` — random silent periods during the session
- `── Cycle 4 [💰SELL] | 341 min left ──` — main cycle header
- `⏭️ #: our ad still latest, waiting` — smart cooldown, channel is slow
- `↪️ #: skipped this pass (human-like)` — random 5-15% per-channel skip rate
- `   💭 Distraction pause — 142s (like checking a DM)` — random short pauses
- `   ✅ #: "💸 SELLING BB..." (total: 23)` — successful post
- `☕ AFK break — 14.2 min left` — long silent period (but with tiny keepalives)
- `💓 keepalive` (debug only) — tiny background ping during long silences
- `🏁 Reached scheduled end time.` — clean finish

**What you will NOT see** (that's good):
- No `🗑️ Deleted old ad` messages (no more auto-delete fingerprint)
- No metronomic perfect-5-minute posts (jitter ±25%)
- No instant first post (startup + warmup delays)
- No identical messages on every channel (different variation per channel)

---

## 📱 Monitoring DMs

Discord mobile has built-in **account switcher**: profile pic → scroll down →
**Switch Accounts** → Add Account. Both main and alt stay logged in and both
send push notifications. No second phone number needed.

---

## ❓ FAQ

**I click Run twice?** The old run cancels automatically. No double-spam.

**I need to change rates mid-session?** Cancel → Run with new rate. Within ~5
minutes new ads appear; old ones stay (no deletion).

**Token invalidated mid-run?** Script stops with CRITICAL error. Log into alt in
browser, complete any verification, get a new token, update secret, restart.

**GitHub outage?** Run fails; restart when GitHub is back. No runaway spam.

**Channel slowmode?** Detected and respected automatically.

**All channels say "our ad still latest, waiting"?** Normal in quiet periods —
your ad is visible at the bottom, no need to repost. That's the whole point.

**How long does the token last?** Days to weeks. Dies on logout, password change,
forced re-auth, or ban. If the script says "Token invalid", grab a fresh one.

**Main account risk?** Keep it separate (different email, don't run script on
it). Self-bot bans are usually isolated to the bot account.

## 🧪 Local test

```bash
pip install requests
python send_ads.py --self-test
```
Expect `🎉 ALL SELF-TESTS PASSED`.

---

## 🔧 v4.2 fixes (audit)

What changed since v4.1:

- **AFK keepalive bug fixed**: Previously, AFK breaks slept in 60-second chunks
  but reset the keepalive timer each call, so the 5-minute background ping
  never fired during AFK. Now a persistent `_KeepaliveSleep` tracks last_ping
  across calls.
- **429 infinite-loop fixed**: Old code never counted consecutive 429s, so a
  Cloudflare-style ban could loop `sleep + POST` forever. Now capped at 6
  consecutive 429s; `retry_after` capped at 10 minutes so a huge ban value
  doesn't freeze the job.
- **429 global cooldown respected across subsequent requests** (not just
  inside the single call that hit it).
- **5xx server errors now retried** with backoff (500/502/503 are transient).
- **`am_i_last` fails safe**: if fetching recent messages fails (network
  blip), returns `True` → skips that post instead of possibly double-posting.
- **Empty / non-200 message fetch returns `None` (sentinel)** instead of `[]`
  which was ambiguous.
- **Dead channel tracking**: channels returning 403/404 during warmup or
  mid-run get added to a skip-list so we don't hammer them every cycle.
- **Consecutive-error backoff per channel**: 3 failures → temporary skip
  (decays over time).
- **Distinguish token-death from channel 403**: previously a channel-level
  403 could be misread as a ban. Now re-validates the token before stopping.
- **Multi-line (BUY) emoji variations now prepended to the HEADER only**,
  not appended to the last line.
- **Per-cycle variations truly unique** within a cycle (`used_variations` set).
- **Missing browser anti-fingerprint headers added** (Sec-Ch-Ua, Sec-Fetch-*).
- **Chrome UA + build number bumped**; build number auto-refreshed from
  discord.com at startup (falls back to default if fetch fails).
- **Config validation**: clamps AFK break config so bad secrets can't crash
  `randint`; clamps `INTERVAL_MIN` to ≥1; validates `/users/@me` response.
- **Top-level exception handler** prints stats then re-raises so GitHub
  Actions marks the run failed.
- **Request timeout raised from 20s → 30s** (GitHub Actions network is slower).
- **Warmup startup delay moved before token validation** so it doesn't look
  like an instant POST on connect.
- **workflow**:
    - Moved message construction into `plan` job and passed as output (instead
      of duplicated per-chunk shell logic).
    - All inputs now passed through `env:` (not `${{ }}` inline in `run:`)
      preventing shell injection / quoting bugs from special characters
      (backticks, quotes, `$`, `;`).
    - Random heredoc delimiter so message body cannot close the heredoc.
    - Newlines/carriage returns stripped from one-line inputs.
    - Empty `IMAGE_PATH` handled explicitly.
    - `fail-fast: true` so if one chunk detects a ban, remaining chunks cancel
      instead of continuing to hammer.
    - `PYTHONUNBUFFERED=1` for realtime logs.
    - DISCORD_LOCALE / DISCORD_TIMEZONE secrets now actually wired through
      to the script (were missing in v4.1).
    - `run-name` guarded against empty `buy_rate`/`buy_rate_rap`.
