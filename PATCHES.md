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
