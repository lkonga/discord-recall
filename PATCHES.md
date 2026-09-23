# Fork notes: DeepSeek-ready LLM + channel x date-range digests

Two things were missing for the requested workflow ("pick channels, pick a
date range, script it, use DeepSeek"):

1. The stock CLI digests **one channel on one day** (`digest --date`), so any
   range required an external loop. There was no `--from/--to`.
2. The LLM client was hardwired to OpenRouter's URL, so pointing it at
   DeepSeek needed a code edit.

Both are fixed here, plus two upstream bugs found while verifying.

## 1. Any OpenAI-compatible LLM endpoint (DeepSeek, OmniRoute, vLLM, ...)

`src/discord_recall/config.py` gains `llm_base_url`, `llm_model`, `llm_api_key`.
`src/discord_recall/llm.py` resolves the endpoint from those settings and falls
back to the original OpenRouter config when `LLM_BASE_URL` is empty, so existing
setups keep working unchanged.

```bash
# DeepSeek direct
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat          # or deepseek-reasoner
LLM_API_KEY=sk-...

# local Ollama, vLLM, llama.cpp, any gateway
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=deepseek-r1:14b
LLM_API_KEY=not-needed
```

The base URL is normalized (`/v1`, with or without `/chat/completions`, trailing
slashes), transport/5xx/429/empty-completion failures are retried three times
with backoff, and permanent errors (401/402/404/422) fail fast instead of
burning retries. Responses are decoded defensively (string content, list
content, `reasoning_content`-only).

## 2. `digest-range`: channels x date range in one command

New module `src/discord_recall/digest/range.py` and CLI command
`discord-recall digest-range`:

```bash
# two channels, 14 days, daily digests
discord-recall digest-range -c 111 -c 222 --from 2026-09-01 --to 2026-09-14

# whole server, weekly, from June onward
discord-recall digest-range --server 999 --period weekly --from 2026-06-01

# cron-friendly rolling window
discord-recall digest-range -c 111 -c 222 --last 7 --send
```

Options: `--channel` (repeatable) / `--server`, `--from`, `--to`, `--last N`,
`--period daily|weekly|monthly`, `--print`, `--send`, `--dry-run`, `--force`.

Behaviour:

* Periods are aligned exactly like the upstream backfill: daily = UTC calendar
  day, weekly = Monday-anchored, monthly = 1st of month.
* Idempotent by `(channel, period, period_start)`: existing digests are reused
  (no LLM call), `--force` deletes and rebuilds just that cell.
* Per-cell failure isolation: one bad cell is reported and the run continues;
  the command exits non-zero if anything failed.
* `--dry-run` lists channels, period labels and message counts with no LLM
  calls, so you can pick the range before spending tokens.

`scripts/digest-range.sh` wraps it for cron, reading defaults from
`RECALL_CHANNELS`, `RECALL_SERVER`, `RECALL_FROM`, `RECALL_TO`, `RECALL_LAST`,
`RECALL_PERIOD` when the matching flag is absent:

```cron
5 7 * * * cd /path/to/discord-recall && RECALL_CHANNELS=111,222 ./scripts/digest-range.sh --last 1 --send >> logs/digest.log 2>&1
```

## 3. Upstream bug: enums never bound to their String columns

`DigestPeriod`, `BackfillStatus` and `WikiPageType` were plain `enum.Enum`
classes mapped onto `String` columns.

* `digest`/`digest-backfill` crashed outright:
  `sqlite3.ProgrammingError: Error binding parameter 2: type 'DigestPeriod' is not supported`
  (confirmed on SQLite; the same bind would fail on Postgres).
* `backfill` resume never worked: `state.status == BackfillStatus.complete`
  compared a stored `str` against a plain Enum and was always `False`, so
  completed channels were re-walked instead of skipped.

Fix: the three enums now mix in `str` (`class DigestPeriod(str, enum.Enum)`),
so they bind as text and compare equal to stored strings. No schema change, no
data migration.

## 4. Offline verification harness

`scripts/seed_demo_data.py` creates a demo server, three channels (one
deliberately quiet), authors and 100+ messages across a date range, so the
digest path can be tested without a Discord token:

```bash
uv run alembic upgrade head
uv run python scripts/seed_demo_data.py --days 14 --end 2026-09-20
uv run discord-recall digest-range -c <id> --from 2026-09-18 --to 2026-09-20 --dry-run
```

`tests/test_range_and_llm.py` covers period math, endpoint resolution, and a
full `run_range` integration over a throwaway SQLite DB with a stubbed LLM
(build, reuse, force, empty day, dry-run, error cases, and both regressions):

```bash
uv sync --extra dev
uv run python -m pytest tests/ -q
```

## 5. Bounded backfill (`--since/--until/--max-messages`)

The stock `backfill` walks a channel's entire history, which is unusable on an
active channel (Cursor `#general` runs ~220 msgs/day, so a full walk is 100k+
messages and thousands of requests). The capture side now takes a window:

```bash
# last few days only, hard cap, gentle cadence
uv run discord-recall backfill -c <channel_id> --since 2026-09-14 \
    --max-messages 400 --batch-size 100 --delay 1.2

# everything up to a date
uv run discord-recall backfill -c <channel_id> --until 2026-09-20

# whole server, per-channel cap
uv run discord-recall backfill -s <server_id> --since 2026-09-01 --max-messages 500
```

Semantics:

* `--since` and `--until` are both inclusive (`window_bounds` turns `--until`
  into an exclusive `before` bound at the next midnight UTC).
* `--max-messages` is a hard cap per channel (per channel for `--server` runs).
* `--batch-size` / `--delay` override the config defaults for one run.
* Windowed runs are **stateless**: they neither read nor write
  `channel_backfill_state`, so a bounded catch-up can never mark a channel
  "complete" or make the next unbounded run skip what it still owes.
  Unbounded runs keep resume/complete behaviour exactly as before.

`tests/test_backfill_window.py` covers the window math, the cap, and the
"state untouched" guarantee with stubs (no Discord, no DB).

## Verified

* Live capture against the real Discord API with a self-bot token: 328 messages
  from XMG & Friends `#general` (`--since 2026-09-14 --max-messages 400`, 21
  authors) and 1200 from Cursor `#general` (`--since 2026-09-16
  --max-messages 1200`, 92 authors), 4 requests / ~7s and 12 requests / ~30s,
  bounded runs leaving `channel_backfill_state` untouched.
* Real DeepSeek digests over that captured data, e.g. Cursor `#general`
  2026-09-17 with 716 messages chunked into 2 passes + synthesis, and a
  grounded `ask` answer citing captured messages with dates.
* Real DeepSeek summaries produced end-to-end (`deepseek/deepseek-v4-flash` via
  an OpenAI-compatible gateway): single-day `digest`, 2 channels x 3 days
  `digest-range` (5 built, 1 reused), weekly + monthly periods, server-wide
  runs, `--print`, `ask` grounded in stored digests.
* Idempotency: re-running the same range = `0 built, 6 reused`, no LLM calls.
* `--force` replaces the row in place (no duplicate rows).
* Failure path: `LLM_BASE_URL=https://api.deepseek.com` with a key that has no
  balance produced `HTTP 402 Insufficient Balance`, zero retries logged, per-cell
  `FAILED`, exit code 1.
* CLI error paths exit 1: reversed range, unknown period, unknown channel, no
  channel selection, `--last` together with `--from`.
* `uv run python -m pytest tests/ -q` -> 17 passed.

Not verified: `listen` (real-time capture) was not exercised, only `backfill`.
The token used is a self-bot user token, so Discord ToS risk applies.

## 6. Consuming digests (web UI) and deployment on g5kc

Upstream ships no UI (`GUI.md` is a design spec for one, and `src/` has no web
dependency). Added a read-only FastAPI UI plus a containerised deployment.

### UI

```bash
uv sync --extra web
uv run discord-recall-web            # http://127.0.0.1:9879
```

Routes: `/` (channels + digest counts + ask box), `/channel/<id>` (every digest
for that channel, newest first), `POST /generate/<id>` (build one day's digest
from a date input), `POST /ask` (grounded Q&A over stored digests/messages),
`/healthz`. No authentication: it is meant to sit behind loopback +
`tailscale serve`.

### Deployment (g5kc, Dokploy)

| Item | Value |
|---|---|
| Source | `github.com/lkonga/discord-recall`, branch `main` |
| Dokploy project / app | `discord-recall` / `discord-recall-dffwka` |
| composeId | `hwSpDcOHts8-cq3Ph-RXU` |
| Compose path | `docker-compose.dokploy.yml` |
| Container | `discord-recall-dffwka-recall-1` (healthy) |
| Store | docker volume `discord-recall-dffwka_recall-data` -> `/data` |
| UI | `https://g5kc.tail1e9037.ts.net:9879/` (tailnet only) |

Design notes:

* **Host networking.** OmniRoute listens on `127.0.0.1:20129` only, so a bridge
  container cannot reach it. `network_mode: host` fixes that and keeps
  MagicDNS working. `WEB_HOST=127.0.0.1` then pins the UI to loopback so host
  networking does not publish it.
* **Exposure.** `tailscale serve --bg --https=9879 http://127.0.0.1:9879`.
  No Dokploy Traefik domain is attached, so there is no public URL.
* **Secrets** live in Dokploy's compose env (`DISCORD_TOKEN`, `LLM_BASE_URL`,
  `LLM_MODEL`, `LLM_API_KEY`) and are injected at runtime; the public repo
  contains none.
* **Scheduler** (`docker/scheduler.py`) runs every `SCHEDULE_INTERVAL_SECONDS`
  (default 6h): per configured channel a bounded backfill
  (`BACKFILL_DAYS`, `MAX_MESSAGES`) then `digest-range` over the last
  `DIGEST_DAYS`, with `--send` when `SEND_TELEGRAM=true`. Each step is a fresh
  CLI process, so a failure cannot wedge the loop.

Redeploy after a change:

```bash
git push deploy deepseek-digest-range:main
curl -sk -X POST https://g5kc.tail1e9037.ts.net:13900/api/compose.deploy \
  -H "x-api-key: $DOKPLOY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"composeId":"hwSpDcOHts8-cq3Ph-RXU"}'
```

Back up / restore the store:

```bash
docker run --rm -v discord-recall-dffwka_recall-data:/data -v "$PWD":/out alpine \
  tar czf /out/recall-store.tgz -C /data .
docker run --rm -v discord-recall-dffwka_recall-data:/data -v "$PWD":/out alpine \
  tar xzf /out/recall-store.tgz -C /data
```

Timeshift snapshots on g5kc need an interactive sudo password:

```bash
sudo timeshift --create --comments "discord-recall pre-deploy"
sudo timeshift --create --comments "discord-recall post-deploy"
```

## 7. Pacing policy for the self-bot token (researched)

Capture runs through the background queue (`src/discord_recall/web/jobs.py`), one
Discord-touching job at a time. Every row below carries the **verbatim** snippet
that justifies the shipped number, plus a verdict:

* **QUOTED** - the value is backed by a line quoted from the repo named in the row.
* **MARGIN** - no upstream number exists; it is a conservative engineering choice
  with the reasoning stated, deliberately *not* claimed as policy.

Sources (paths re-verified 2026-09-24): `dolfies/discord.py-self` (`discord/http.py`,
`discord/abc.py`), `Tyrrrz/DiscordChatExporter`
(`DiscordChatExporter.Core/Discord/DiscordClient.cs`, `DiscordChatExporter.Core/Utils/Http.cs`,
`DiscordChatExporter.Cli/Commands/Base/ExportCommandBase.cs`), `victornpb/undiscord`
(`src/undiscord-core.js`, `src/ui/undiscord.html`, issues #168 and #245 + the gist
linked from #168), `discord/discord-api-docs` (`developers/topics/rate-limits.mdx`,
`developers/resources/message.mdx`) and `discord-userdoccers/discord-userdoccers`
(`pages/topics/rate-limits.mdx`, branch `master`).

Two citation corrections from that pass: the api-docs rate-limit page **moved** to
`developers/topics/rate-limits.mdx` (old `docs/topics/Rate_Limits.md` 404s), and
undiscord issue **#414 does not exist** (`gh api repos/victornpb/undiscord/issues/414`
-> 404; #168 and #245 resolve).

| Parameter | Value we ship | Verdict | Verbatim evidence (snippet - repo - file - symbol) |
|---|---|---|---|
| Page size | `limit=100` (`--batch-size 100`) | **QUOTED** | `retrieve = 100 if limit is None else min(limit, 100)` - py-self - `discord/abc.py` - `Messageable.history` (L2427, with `if count < 100:` at L2443). DCE: `.SetQueryParameter("limit", "100")` - `DiscordChatExporter.Core/Discord/DiscordClient.cs` - `GetMessagesAsync` (L712; also L232 guilds, L855 reactions). **Disagrees with a quoted value:** the documented *default* is 50, not 100 - api-docs - `developers/resources/message.mdx` - Get Channel Messages: "Max number of messages to return (1-100)", default `50`. We sit at the documented **max**; undiscord sends no `limit` at all (offset paging, 25/page assumed in `calcEtr`: `Math.round(this.state.grandTotal / 25)`) |
| Base delay | 2.5 s (`PACE_DELAY`) | **MARGIN** | No upstream sentence exists. Nearest shipped values: `<input id="searchDelay" type="range" value="30000" step="100" min="100" max="60000">` and `<input id="deleteDelay" type="range" value="1000" step="50" min="50" max="10000">` - undiscord - `src/ui/undiscord.html` - delay sliders. The #168 gist uses `let deleteDelay = 1100;` / `let minDeleteDelay = 900;` / `let maxDeleteDelay = 2200;` / `let searchDelay = 1200;` - gist `12b8a72abf6bf17fe3b9e75d19d60743`. **Previous claim withdrawn:** "Undiscord hand-tested floor ~2100 ms" matches no upstream number; the shipped defaults are 30 000 ms search / 1000 ms delete |
| Jitter | +0 to +1.5 s uniform, never below base (`PACE_JITTER_EXTRA`) | **MARGIN** | No shipped client jitters (`Math.random` appears nowhere in undiscord's source; none in py-self or DCE). Community code only: `deleteDelay = Math.floor(Math.random() * (maxDeleteDelay - minDeleteDelay + 1) ) + minDeleteDelay;` (span 900-2200 ms) - gist `12b8a72abf6bf17fe3b9e75d19d60743` - `deleteMessages`; and `setTimeout(done, ms + Math.floor(Math.random() * 200) )`, later raised to `* 700` - undiscord - issue #245 body. **Disagrees with the quoted range:** our added span is 0-1500 ms on a 2500 ms base, i.e. wider and higher than the gist's 900-2200 ms absolute range |
| Cap per run | 1000 messages (`PACE_MAX_MESSAGES`) | **MARGIN** | No upstream run cap in any client. DCE's only count limit is per output file: `public override bool IsReached(long messagesWritten, long bytesWritten) => messagesWritten >= limit;` - DCE - `Exporting/Partitioning/MessageCountPartitionLimit.cs` - `MessageCountPartitionLimit` (driven by `-p`); its docs only describe file splitting (`.docs/Using-the-CLI.md`: "You can use partitioning to split files after a given number of messages or file size.") |
| Max requests | ≤30/run, ≤60/h, ≥10 min between automated runs (`PACE_MAX_REQUESTS`) | **MARGIN** | No upstream request budget exists. The nearest upstream ceilings are attempt ceilings, not budgets: `for tries in range(5):` - py-self - `discord/http.py` - `HTTPClient.request` (L928); `MaxRetryAttempts = 4` and `MaxRetryAttempts = 8` - DCE - `Utils/Http.cs` - `ResiliencePipeline` / `ResponseResiliencePipeline`. The hourly and inter-run clauses are additionally **not implemented** (see audit list below, items 1-2) |
| On 429 | wait `retry_after + 1 s` buffer, doubling per consecutive strike (`COOLDOWN_429`) | **QUOTED** | Buffer: `// Add some buffer just in case` / `retryAfter + TimeSpan.FromSeconds(1)` - DCE - `Utils/Http.cs` - `Http.ResponseResiliencePipeline` `DelayGenerator`. Doubling: `log.verb(\`Cooling down for ${w * 2}ms before retrying...\`);` / `await wait(w * 2);` - undiscord - `src/undiscord-core.js` - `search()`. **Disagrees with a quoted value:** ours doubles per *consecutive strike* (60/120/240 s), undiscord doubles the single wait; and our 900 s clamp breaks the `retry_after + 1` floor (characterized at `tests/test_pacing.py:199-205`, `assert harness.sleeps[0] < retry_after + 1  # documented floor does not hold`) |
| Consecutive 429s | **hard stop after 3** (`PACE_MAX_429_STRIKES`) | **MARGIN** | No upstream 3-strike rule. py-self's ceiling is 5 attempts: `for tries in range(5):` - py-self - `discord/http.py` - `HTTPClient.request` (with `# We've run out of retries, raise` at L1143). DCE: `MaxRetryAttempts = 4` / `= 8` - `Utils/Http.cs`. undiscord delete: `maxAttempt: 2,` + `while (attempt < this.options.maxAttempt)`; undiscord search has no ceiling at all: `await wait(w * 2); return await this.search();`. **Previous claim withdrawn:** "py-self stops sleeping and raises instead of retrying forever" is wrong in both directions (it raises after 5 *attempts*, and undiscord's search path never stops). **Off by one:** the stop fires on the fourth line (`if cooldowns > MAX_429_STRIKES`, `jobs.py:273`), and the job row even prints `{cooldowns - 1} consecutive rate limits` |
| 429 without `Retry-After` | abort the run, do not retry | **QUOTED** | "If no \`Retry-After\` header is present, you should not programatically retry the request." - discord-userdoccers - `pages/topics/rate-limits.mdx` (L139), preceded by "Sometimes, these limits may return a `Retry-After` header. In these cases, you should respect the header and not make further requests until the time has elapsed." **Scope caveat:** that sentence is in the *user* docs only; api-docs has no equivalent and says only "Your application should rely on the \`Retry-After\` header or \`retry_after\` field to determine when to retry the request." |
| Global / Cloudflare 429 | abort run, pause 15 min (`GLOBAL_COOLDOWN`) | **QUOTED (detection) / MARGIN (duration)** | `if is_global:` / `self._global_over.clear()` and the per-request gate `if not self._global_over.is_set(): await self._global_over.wait()` - py-self - `discord/http.py` - `HTTPClient.request`; Cloudflare: `is_cloudflare = not response.headers.get('Via')` and, with no `Retry-After`, `raise HTTPException(response, f'Cloudflare ban (code: {code})')` (`_CLOUDFLARE_REGEX = re.compile(r'<span>(\d{3,4})</span>')`). The 900 s figure is ours. Note py-self reads the JSON body field `is_global: bool = data.get('global', False)`, **not** the documented `X-RateLimit-Global` / `X-RateLimit-Scope: global` headers |
| After any 429 | 10 min pause before the next job (`POST_429_PAUSE`) | **MARGIN** | No upstream number. Implementation: `await asyncio.sleep(POST_429_PAUSE if limited else _jittered(3))` - `src/discord_recall/web/jobs.py:400` - `worker_loop`. Characterized as compounding with the strike-out pause at `tests/test_pacing.py:233-237` ("the effective strike-out pause is ~25 min") |
| 401 | stop immediately, no retry | **QUOTED** | `if exc.status == 401:` / `raise LoginFailure('Improper token has been passed') from exc` - py-self - `discord/http.py` - `HTTPClient.static_login` (L1258). DCE: `HttpStatusCode.Unauthorized => throw new DiscordChatExporterException("Authentication token is invalid.", true)` - `DiscordClient.cs` (the `true` is fatal, so it escapes `catch (DiscordChatExporterException ex) when (!ex.IsFatal)` in `ExportCommandBase.cs`). Docs: `**401** responses are avoided by providing a valid token in the authorization header when required and by stopping further requests after a token becomes invalid` - api-docs / userdoccers |
| 403 | fail that channel's run, no retry | **QUOTED** | `if response.status_code == 403:` / `raise Forbidden(response, data)` - py-self - `discord/http.py` - `HTTPClient.request` (L1100). DCE: `HttpStatusCode.Forbidden => throw new DiscordChatExporterException($"Request to '{url}' failed: forbidden.")` - `DiscordClient.cs` (no `true`, so **non-fatal**: `catch (DiscordChatExporterException ex) when (!ex.IsFatal)` records `errorsByChannel[channel] = ex.Message;` and the other channels continue). **Disagrees with a quoted value:** DCE skips the channel and continues the export; we abort that channel's run (equivalent only because our jobs are single-channel) |
| 5xx | ≤2 retries (handled inside py-self), then abort | **QUOTED (formula) / MISMATCH (count)** | `if response.status_code in {502, 504, 507, 522, 523, 524}:` / `failed += 1` / `await asyncio.sleep(1 + tries * 2)` - py-self - `discord/http.py` - `HTTPClient.request` (L1094-1097; 500 itself is *not* in the set and is raised as `DiscordServerError`; the upload path is wider: `{500, 502, 504, 507, 522, 523, 524}`). DCE: `(int)statusCode >= 500` retryable with `TimeSpan.FromSeconds(Math.Pow(2, args.AttemptNumber) + 1)` - `Utils/Http.cs`. **Disagrees with a quoted value:** py-self allows up to 4 retries inside `range(5)` and DCE allows 8; "≤2 retries" matches neither |
| Parallel channels | never more than 1 | **QUOTED** | `[CommandOption("parallel", Description = "Limits how many channels can be exported in parallel.")]` / `public int ParallelLimit { get; set; } = 1;` with `MaxDegreeOfParallelism = Math.Max(1, ParallelLimit),` - DCE - `Cli/Commands/Base/ExportCommandBase.cs` - `ExportCommandBase`. py-self: `async with ratelimit:` per request with `# Only a single rate limit object should be sleeping at a time.` - `discord/http.py` - `HTTPClient.request` / `Ratelimit`. **Disagrees with a quoted value:** py-self keeps one `Ratelimit` per bucket hash + major params, so it does **not** serialise different channels; our stricter stance is a choice, not a copy |

Invalid-request budget: `Currently, this limit is **10,000 per 10 minutes**. An
invalid request is one that results in **401**, **403**, or **429** statuses.`
(api-docs) and `... **10,000 per 10 minutes** and leads to a **24 hour ban**.`
(userdoccers - the 24 h duration is user-docs only). Both docs also exempt
`X-RateLimit-Scope: shared` responses from the count.

The judgement that follows is ours, not documented: at ~10 requests per run this
budget is not the binding constraint, so per-route 429s and behavioural detection
matter more than delay tuning. `PACE_*` env vars override every value above.

Known trade-off: windowed captures are stateless by design (they never touch
`channel_backfill_state`), so a repeated cron re-reads the head of the window
instead of resuming a cursor. With a 2-day window and a 1000-message cap that is
a handful of requests, and it keeps the "bounded run cannot corrupt resume
state" guarantee.

### What the audit found still missing

Gaps implied by the table above that the current tree does not yet implement
(grepped 2026-09-24 over `src/discord_recall/`, `docker/scheduler.py`, `tests/`;
the tree had uncommitted changes in `docker/scheduler.py` and `web/*` plus an
untracked `tests/test_scheduler_queue.py` at that moment).

1. **≤60 requests/hour counter - OPEN.** No hourly request accounting exists:
   `src/discord_recall/web/jobs.py:40` still holds only the per-run budget
   (`PACE_MAX_REQUESTS = int(os.environ.get("PACE_MAX_REQUESTS", "30"))`), and
   `docker/scheduler.py:34`
   (`INTERVAL = int(os.environ.get("SCHEDULE_INTERVAL_SECONDS", "21600"))  # 6h default`)
   is wall-clock spacing, not a request counter.
2. **≥10 min between automated runs - OPEN.**
   `src/discord_recall/web/jobs.py:400`
   (`await asyncio.sleep(POST_429_PAUSE if limited else _jittered(3))`) starts the
   next job ~3 s after a clean run; the only ≥10 min gate is `POST_429_PAUSE`
   (`jobs.py:44`, 600 s) and it applies only when the previous run saw a 429.
3. **403 drop-channel on the single-channel capture path - FIXED in the CLI, OPEN
   in the queue.** Fixed: `src/discord_recall/capture/backfill.py:213-224`
   (`except discord.Forbidden` -> `raise ChannelForbidden(...)`) caught at
   `backfill.py:324-328` for both the single-channel (`backfill.py:312-316`) and
   server paths, exiting with `self._exit_code = 3`. Open: the queue collapses
   that distinct code into a generic error -
   `jobs.py:370` `status=(JobStatus.done.value if rc == 0 else JobStatus.error.value)` -
   so nothing marks the channel *dropped* and the next cycle resubmits it.
4. **Jitter on the scheduler/UI paths - FIXED for the queue path, OPEN for the
   legacy UI.** Queue: `jobs.py:152` + `:163-164` (capture) and `:189` (discover)
   route through `_jittered`; the scheduler only POSTs to `/api/jobs`
   (`docker/scheduler.py:6-12`). Open: the server-rendered handlers bypass it -
   `src/discord_recall/web/app.py:318-325` builds
   `args = ["backfill", "-c", str(channel_id), "--max-messages", str(max_messages)]`
   with **no `--delay`**, falling back to the fixed
   `backfill_delay_seconds: float = 1.0` (`src/discord_recall/config.py:35`).
5. **Parent-side enforcement of the 1000-message cap - FIXED.**
   `jobs.py:154`
   (`cap = min(int(payload.get("maxMessages") or PACE_MAX_MESSAGES), PACE_MAX_MESSAGES)`),
   pinned by `tests/test_pacing.py:124-141`. Caveat: the legacy path
   (`web/app.py:319`, `max_messages: int = Form(600)` at `:333`) and the API's
   accepted field (`web/api.py:29`, `maxMessages: int = 600`) are clamped only once
   the job reaches `build_args`; the legacy handler does not clamp at all.
6. **Real request counting instead of messages/page - OPEN.**
   `jobs.py:234` is still an estimate from the progress line:
   `requests_est = fetched // max(1, PACE_BATCH) + 1`, updated only when
   `_MESSAGES_RE` matches, so short pages, in-flight retries and 202 "not indexed"
   responses are invisible.
7. **The 3-strike stop being reachable - FIXED, and pinned by a test.**
   `tests/test_pacing.py:213-230` (`test_four_consecutive_rate_limits_abort_the_run`)
   drives four canned `429` lines through `jobs._run_process` and asserts
   `pytest.raises(jobs.RateLimitAbort)` with
   `harness.sleeps == [jobs.COOLDOWN_429, jobs.COOLDOWN_429 * 2, jobs.COOLDOWN_429 * 4, jobs.GLOBAL_COOLDOWN]`;
   the trigger is `jobs.py:273` `if cooldowns > MAX_429_STRIKES or global_block:`.
   Still carries the off-by-one from row 7 (fourth line, not third) and the extra
   `GLOBAL_COOLDOWN` sleep characterized at `tests/test_pacing.py:233-237`.
8. **Matching py-self's exact "Global rate limit has been hit." wording - OPEN.**
   `jobs.py:270` is still a loose pattern:
   `global_block = bool(re.search(r"global|cloudflare|ban", line, re.I))`, which
   also fires on unrelated text (any channel name containing "global", or the
   substring "ban"), instead of matching py-self's actual strings
   (`'Global rate limit has been hit. Retrying in %.2f seconds.'`,
   `'Cloudflare rate limit has been hit. Retrying in %.2f seconds.'`,
   `'Cloudflare ban (code: %s)'`).
9. **Serialising the scheduler against the queue - FIXED.** `docker/scheduler.py:6-12`
   ("The loop is **enqueue-only**: it POSTs jobs to the app's own API
   (`POST /api/jobs`, exactly like the UI does) and never touches Discord itself
   ... shelling out would bypass the pacing and could open a second Discord session
   alongside a job the queue is already running"), the enqueue-only
   `cycle()`/`enqueue()` at `scheduler.py:126-198`, and the regression test
   `tests/test_scheduler_queue.py:75-80` (`test_module_cannot_run_the_cli_itself`
   asserts no `subprocess`/`Popen` in the module).

The FIXED/OPEN marks above come from grepping the tree as it stood on
2026-09-24; items 1, 2, 6 and 8 are the four that still need code.
