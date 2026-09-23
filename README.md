# Discord Recall

A personal Discord intelligence system. It captures messages from the Discord
servers you're in (via a user-account "self-bot"), stores them in a local
SQLite database, and uses an LLM to turn that firehose into readable digests
and answerable questions — optionally delivered to you on Telegram. It's also a
great fit for agentic harnesses like **Hermes** and **Open Claw**, which can run
it on a schedule and act on what it surfaces.

> [!CAUTION]
> **Self-bots violate Discord's [Terms of Service](https://discord.com/terms).**
> Automating a *user account* (as opposed to a proper Bot account) can get your
> account **permanently terminated**. Use a secondary / throwaway account only,
> keep request rates conservative, and treat this project as personal research
> and educational software. You assume all risk. Respect the privacy of the
> people in any community you capture.

This project was inspired by — and is built on top of —
[**dolfies/discord.py-self**](https://github.com/dolfies/discord.py-self).
See [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md) for full credits.

---

## Table of Contents

- [What it does](#what-it-does)
- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Discord Onboarding](#discord-onboarding) ← **start here if you're new**
  - [1. Enable Developer Mode](#1-enable-developer-mode)
  - [2. Get your Discord token](#2-get-your-discord-token)
  - [3. Copy a Server ID](#3-copy-a-server-id)
  - [4. Copy a Channel ID](#4-copy-a-channel-id)
- [Configuration](#configuration)
- [Usage](#usage)
- [Agentic Use (Hermes / Open Claw)](#agentic-use-hermes--open-claw)
- [Architecture](#architecture)
- [Database](#database)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

---

## What it does

- **Capture** — a real-time listener records new messages, edits, and deletions
  from the servers you watch.
- **Backfill** — paginate through a channel's or an entire server's history to
  pull in everything that was said before you started.
- **Digest** — summarize a day/week/month of a channel or server into a concise
  brief using an LLM (tiered so years of history don't cost a fortune).
- **Ask** — query your captured data in natural language ("what did X say about
  Y?"), grounded in the stored messages and digests.
- **Deliver** — optionally push digests to a Telegram chat.
- **Feed an agent** — every command is a scriptable, stdout-emitting building
  block, so an LLM agent can schedule it, parse it, and act on it. See
  [Agentic Use](#agentic-use-hermes--open-claw).

---

## Tech Stack

- **Language**: Python 3.11+
- **Package/Env manager**: [uv](https://github.com/astral-sh/uv)
- **Discord library**: [discord.py-self](https://github.com/dolfies/discord.py-self)
- **CLI**: [Typer](https://typer.tiangolo.com/)
- **Database**: SQLite by default (via `aiosqlite`); Postgres supported
- **ORM / migrations**: [SQLAlchemy 2.0](https://www.sqlalchemy.org/) (async) + [Alembic](https://alembic.sqlalchemy.org/)
- **LLM gateway**: [OpenRouter](https://openrouter.ai/) (any compatible model)
- **Delivery**: [python-telegram-bot](https://python-telegram-bot.org/) (optional)
- **Config / logging**: Pydantic Settings, Loguru

---

## Prerequisites

- **Python 3.11 or newer**
- **[uv](https://github.com/astral-sh/uv)** — install with:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- A **Discord account** you're willing to use a token from (throwaway recommended).
- An **[OpenRouter API key](https://openrouter.ai/keys)** (needed for `digest` and `ask`).
- *(Optional)* A **Telegram bot token** from [@BotFather](https://t.me/BotFather) for delivery.
- *(Optional)* **Docker** — only if you'd rather run Postgres than SQLite.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/<your-username>/discord-recall.git
cd discord-recall

# 2. Install dependencies into a managed virtualenv
uv sync

# 3. Create your config
cp .env.example .env
#    ...then fill in DISCORD_TOKEN, SERVER_IDS, OPENROUTER_API_KEY
#    (see "Discord Onboarding" below for where these come from)

# 4. Create the database schema
uv run discord-recall migrate

# 5. Start capturing
uv run discord-recall listen
```

> Don't have your token / IDs yet? Head to **[Discord Onboarding](#discord-onboarding)**.

---

## Discord Onboarding

Everything below happens inside the Discord app (desktop or web). These four
values are all you need to get going.

### 1. Enable Developer Mode

Developer Mode adds "Copy ... ID" options to right-click menus.

1. Open Discord.
2. Click the **gear icon** (User Settings) next to your name, bottom-left.
3. In the sidebar scroll to **App Settings → Advanced**.
4. Toggle **Developer Mode** **ON**.

You can now right-click servers, channels, messages, and users to copy their IDs.

### 2. Get your Discord token

Your **user token** authenticates the self-bot *as you*. Treat it like a
password — anyone with it has full access to your account.

> [!WARNING]
> Never paste your token into a website, share it, or commit it. Discord will
> never ask for it. Tokens are what account-stealing scams are after.

**Via the desktop/web app (Chrome/Edge/Firefox DevTools):**

1. Open Discord in your browser (or press `Ctrl/Cmd + Shift + I` in the desktop app to open DevTools).
2. Open the **Network** tab in DevTools.
3. Refresh / interact with Discord so requests appear (e.g. click a channel).
4. In the request filter box, type `api` (or `messages`).
5. Click any request to `discord.com/api/...` and open its **Headers**.
6. Under **Request Headers**, find **`authorization`** — its value *is* your token.
7. Copy that value into your `.env` as `DISCORD_TOKEN=...`.

That token goes into your `.env`:

```bash
DISCORD_TOKEN=your_token_here
```

### 3. Copy a Server ID

A "Server ID" (Discord's API calls it a *guild* ID) tells Discord Recall which
servers to capture.

1. With **Developer Mode** on, **right-click the server's icon** in the far-left
   server rail.
2. Click **Copy Server ID**.
3. Paste it into `SERVER_IDS` in your `.env` as a JSON list:

   ```bash
   # Watch one server
   SERVER_IDS=[111111111111111111]

   # Watch several
   SERVER_IDS=[111111111111111111, 222222222222222222]

   # Watch EVERY server the account is in (use with care)
   SERVER_IDS=[]
   ```

### 4. Copy a Channel ID

You need a Channel ID to backfill or digest a single channel.

1. With **Developer Mode** on, **right-click the channel name** in the channel
   list (or right-click a thread).
2. Click **Copy Channel ID**.
3. Use it on the command line, e.g.:

   ```bash
   uv run discord-recall backfill --channel 998877665544332211
   ```

> **Tip:** You can also copy a **Message ID** (right-click a message → *Copy Message ID*)
> or a **User ID** (right-click a user → *Copy User ID*) the same way — handy for debugging.

---

## Configuration

Configuration is read from a `.env` file in the project root (see
[`.env.example`](.env.example) for the annotated template).

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | — | Your Discord user-account token ([how to get it](#2-get-your-discord-token)). |
| `SERVER_IDS` | ✅ | `[]` | JSON list of [Server IDs](#3-copy-a-server-id) to watch. `[]` = all servers. |
| `DATABASE_URL` | — | `sqlite+aiosqlite:///discord_recall.db` | Async DB connection string. |
| `OPENROUTER_API_KEY` | ✅* | — | [OpenRouter](https://openrouter.ai/keys) key. *Required for `digest` and `ask`. |
| `OPENROUTER_MODEL` | — | `google/gemini-3.1-flash-lite` | Any OpenRouter-compatible model ID. |
| `TELEGRAM_BOT_TOKEN` | — | — | Bot token from [@BotFather](https://t.me/BotFather) (for `--send`). |
| `TELEGRAM_CHAT_ID` | — | — | Chat/group/channel ID to deliver digests to. |
| `LOG_LEVEL` | — | `INFO` | Loguru log level. |
| `BACKFILL_BATCH_SIZE` | — | `100` | Messages fetched per Discord API call during backfill. |
| `BACKFILL_DELAY_SECONDS` | — | `1.0` | Pause between backfill batches (rate-limit safety). |

> See [GUI.md](GUI.md) for the design rationale behind every setting (intended
> as a reference for a future settings UI).

---

## Usage

All commands are exposed through the `discord-recall` CLI. Prefix with
`uv run` (or use the [`./start.sh`](start.sh) convenience wrapper).

```bash
# Initialize / upgrade the database schema
uv run discord-recall migrate

# Start the real-time listener (captures new messages, edits, deletes)
uv run discord-recall listen

# Backfill a single channel's full history
uv run discord-recall backfill --channel <channel_id>

# Backfill every readable channel + thread in a server
uv run discord-recall backfill --server <server_id>

# Generate a digest for a server (defaults to yesterday)
uv run discord-recall digest --server <server_id>

# Digest a specific channel on a specific day, and send to Telegram
uv run discord-recall digest --channel <channel_id> --date 2026-06-01 --send

# Backfill historical digests for a channel (monthly >1yr, weekly last year)
uv run discord-recall digest-backfill --channel <channel_id>

# Digest several channels across a whole date range (daily | weekly | monthly)
uv run discord-recall digest-range -c <id_a> -c <id_b> --from 2026-09-01 --to 2026-09-14
uv run discord-recall digest-range --server <server_id> --period weekly --from 2026-06-01
uv run discord-recall digest-range -c <id> --last 7 --send

# Same thing from cron, with env-driven defaults
RECALL_CHANNELS=<id_a>,<id_b> ./scripts/digest-range.sh --last 1 --send

# Ask a natural-language question about your captured data
uv run discord-recall ask "what did the community decide about X?" --server <server_id>
uv run discord-recall ask "summarize alice's takes on rust" --user alice
```

`digest-range` is idempotent per `(channel, period, period_start)`, supports
`--dry-run` (lists channels, period labels and message counts, no LLM calls)
and `--force` (rebuild a cell), and isolates failures per cell so one bad day
does not abort the run. See [PATCHES.md](PATCHES.md) for details, including how
to point the summarizer at DeepSeek or any OpenAI-compatible endpoint via
`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`, and
`scripts/seed_demo_data.py` for a no-Discord-token test dataset.

Run any command with `--help` for its full options:

```bash
uv run discord-recall --help
uv run discord-recall digest --help
uv run discord-recall digest-range --help
```

---

## Agentic Use (Hermes / Open Claw)

Discord Recall is designed to be the **eyes and memory of an LLM agent**, not just
a standalone CLI. The interesting part isn't `uv sync` — it's that you turn a pile
of Discord servers into a **queryable local intelligence feed**, then let an agent
do scheduled analyst work on top of it. Because every command is non-interactive,
scriptable, and prints plain text to stdout, an agentic runtime — like a
**Hermes**-style autonomous agent or an **Open Claw** Claude agent — can run it on
a schedule, read the output, reason over it, and act (alert you, file a note, kick
off the next job).

### The orchestration layers

```
1. Listener          →  the raw data pipe (real-time Discord capture)
2. SQLite            →  durable memory of everything that was said
3. Agent cron        →  the analyst (summarize, flag, decide) — a prompt, not code
4. Discord/Telegram  →  the delivery surface
```

The key move: the summarizer lives in the **agent's cron prompt**, not in the
app. Want "more tickers," "less fluff," "track recurring users," or "only channels
with >20 messages"? That's a prompt edit, not a redeploy. It ends up feeling like
a private Discord Bloomberg terminal — held together by Python, SQLite, cron, and
an agent that refuses to sleep.

### How it runs in practice

The listener runs as a **long-lived background process** on the same machine as
the agent — not babysat in a terminal tab. The agent starts it and can later check
whether it's still alive.

```bash
cd /opt/discord-recall
./start.sh listen          # thin wrapper around: uv run discord-recall listen
```

So the live process tree looks roughly like:

```text
uv run discord-recall listen
└── .venv/bin/python .../discord-recall listen
```

> **Secrets on a server:** `start.sh` just reads a local `.env`. If you'd rather
> not keep a plaintext `.env` on the box, front the command with a secrets
> manager — e.g. `op run --env-file=.env.tpl -- uv run discord-recall listen` for
> 1Password, or your platform's secret injection. The app only cares that the env
> vars are set.

### What the listener captures

Real-time, via Discord gateway events:

- `on_message` — new messages
- `on_message_edit` — edits update the stored row
- `on_message_delete` / `on_raw_message_delete` — mark messages deleted (kept, not purged)
- `SERVER_IDS` controls which servers are watched; an **empty list means watch everything**

Everything lands in SQLite (default `discord_recall.db`). Run a `backfill` first to
build history, then let the listener keep it current:

```bash
uv run discord-recall backfill --server <server_id>    # whole server
uv run discord-recall backfill --channel <channel_id>  # one channel
```

### Scheduled analyst (cron)

A typical agent keeps a daily cron job that summarizes the last day **from the
local database only** — it does *not* call Discord during summary generation:

```cron
# 22:00 UTC daily — build a per-server summary and deliver it back to Discord/Telegram
0 22 * * *  cd /opt/discord-recall && uv run discord-recall digest --server <server_id> --send
```

The editorial judgment lives in the agent's prompt, not the code. A real one might
say: use `America/New_York` as the day boundary, summarize midnight-ET to now,
include message + channel counts per server, and emphasize different things per
server — for example:

| Example server | What the agent emphasizes |
|---|---|
| **Trading Floor** | tickers, market themes, investment theses, risk flags |
| **Build Logs** | launches, shipped projects, tools, workflow ideas, bugs |
| **Macro Lounge** | macro themes, market debates, notable arguments |

*(Example server names — swap in your own.)*

### Example: investment-Discord price alerts

Watch a few **investment / trading Discords**, summarize them, and fire **price
alerts** when something actionable shows up.

1. **Capture** continuously: `uv run discord-recall listen` (as a service — see [Deployment](#deployment)).
2. **Agent-scheduled cron** maintains entries like:
   ```cron
   # Every 30 min: ask whether any actionable signals appeared, pipe the answer to the agent
   */30 * * * *  cd /opt/discord-recall && uv run discord-recall ask \
       "List any new price targets, entries, exits, or alerts in the last 30 minutes, with ticker, level, and who said it. Reply 'none' if there are none." \
       --server <trading_server_id> >> /var/log/discord-recall/signals.log 2>&1
   ```
3. **Agent reacts** — when it sees a concrete signal (e.g. "buy $NVDA under 120"),
   it cross-checks the live price and, if the condition is met, pushes a **price
   alert** and logs it to your watchlist. It can also *write new cron jobs on the
   fly* — e.g. poll a hot ticker more frequently once it's mentioned.

### Other agentic recipes

- **Daily intel briefing** — server-wide `digest --send` each morning with a one-line "why this matters" preface.
- **Topic watch** — periodic `ask "anything new about <topic>?"`, notify only when the answer changes.
- **Auto-onboarding new servers** — on join, `backfill --server <id>` once, then add a recurring digest cron.
- **Person tracker** — `ask "summarize <user>'s recent takes" --user <name>` on a cadence.

> The CLI never assumes an agent is present — these are just scheduled
> invocations. Any orchestrator that can run a shell command and read stdout
> (cron, systemd timers, Hermes, Open Claw, n8n, a plain Python script) works.

---

## Architecture

### Directory structure

```
.
├── src/discord_recall/
│   ├── cli.py              # Typer CLI entry point (listen/backfill/digest/ask/...)
│   ├── config.py           # Pydantic settings, loads .env
│   ├── llm.py              # OpenRouter chat-completions client (httpx)
│   ├── capture/
│   │   ├── listener.py     # Real-time self-bot event handlers
│   │   └── backfill.py     # Paginated history backfill (channel + server)
│   ├── db/
│   │   ├── __init__.py     # Async engine + session factory
│   │   ├── models.py       # SQLAlchemy models (servers, channels, messages, ...)
│   │   └── ingest.py       # Upsert logic for Discord entities (SQLite dialect)
│   ├── digest/
│   │   ├── builder.py      # LLM summarization, tiered digest backfill
│   │   ├── query.py        # "ask" engine — retrieves context, prompts the LLM
│   │   └── telegram.py     # Optional Telegram delivery
│   └── enrich/             # Entity extraction / knowledge graph (stubs, WIP)
├── migrations/             # Alembic migrations
├── .env.example            # Annotated config template
├── docker-compose.yml      # Optional Postgres for non-SQLite setups
├── pyproject.toml          # Project metadata + dependencies
└── start.sh                # Convenience CLI wrapper
```

### Capture flow

```
Discord (your account)
        │  discord.py-self events / history API
        ▼
  capture/listener.py ──┐
  capture/backfill.py ──┤ upsert
        ▼               │
   db/ingest.py ────────┘
        ▼
   SQLite (servers, channels, threads, authors, messages, attachments, reactions)
```

### Digest / ask flow

```
   SQLite messages ──► digest/builder.py ──► llm.py (OpenRouter) ──► digests table
                                                                          │
   question ──► digest/query.py ── retrieves digests + matching messages ─┤
                                                                          ▼
                                                          llm.py (OpenRouter) ──► answer ──► stdout / Telegram
```

The digest backfill is **tiered** to control LLM cost:

| Period | Coverage | Why |
|---|---|---|
| Monthly | Messages older than 1 year | Compress deep history into a few calls. |
| Weekly | The last 12 months | More granularity where detail still matters. |
| Daily | Today onward | Full resolution for current activity. |

---

## Database

By default everything is stored in a local SQLite file, `discord_recall.db`.
This file holds captured messages and **is intentionally git-ignored** — never
commit it.

**Initialize or upgrade the schema:**

```bash
uv run discord-recall migrate     # == alembic upgrade head
```

**Using Postgres instead of SQLite (optional):**

```bash
# Start the bundled Postgres
docker compose up -d

# Point the app at it
# DATABASE_URL=postgresql+asyncpg://discord_recall:discord_recall@localhost:5433/discord_recall
```

Then run `migrate` again against the new database.

### Core tables

| Table | Holds |
|---|---|
| `servers` | Guilds (id, name, icon, joined_at) |
| `channels` | Text channels per server |
| `threads` | Threads, linked to a parent channel |
| `authors` | Message authors (username, display name, bot flag) |
| `messages` | Message content, timestamps, edits, soft-deletes, embeds |
| `attachments` | Files/media attached to messages |
| `reactions` | Emoji reactions per message/user |
| `digests` | Generated summaries (daily/weekly/monthly) |
| `wiki_pages` | Enrichment pages (schema present, population WIP) |
| `channel_backfill_state` | Resumable backfill progress per channel |

---

## Deployment

There's no web service to host — this is a long-running CLI process plus a
database. A typical production setup runs the listener as a background service.

**systemd (Linux VPS) example:**

```ini
# /etc/systemd/system/discord-recall.service
[Unit]
Description=Discord Recall listener
After=network-online.target

[Service]
WorkingDirectory=/opt/discord-recall
ExecStart=/usr/bin/env uv run discord-recall listen
Restart=on-failure
EnvironmentFile=/opt/discord-recall/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now discord-recall
sudo journalctl -u discord-recall -f      # follow logs
```

Schedule digests with cron:

```cron
# Send a daily digest at 8am
0 8 * * *  cd /opt/discord-recall && uv run discord-recall digest --server <server_id> --send
```

> Keep `BACKFILL_DELAY_SECONDS` conservative in production. Aggressive request
> rates are the fastest way to get a self-bot account flagged.

---

## Troubleshooting

**`DISCORD_TOKEN is not set in .env`**
Your `.env` is missing or the token line is blank. Re-check
[step 2](#2-get-your-discord-token). Make sure the file is named exactly `.env`.

**`OPENROUTER_API_KEY is not set in .env`**
`digest` and `ask` need an OpenRouter key. Add one from
[openrouter.ai/keys](https://openrouter.ai/keys).

**Login fails / immediate disconnect**
Your token is likely expired (logging out of Discord rotates it) or invalid.
Grab a fresh one. Repeated failures can also indicate the account was flagged.

**"No access to #channel, skipping" during backfill**
The account simply can't read that channel — backfill skips it and continues.

**Watching the wrong servers**
`SERVER_IDS` must be a JSON array of integers, e.g. `[123, 456]`. An empty list
`[]` means *every* server the account is in.

**No activity found for that period**
You can only digest what you've captured. Run `backfill` (or let `listen` run a
while) before generating digests.

**Rate limited / account warnings**
Increase `BACKFILL_DELAY_SECONDS` and decrease `BACKFILL_BATCH_SIZE`.

---

## Contributing

Contributions are welcome. For local development:

```bash
# Install dev dependencies
uv sync --extra dev

# Lint & format
uv run ruff check .
uv run ruff format .

# Run tests
uv run pytest
```

Please keep the [ethical note](ACKNOWLEDGEMENTS.md#a-note-on-ethics) in mind and
don't submit features designed to evade Discord's anti-abuse systems or to
harvest data at scale.

---

## License

Released under the [MIT License](LICENSE).

Built on and inspired by [dolfies/discord.py-self](https://github.com/dolfies/discord.py-self).
Full credits in [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).
