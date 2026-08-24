# 🔍 Audit Report — Discord Ad Sender v4.1 → v4.2

Comprehensive audit of `send_ads.py` (v4.1, 736 lines) and `send_ads.yml`
(workflow). Every issue found is listed with severity, location,
description, and the fix applied. **All High and Medium issues are
fixed in v4.2.**

---

## Summary

| Severity | Count | Status |
|---|---|---|
| **High** (failure / ban / leak / infinite loop) | 8 | ✅ Fixed |
| **Medium** (misbehavior / stealth loss / crash) | 12 | ✅ Fixed |
| **Low** (cosmetic / minor fingerprint) | 8 | ✅ Fixed |

---

## HIGH SEVERITY

### Issue #1 — Infinite loop on repeated 429s
**File:** `send_ads.py`, `api()`
**Bug:** The 429 branch did `time.sleep(wait); continue` without incrementing
against the retry cap, so persistent 429s (Cloudflare ban, per-IP limit)
looped forever — `sleep + POST` — until the 380-min GitHub timeout killed
the job.
**Fix:** Added `_429_streak` counter; after 6 consecutive 429s the function
returns the response to the caller (which then channels through the
error backoff / ban detection logic).

### Issue #2 — `retry_after` could be arbitrarily large (hours)
**File:** `send_ads.py`, `api()` 429 branch
**Bug:** Discord returns `retry_after` in seconds. Cloudflare bans can
return thousands of seconds, causing the chunk to sleep for hours.
**Fix:** Cap `wait = min(raw_wait, 600) + jitter` (10 min max). Combined
with the 429 streak cap (Issue #1), long-bans surface as errors instead
of dead sleep.

### Issue #3 — AFK keepalive NEVER fired (dead TCP for up to 30 min)
**File:** `send_ads.py`, `sleep_with_keepalive()` + AFK loop
**Bug:** The AFK loop called `sleep_with_keepalive(min(60, afk_left))`
repeatedly. Each call initialized `last_ping = time.time()`, then chunked
in 5s sleeps for 60s total — never reaching the 300s threshold. Zero
keepalives were sent during AFK breaks. A real Discord client never goes
30 minutes radio-silent.
**Fix:** Replaced with a `_KeepaliveSleep` class instantiated once in
`main()`, so `last_ping` persists across calls. Chunk size 30s, threshold
290s. Self-tested.

### Issue #4 — `am_i_last` returned False on failed/empty fetch → spam
**File:** `send_ads.py`, `am_i_last()`, `get_last_messages()`
**Bug:** v4.1: `return bool(msgs) and msgs[0].get("author",{}).get("id") == my_id`.
When Discord returned an empty list (403/404/500), `bool([]) == False`,
so the function returned False → "I'm not last" → bot POSTED. Repeated
network/permission failures caused the bot to spam-post to a broken
channel.
**Fix:** `get_last_messages()` returns `None` on failure (sentinel), a
`list` on success. `am_i_last()` returns `True` (safe: don't post) when
msgs is None or empty. One skipped cycle is invisible; spamming on a
failed fetch is not.

### Issue #5 — Channel-level 403/404 not distinguished from account ban
**File:** `send_ads.py`, main loop error handling
**Bug:** A channel-level 403 (archived, kicked, no perms) was logged but
the channel kept being retried every cycle — same for 404.
**Fix:** After 401/403 from POST, re-validate token. If token is dead →
stop (ban). If token is still alive → mark channel dead and remove from
rotation. 404 → mark dead immediately. Warmup channels that fail fetch
also marked dead before the posting loop starts.

### Issue #6 — Dead/errored channels retried forever
**File:** `send_ads.py`, main loop
**Bug:** v4.1 tracked `total_err` globally but never acted on per-channel
error streaks. One bad channel generated ❌ logs for the entire run.
**Fix:** Per-channel `channel_errors[cid]` counter. After 3 consecutive
errors the channel is temporarily skipped with decaying backoff. Combined
with Issue #5 for 403/404 (permanent removal).

### Issue #7 — `fail-fast: false` → ban cascaded across all 8 chunks
**File:** `send_ads.yml`, `send-chunk` strategy
**Bug:** With chunks running sequentially, if chunk 1 hit a ban and
failed, chunks 2–8 still queued up one after another — each immediately
hitting 401. That's 8 more login attempts on a freshly-banned token.
**Fix:** `fail-fast: true` so any chunk failure cancels all remaining
queued chunks instantly.

### Issue #8 — Global 429 not respected across subsequent requests
**File:** `send_ads.py`, `api()`
**Bug:** When Discord returned `"global": true`, v4.1 slept inside that
one call then returned normally. The very next request (different
endpoint/channel) would be globally rate-limited again — cascade.
**Fix:** Module-level `_global_cooldown_until` set on global 429. Every
`api()` call checks it via `_apply_global_cooldown()` before sending.

---

## MEDIUM SEVERITY

### Issue #9 — Shell injection / quoting bugs in workflow message build
**File:** `send_ads.yml`, Build message step
**Bug:** Inputs were interpolated directly into bash via
`${{ github.event.inputs.xxx }}` inside double-quoted strings — `"`,
backticks, `$()`, `;`, newlines could break MSG or inject commands.
Heredoc delimiter `MSG_EOF` was fixed (could collide with message body).
**Fix:** (a) Pass all inputs through `env:` (GitHub handles quoting).
(b) Added `clean()` stripping `\r`/`\n` from one-line inputs.
(c) `printf '%s'` with positional args — no shell expansion.
(d) Random 16-hex heredoc delimiter. (e) Empty/oversize validators.

### Issue #10 — BUY emoji suffixes landed on last line ("DM me quick 💸")
**File:** `send_ads.py`, `build_variations()` multiline branch
**Bug:** v4.1: `f"{e1} {base} {e2}"` placed `e2` after the last line,
producing `DM me quick 💸` on its own line. Comment said "prefix only"
but code added both.
**Fix:** Multiline branch splits into `header` + `rest`, prepends emojis
to header only. Self-test asserts `lines[-1].strip() == "DM me quick"`.

### Issue #11 — Missing modern browser headers (Sec-Ch-Ua, Sec-Fetch-*)
**File:** `send_ads.py`, session headers
**Bug:** Modern Chrome sends `Sec-Ch-Ua`, `Sec-Ch-Ua-Mobile`,
`Sec-Ch-Ua-Platform`, `Sec-Fetch-Dest/Mode/Site`. Their absence is a
real fingerprint, especially on datacenter IPs.
**Fix:** Added all six + `Cache-Control: no-cache` + `Pragma: no-cache`,
matching Chrome 128.

### Issue #12 — Outdated client build number (321520 from Aug 2024)
**File:** `send_ads.py`, `_SUPER_PROPERTIES`
**Bug:** Hardcoded build 321520 is ~2 years out of date. Discord updates
roughly weekly; stale `client_build_number` is a fingerprint.
**Fix:** Bumped default to 362220 + added `_fetch_build_number()` which
GETs discord.com at startup and scrapes `"buildNumber":(\d+)`. Falls
back to default on failure.

### Issue #13 — UA Chrome version 127 mismatched super-props
**File:** `send_ads.py`, `_UA` vs super-props
**Bug:** v4.1 sent `Chrome/127` in UA but matching version in super-props;
bumped to 128 together in v4.2. (Mismatched UA/Super-Props is a known
anti-bot signal.)

### Issue #14 — Same variation could post to multiple channels in a cycle
**File:** `send_ads.py`, main loop
**Bug:** v4.1 picked `msg = random.choice(variations)` per channel with
no tracking — duplicates within a cycle were possible.
**Fix:** Per-cycle `used_variations` set; channels pick from unused
variations.

### Issue #15 — Skip probability rolled per channel instead of per cycle
**File:** `send_ads.py`, main loop
**Bug:** `random.random() > random.uniform(0.85, 0.95)` per channel picked
a new threshold each time (avg 10% skip, but the "mood" shifted every
channel).
**Fix:** Pick one `post_threshold` per cycle, roll per channel against it.
One cycle = 95% (focused), next = 86% (lazy/AFK) — more human.

### Issue #16 — 5xx server errors not retried
**File:** `send_ads.py`, `api()`
**Bug:** Network errors got 3x exponential backoff, but 500/502/503/504
returned immediately as a ❌.
**Fix:** 5xx retried with `3*attempt + jitter` backoff, same as network
errors.

### Issue #17 — JSON parse errors could crash the script
**File:** `send_ads.py`, `validate_token()`, `get_channel_info()`,
`get_last_messages()`
**Bug:** Called `r.json()` without guarding against HTML (Cloudflare
challenge) or non-dict responses.
**Fix:** All JSON parses wrapped in try/except; non-JSON / non-list
responses treated as failures.

### Issue #18 — Startup delay happened AFTER token validation
**File:** `send_ads.py`, `main()`
**Bug:** First packet to Discord was the token-validation GET — instant —
then the bot waited 25-70s. Real users wait before any traffic.
**Fix:** Startup delay moved BEFORE `validate_token()`.

### Issue #19 — Workflow didn't pass DISCORD_LOCALE/TIMEZONE to script
**File:** `send_ads.yml`, env block
**Bug:** Script supported the vars; workflow never exported them — secrets
for those did nothing.
**Fix:** Added both to env block.

### Issue #20 — Bad AFK secret values (MIN>MAX) caused ValueError crash
**File:** `send_ads.py`, config + `plan_breaks()`
**Bug:** `randint(MIN, MAX)` throws if MIN>MAX; float bounds could invert.
**Fix:** Clamp all AFK configs on load (MAX ≥ MIN, positives). Planner
guards `n<=0` and short usable windows.

---

## LOW SEVERITY

### Issue #21 — `get_last_messages` failure was ambiguous with empty channel
**Fix:** `None` sentinel for failure; `[]` reserved for real empty lists.

### Issue #22 — Unhandled exception crashed without printing stats
**Fix:** Generic `Exception` handler prints stats then re-raises.

### Issue #23 — Request timeout 20s too short for GitHub Actions network
**Fix:** Raised to 30s.

### Issue #24 — `Content-Type` header strip was case-sensitive
**Fix:** `k.lower() != "content-type"`.

### Issue #25 — Stats combined smart-cooldown skips with random skips
**Fix:** Added per-channel `"cooldown"` counter; final stats show both.

### Issue #26 — Chunk gap off-by-one (120-299 instead of 120-300)
**Fix:** `$(( RANDOM % 181 + 120 ))`.

### Issue #27 — `run-name` showed 📷img for BUY runs
**Fix:** Added `ad_type == 'sell' &&` to condition.

### Issue #28 — Python stdout buffered in GitHub Actions (logs laggy)
**Fix:** `PYTHONUNBUFFERED: '1'` in env.

---

## Anti-detection posture (before → after)

| Surface | v4.1 | v4.2 |
|---|---|---|
| Browser headers | Incomplete, Chrome 127/build 321520 | Full set, Chrome 128, build # auto-refreshed |
| AFK keepalive | ❌ Broken (never fired) | ✅ Persistent, every ~5 min |
| 429 handling | Infinite loop, no cap | 6-streak cap, 10-min cap, global cooldown |
| 5xx handling | Immediate failure | Retried with backoff |
| Dead channels | Hammered all run | Auto-detected (403/404) → removed |
| Failed fetch | Posted anyway (spam) | Fails safe (skips) |
| Per-cycle variations | Could repeat | Used-set guarantees uniqueness |
| Skip threshold | Per-channel random | Per-cycle "mood" |
| BUY emoji | Suffix dangled on last line | Header-only |
| Chunk failure | All 8 chunks ran despite ban | fail-fast cancels siblings |
| Shell injection | Possible (interpolated inputs) | env-passed, sanitized, random delim |
| Config validation | MIN>MAX crashed | Clamped |
| First packet timing | Instant on startup | 25-70s delay first |
| Log flushing | Buffered | Unbuffered |

---

## Intentionally NOT changed (accepted risk / by design)

1. **No residential proxy.** Azure IPs are a known fingerprint; short
   sessions (6h) on an aged alt mitigate it. HTTP_PROXY support is a
   possible future enhancement.
2. **No WebSocket / real Rich Presence.** Gateway connections are a bigger
   self-bot fingerprint than they're worth. Custom HTTP status stays.
3. **No auto-delete** (per explicit user request).
4. **No self-chaining past 48h** (manual click required per user preference).
5. **Build number fetch is best-effort.** Default 362220 is already a
   major improvement over 321520.
6. **No DM/webhook alerts on ban.** User monitors via GitHub Actions logs
   and Discord mobile push on the alt.

---

## Verification

- ✅ `python3 send_ads.py --self-test` → ALL SELF-TESTS PASSED (11 checks)
- ✅ Workflow YAML parses cleanly
- ✅ BUY/SELL message construction tested; `$` preserved; shell injection
  attempts (backticks, `$(whoami)`, quotes, `;`) captured as literals
- ✅ Random heredoc delimiter works
- ✅ AFK planner: 1000 seeds → zero overlaps
- ✅ Keepalive: timer persists across calls
- ✅ `am_i_last`: None/[] → True (fail-safe)

---

## SECOND-PASS AUDIT (v4.2 post-fix review)

After writing v4.2, I did a second review pass. Found and fixed **5 more
issues** that the first pass missed:

### S-1 (High) — `Validate plan` step crashed with `set -u`
**File:** `send_ads.yml`, Validate plan step
**Bug:** The step ran `echo "Msg length: ${#MSG}"` with `set -euo pipefail`,
but `MSG` was a local variable from the previous step — not exported, not
a step output. Bash `nounset` would abort with "unbound variable", failing
every run before the bot even started.
**Fix:** Deleted the entire separate validate step — length/empty checks
already live inside the `Build message` step. Message construction has
been moved *into* `send-chunk` entirely (the `plan` job now calculates
only `chunks` — a tiny integer), which also eliminates:
- Cross-job multiline-message output edge cases (GitHub Actions multiline
  outputs can strip trailing newlines depending on runner version)
- The `IMAGE_FILENAME` secret being serialized through `job.outputs`
  (secrets are masked in logs but passing them as outputs was unnecessary
  exposure surface)

### S-2 (Medium) — `_fetch_build_number()` made a network call during `--self-test`
**File:** `send_ads.py`, module load
**Bug:** `CLIENT_BUILD = _fetch_build_number()` runs at module import,
before `if __name__ == "__main__"` — so even `--self-test` (which is
supposed to be offline) hit discord.com. If that network was blocked (e.g.
corporate firewall), self-test would hang 8s before running; if it was
intercepted, self-test wasn't truly offline.
**Fix:** Added `if _SELF_TEST: return _DEFAULT_BUILD` early-exit inside
`_fetch_build_number()`. Self-test is now 100% offline (and instant).

### S-3 (High) — Global cooldown recorded *capped* wait, causing 429 cascade
**File:** `send_ads.py`, `api()`
**Bug:** When Discord returned a global 429 with a large `retry_after`
(e.g. 3600s for a global ban), v4.2 correctly *slept* a capped 600s chunk
but then set `_global_cooldown_until = time.time() + wait` using that same
capped ~603s value. The next request 603s later thought the global ban
was over, sent another request, immediately got another 429, and repeated
the cycle — 6 loops to cover a single hour-long ban, wasting 6 POSTs and
looking very bot-like.
**Fix:** `_global_cooldown_until = time.time() + raw_wait` (uncapped full
server value). Each individual sleep is capped at 600s so logs/keepalive
still flow, but cross-call cooldown uses the real server-indicated time.
Verified: a 3600s global 429 now results in one 602s capped sleep + one
~3000s cooldown sleep before the next request succeeds.

### S-4 (High) — Mid-loop token recheck confused network errors with bans
**File:** `send_ads.py`, main loop 401/403 handling + `validate_token()`
**Bug:** v4.2 changed `validate_token()` to return user-or-None. When a
POST returned 401/403 the code re-validated: if recheck returned None, it
stopped the run as "banned". But a transient network error (DNS timeout,
connection reset) ALSO returned None, which would cause the bot to abort
an otherwise healthy run and misreport a ban.
**Fix:** `validate_token()` now returns a tuple `(user_dict, reason)`
with `reason ∈ {"invalid","network","server","unknown"}`. The mid-loop
handler only stops the run on `reason == "invalid"`. Network/server
glitches fall through to the normal per-channel error backoff. Verified
with mock tests.

### S-5 (Low) — `used_variations` was consumed even on failed POST
**File:** `send_ads.py`, main loop
**Bug:** `used_variations.add(msg)` was called *before* `send_message()`,
so a failed send still "used up" that variation. Over many error cycles
the set could exhaust all 40+ variations, falling back to duplicates.
**Fix:** Moved `used_variations.add(msg)` inside the `if ok:` branch.
Only successfully-sent variations are marked used in the cycle.

---

## Second-pass verification

- ✅ Self-test runs offline (no network calls) — CLIENT_BUILD stays at
  default 362220 during `--self-test`
- ✅ Global 429 cap/chunk logic tested with mock: 3600s ban → ~602s capped
  sleep + ~3000s cooldown sleep before next 200 response
- ✅ `validate_token()` returns correct reasons for 200/401/network/5xx
- ✅ Mid-loop ban detection only aborts on hard 401/403 recheck
- ✅ Workflow YAML parses; plan job only outputs `chunks` (integer);
  message construction + IMAGE_FILENAME handling live inside send-chunk
- ✅ `pip install` pinned to `requests>=2.32,<3`
- ✅ Final self-test: 11/11 checks pass

---

## THIRD-PASS (final zero-tolerance) AUDIT — 7 more issues

### Z-1 (Medium) — Typing duration not applied when typing indicator fails
**File:** `send_ads.py`, `send_typing()`
**Bug:** Old code only slept the typing duration when the /typing endpoint
returned 204. If the request failed (network blip, rate limit, timeout),
`send_typing()` returned immediately and `send_message()` went straight
to POSTing the message — zero time between reading the channel and
sending. A real human types for a few seconds regardless of whether the
indicator was acknowledged by the server.
**Fix:** (a) Added a pre-typing "thinking" pause of 0.8–2.2s (glancing at
the channel before starting to type — humans don't instantly start typing
after reading the latest message). (b) Always sleep the typing duration
after attempting the typing indicator, even on failure. (c) Capped max
typing duration at 9s since Discord typing indicators naturally expire
after ~10s anyway.

### Z-2 (High) — Mid-run ban detection used `return` instead of `sys.exit()`
**File:** `send_ads.py`, main loop error handling
**Bug:** When a mid-loop token recheck detected a ban, the code did `_print_stats(); return`. `return` exits the `main()` function and falls
through to normal process exit with code 0. GitHub Actions then marks
that chunk as ✅ success, so (a) `fail-fast: true` never triggers, and
(b) subsequent chunks still queue up and start, hammering a banned token.
The same issue affected the KeyboardInterrupt handler (exited 0).
**Fix:** (a) Ban detection now calls `sys.exit(2)` — non-zero exit so GHA
marks the chunk ❌ failed and fail-fast cancels siblings. (b)
KeyboardInterrupt exits with 130 (standard SIGINT code). (c) Added
`except SystemExit: raise` before the generic Exception handler so
`sys.exit()` isn't caught and logged as an "unhandled error".
(d) Normal end-of-run explicitly calls `sys.exit(0)`.

### Z-3 (Low/Stealth) — Warmup phase fired back-to-back API calls
**File:** `send_ads.py`, warmup loop
**Bug:** `get_channel_info(cid)` was immediately followed by `read_channel(cid)` (which is `get_last_messages(cid, 10)`) — two GETs to the
same channel back-to-back within milliseconds. Real clients have a small
render gap between loading channel metadata and fetching messages.
**Fix:** Inserted a 0.4–1.0s random sleep between the two. Also bumped the
failure-case sleep to 1.5–3.0s (was 1–2s) and renamed to `sleep_chunked`
to respect `run_end` (which doesn't matter in warmup but is consistent).

### Z-4 (Low) — Multiline emoji random loop could duplicate deterministic prefixes
**File:** `send_ads.py`, `build_variations()` multiline branch
**Bug:** The 6-iteration loop used `random.choice(_EMOJIS)` which could
pick the same emoji multiple times or collide with emojis already added
by the deterministic prefix loop (e.g. "⚡ "), producing redundant
variations and slightly lower unique count than intended.
**Fix:** Replaced with `random.sample(_EMOJIS, k=min(6, len(_EMOJIS)))` so
all random prefixes are unique and not already in the deterministic set.

### Z-5 (Low) — Build-number fetch used generic Mozilla/5.0 UA, not Chrome
**File:** `send_ads.py`, `_fetch_build_number()`
**Bug:** The pre-auth app-shell fetch used a generic `"Mozilla/5.0"` UA
while all subsequent requests used the full Chrome 128 UA. Discord/Cloudflare
sees two different UAs from the same IP within seconds.
**Fix:** Moved `_UA` constant definition above `_fetch_build_number()` and
used it for the build-number request so the pre-auth and post-auth traffic
look like the same browser session.

### Z-6 (Low) — Workflow: openssl fallback for heredoc delimiter
**File:** `send_ads.yml`, build step
**Bug:** `DELIM="EOF_$(openssl rand -hex 8)"` would fail if openssl was
unavailable (unlikely on ubuntu-latest but possible in hardened runners).
**Fix:** Added `2>/dev/null || echo "9f8e7d6c5b4a3210"` fallback so the
build step can never fail because of missing openssl. The hardcoded
fallback is fine — the only failure mode is a potential collision, which
is effectively impossible for a 16-hex static value with normal content.

### Z-7 (Verified NOT a bug) — BUY message multiline printf and GITHUB_OUTPUT
**Conclusion after live testing:** The `printf '...\n...'` with single-quoted
format string correctly produces literal newlines (7 lines, 75 chars).
The heredoc-style multiline GITHUB_OUTPUT (`msg<<EOF_...`) preserves all
lines including blank lines, and GitHub Actions correctly passes the
multiline value to `env: MESSAGE: ${{ steps.build.outputs.msg }}`
(verified by simulating GHA's parser). No fix needed.

### Z-8 (Verified NOT a bug) — fail-fast: true is correct
**Conclusion:** With `max-parallel: 1`, only one chunk runs at a time;
fail-fast: true tells GHA to cancel all *queued* (not-yet-started) chunks
when the running one fails — exactly what we want for ban detection.

### Final counts across all three passes

| Pass | High | Medium | Low |
|---|---|---|---|
| 1 | 8 | 12 | 8 |
| 2 | 3 | 1 | 1 |
| 3 | 1 | 1 | 4 |
| **Total** | **12** | **14** | **13** = **39 issues fixed** |
