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
Discord-touching job at a time, with the policy below. Evidence is from a
dedicated rate-limit study of `dolfies/discord.py-self` (`discord/http.py`,
`discord/abc.py`), `Tyrrrz/DiscordChatExporter` (`DiscordClient.cs`,
`Utils/Http.cs`), the Undiscord issue threads, and Discord's public rate-limit
docs (`discord-api-docs`) plus the user-token notes in `discord-userdoccers`.

| Parameter | Value we ship | Why / evidence |
|---|---|---|
| Page size | `limit=100` (`--batch-size 100`) | DCE paginates at 100; py-self `history(limit=100)`. `limit=1` is a scripted-footprint tell |
| Base delay | 2.5 s | Undiscord hand-tested floor ~2100 ms; DCE's 1 s is a floor, not a pace |
| Jitter | +0 to +1.5 s uniform, never below base | Undiscord #168/#245: *constant* intervals are the signal, but dipping below the floor is worse |
| Cap per run | 1000 messages (≈10 requests) | Keeps a single run well inside the 30-request budget |
| Max requests | ≤30/run, ≤60/h, ≥10 min between automated runs | Requests, not messages, are the scarce unit |
| On 429 | wait `retry_after + 1 s` buffer, doubling per consecutive strike | DCE DelayGenerator adds a 1 s buffer to reset time |
| Consecutive 429s | **hard stop after 3** (`PACE_MAX_429_STRIKES`) | py-self stops sleeping and raises instead of retrying forever |
| 429 without `Retry-After` | abort the run, do not retry | userdoccers: "If no `Retry-After` header is present, you should not programmatically retry" |
| Global / Cloudflare 429 | abort run, pause 15 min | py-self gates every request on a global lock after a global limit |
| After any 429 | 10 min pause before the next job | Behavioural footprint grows with back-to-back runs |
| 401 | stop immediately, no retry | token is invalid; py-self raises rather than retrying |
| 403 | fail that channel's run, no retry | channel is not readable by this account |
| 5xx | ≤2 retries (handled inside py-self), then abort | py-self sleeps `1 + 2n` on 502/504/507/522-524 |
| Parallel channels | never more than 1 | per-route buckets are independent, but the global ceiling, the 10k/10min invalid-request budget and the undocumented Cloudflare layer are shared |

Invalid-request budget (10k/10min → 24 h ban) counts 401/403/429, but at ~10
requests per run it is not the binding constraint. The binding constraints are
per-route 429s and behavioural detection, which is why the hard stops matter more
than delay tuning. `PACE_*` env vars override every value above.

Known trade-off: windowed captures are stateless by design (they never touch
`channel_backfill_state`), so a repeated cron re-reads the head of the window
instead of resuming a cursor. With a 2-day window and a 1000-message cap that is
a handful of requests, and it keeps the "bounded run cannot corrupt resume
state" guarantee.
