"""Seed synthetic Discord data for offline verification.

Lets you exercise ``digest`` / ``digest-range`` / ``ask`` without a Discord
token: it creates one server, three channels (one of them deliberately
quiet), a handful of authors, and messages spread across a date range.

    uv run python scripts/seed_demo_data.py --days 14 --end 2026-09-21
    uv run python scripts/seed_demo_data.py --days 14 --reset
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime, date, timedelta, timezone

from discord_recall.db import get_session_factory
from discord_recall.db.models import (
    Attachment,
    Author,
    Channel,
    Digest,
    Message,
    Reaction,
    Server,
    Thread,
)

SERVER_ID = 9_000_000_000_000_001
CHANNELS = [
    (9_000_000_000_000_011, "general"),
    (9_000_000_000_000_012, "dev"),
    (9_000_000_000_000_013, "quiet"),
]
AUTHORS = [
    (9_000_000_000_000_101, "asha", "Asha"),
    (9_000_000_000_000_102, "mike", "Mike"),
    (9_000_000_000_000_103, "chen", "Chen"),
    (9_000_000_000_000_104, "priya", "Priya"),
]

GENERAL = [
    "Reminder: release review is Thursday 15:00 UTC, agenda in the doc.",
    "We shipped the 2.4 release yesterday, rollback plan is in the runbook.",
    "Docs PR for the new onboarding flow is up for review.",
    "Support ticket volume dropped 30% after the cache fix.",
    "Please add yourself to the on-call rotation sheet before Friday.",
    "The marketing site will be in maintenance mode Saturday 02:00-04:00.",
    "Anyone have the link to the incident postmortem for the DB failover?",
    "Agreed - we keep the old API for two more releases, then deprecate.",
    "Budget approved for the extra staging environment.",
    "New hire starts Monday, someone please own their first-week plan.",
]

DEV = [
    "The digest worker is double-posting when the scheduler retries.",
    "Possible cause: we commit the digest row before the Telegram send.",
    "I reproduced it on staging with a forced 500 from the LLM gateway.",
    "Fix idea: make the (channel, period, period_start) tuple unique.",
    "SQLite lock contention went away after we batched the inserts.",
    "Can we pin the summarizer model per channel? Costs vary a lot.",
    "Bumped max_tokens to 4096 and the truncation warnings stopped.",
    "Tests for the backfill pagination are in PR #48, please review.",
    "The rate limiter needs a longer backoff - we hit 429 twice today.",
    "Let's standardize on UTC everywhere, naive datetimes keep biting us.",
    "Decided: keep chunk size 400 messages and synthesize in a second pass.",
    "Benchmarked the local model: 6x slower, but 0 API cost.",
]


def _build_messages(day: date) -> list[tuple[int, int, str]]:
    """Return ``(channel_id, author_id, content)`` rows for one day."""
    rng = random.Random(f"{day.isoformat()}-seed")
    rows: list[tuple[int, int, str]] = []

    for channel_id, pool, count in (
        (CHANNELS[0][0], GENERAL, rng.randint(4, 8)),
        (CHANNELS[1][0], DEV, rng.randint(5, 10)),
    ):
        for text in rng.sample(pool, k=min(count, len(pool))):
            author_id = rng.choice(AUTHORS)[0]
            rows.append((channel_id, author_id, text))
    return rows


async def _wipe(session) -> None:
    for model in (Reaction, Attachment, Digest, Message, Thread, Channel, Author, Server):
        await session.execute(model.__table__.delete())
    await session.commit()


async def seed(days: int, end: date, reset: bool) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        if reset:
            await _wipe(session)

        session.add(Server(id=SERVER_ID, name="Demo Lab", joined_at=datetime.now(timezone.utc)))
        for position, (channel_id, name) in enumerate(CHANNELS):
            session.add(
                Channel(
                    id=channel_id,
                    server_id=SERVER_ID,
                    name=name,
                    type="text",
                    position=position,
                )
            )
        for author_id, username, display in AUTHORS:
            session.add(
                Author(id=author_id, username=username, display_name=display, is_bot=False)
            )
        await session.commit()

        start = end - timedelta(days=days - 1)
        message_id = 9_100_000_000_000_000
        total = 0
        day = start
        while day <= end:
            for channel_id, author_id, content in _build_messages(day):
                created = datetime.combine(day, datetime.min.time()).replace(
                    tzinfo=timezone.utc
                ) + timedelta(hours=9 + (total % 13), minutes=(total * 7) % 60)
                session.add(
                    Message(
                        id=message_id,
                        channel_id=channel_id,
                        author_id=author_id,
                        content=content,
                        created_at=created,
                    )
                )
                message_id += 1
                total += 1
            day += timedelta(days=1)
        await session.commit()

    print(
        f"seeded server {SERVER_ID} (Demo Lab), channels "
        f"{', '.join(f'#{name} ({cid})' for cid, name in CHANNELS)}, "
        f"{total} messages from {start} to {end}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="How many days to seed.")
    parser.add_argument(
        "--end",
        default=(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat(),
        help="Last day to seed (YYYY-MM-DD). Default: yesterday.",
    )
    parser.add_argument(
        "--reset", action="store_true", help="Wipe tables before seeding."
    )
    args = parser.parse_args()
    asyncio.run(seed(args.days, date.fromisoformat(args.end), args.reset))


if __name__ == "__main__":
    main()
