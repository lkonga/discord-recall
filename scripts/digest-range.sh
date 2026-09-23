#!/usr/bin/env bash
# Channel(s) x date-range digests, for cron or one-off runs.
#
# Flags (passed straight through to `discord-recall digest-range`):
#   --channel <id>       repeatable; digest these channel IDs
#   --server <id>        digest every stored channel of a server
#   --from / --to        YYYY-MM-DD, inclusive (--to defaults to --from)
#   --last <N>           last N days, ending yesterday (UTC)
#   --period <p>         daily | weekly | monthly
#   --send               push each digest to Telegram
#   --print              print each digest to stdout
#   --dry-run            list what would be built, no LLM calls
#   --force              recompute digests that already exist
#
# Env defaults (used when the matching flag is absent; CLI wins on conflict):
#   RECALL_CHANNELS=111,222   RECALL_SERVER=999      RECALL_PERIOD=daily
#   RECALL_FROM=2026-09-01    RECALL_TO=2026-09-14   RECALL_LAST=7
#
# Examples:
#   ./scripts/digest-range.sh --channel 111 --channel 222 --from 2026-09-01 --to 2026-09-14
#   ./scripts/digest-range.sh --server 999 --period weekly --from 2026-06-01
#   RECALL_CHANNELS=111,222 RECALL_LAST=7 ./scripts/digest-range.sh --send
#
# Cron (daily catch-up of two channels, 07:05 every day):
#   5 7 * * * cd /path/to/discord-recall && RECALL_CHANNELS=111,222 ./scripts/digest-range.sh --last 1 --send >> logs/digest.log 2>&1
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -f .env && ! -f .env.local ]]; then
  echo "No .env / .env.local in $(pwd) - copy .env.example first." >&2
  exit 1
fi

args=()
if [[ -n "${RECALL_CHANNELS:-}" ]]; then
  IFS=',' read -ra channels <<< "${RECALL_CHANNELS}"
  for channel in "${channels[@]}"; do
    channel="${channel// /}"
    [[ -n "$channel" ]] && args+=(--channel "$channel")
  done
fi
[[ -n "${RECALL_SERVER:-}" ]] && args+=(--server "$RECALL_SERVER")
[[ -n "${RECALL_PERIOD:-}" ]] && args+=(--period "$RECALL_PERIOD")
if [[ -n "${RECALL_FROM:-}" ]]; then
  args+=(--from "$RECALL_FROM")
elif [[ -n "${RECALL_LAST:-}" ]]; then
  args+=(--last "$RECALL_LAST")
fi
[[ -n "${RECALL_TO:-}" ]] && args+=(--to "$RECALL_TO")

mkdir -p logs
exec uv run discord-recall digest-range "${args[@]}" "$@"
