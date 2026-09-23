"""Digest builder — summarize activity across channels via LLM."""

from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta

from loguru import logger
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from discord_recall.db import get_session_factory
from discord_recall.db.models import (
    Author,
    Channel,
    Digest,
    DigestPeriod,
    Message,
    Server,
)
from discord_recall.llm import complete

DIGEST_SYSTEM = """You are a Discord channel analyst. Summarize the conversation that happened in this channel during the given time period.

Rules:
- Be concise but capture key topics, decisions, arguments, and notable statements
- Name specific users when they make significant contributions
- Note any links, resources, or media shared
- Capture the emotional tone and energy level
- If there are debates, capture both sides
- Use bullet points for readability
- If the conversation is quiet or trivial, say so briefly
- Do NOT editorialize or add your own opinions"""

SYNTHESIS_SYSTEM = """You are a Discord intelligence analyst. Synthesize multiple conversation summaries into a coherent higher-level digest.

Rules:
- Identify the main themes and storylines across the summaries
- Highlight the most important discussions, decisions, or events
- Note active users and their key contributions
- Track evolving conversations and emerging trends
- Keep it scannable — use headers and bullets
- Lead with what matters most
- Be specific — names, topics, positions, not vague summaries"""

# Max messages to send in a single LLM call
CHUNK_SIZE = 400


async def _fetch_messages(
    session: AsyncSession,
    channel_id: int,
    start: datetime,
    end: datetime,
) -> list:
    """Fetch messages as (username, display_name, content, created_at) tuples."""
    stmt = (
        select(Author.username, Author.display_name, Message.content, Message.created_at)
        .join(Author, Message.author_id == Author.id)
        .where(
            and_(
                Message.channel_id == channel_id,
                Message.created_at >= start,
                Message.created_at < end,
                Message.deleted_at.is_(None),
            )
        )
        .order_by(Message.created_at)
    )
    result = await session.execute(stmt)
    return result.all()


def _format_messages(rows) -> str:
    """Format message rows into a chat transcript."""
    lines = []
    for username, display_name, content, created_at in rows:
        name = display_name or username
        ts = created_at.strftime("%Y-%m-%d %H:%M")
        if content and content.strip():
            lines.append(f"[{ts}] {name}: {content}")
    return "\n".join(lines)


async def _fetch_existing(session, channel_id, period, start, end) -> Digest | None:
    stmt = (
        select(Digest)
        .where(
            and_(
                Digest.channel_id == channel_id,
                Digest.period == period,
                Digest.period_start == start,
            )
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _summarize_messages(rows, server_name: str, channel_name: str, period_label: str) -> str:
    """Summarize messages, chunking if there are too many."""
    if not rows:
        return ""

    if len(rows) <= CHUNK_SIZE:
        transcript = _format_messages(rows)
        prompt = (
            f"Server: {server_name}\n"
            f"Channel: #{channel_name}\n"
            f"Period: {period_label}\n"
            f"Message count: {len(rows)}\n\n"
            f"--- Transcript ---\n{transcript}"
        )
        return await complete(DIGEST_SYSTEM, prompt)

    # Chunk and summarize in passes
    chunks = [rows[i:i + CHUNK_SIZE] for i in range(0, len(rows), CHUNK_SIZE)]
    logger.info(f"  Chunking {len(rows)} messages into {len(chunks)} passes")

    chunk_summaries = []
    for i, chunk in enumerate(chunks):
        transcript = _format_messages(chunk)
        start_ts = chunk[0][3].strftime("%Y-%m-%d %H:%M")
        end_ts = chunk[-1][3].strftime("%Y-%m-%d %H:%M")
        prompt = (
            f"Server: {server_name}\n"
            f"Channel: #{channel_name}\n"
            f"Period: {start_ts} to {end_ts} (chunk {i+1}/{len(chunks)})\n"
            f"Message count: {len(chunk)}\n\n"
            f"--- Transcript ---\n{transcript}"
        )
        summary = await complete(DIGEST_SYSTEM, prompt)
        chunk_summaries.append(f"### Part {i+1} ({start_ts} to {end_ts}, {len(chunk)} msgs)\n{summary}")

    # Synthesize chunks
    combined = "\n\n".join(chunk_summaries)
    synth_prompt = (
        f"Server: {server_name}\n"
        f"Channel: #{channel_name}\n"
        f"Period: {period_label}\n"
        f"Total messages: {len(rows)}\n\n"
        f"--- Part Summaries ---\n{combined}"
    )
    return await complete(SYNTHESIS_SYSTEM, synth_prompt)


async def _build_digest(
    channel_id: int,
    period: DigestPeriod,
    period_start: datetime,
    period_end: datetime,
    period_label: str,
) -> Digest | None:
    """Generic digest builder — works from raw messages for any period."""
    session_factory = get_session_factory()

    async with session_factory() as session:
        existing = await _fetch_existing(session, channel_id, period, period_start, period_end)
        if existing:
            return existing

        ch = await session.get(Channel, channel_id)
        if not ch:
            return None
        srv = await session.get(Server, ch.server_id)

        rows = await _fetch_messages(session, channel_id, period_start, period_end)

    if not rows:
        return None

    server_name = srv.name if srv else "Unknown"
    channel_name = ch.name

    logger.info(f"[{server_name}/#{channel_name}] {period.value} digest for {period_label} ({len(rows)} msgs)")
    content = await _summarize_messages(rows, server_name, channel_name, period_label)

    now = datetime.now(timezone.utc)
    digest = Digest(
        channel_id=channel_id,
        period=period,
        period_start=period_start,
        period_end=period_end,
        content=content,
        message_count=len(rows),
        metadata_={"server": server_name, "channel": channel_name},
        created_at=now,
    )

    async with session_factory() as session:
        session.add(digest)
        await session.commit()
        await session.refresh(digest)

    return digest


async def build_daily_digest(channel_id: int, day: datetime) -> Digest | None:
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return await _build_digest(
        channel_id, DigestPeriod.daily, day_start, day_end,
        day_start.strftime("%Y-%m-%d"),
    )


async def build_weekly_digest(channel_id: int, week_start: datetime) -> Digest | None:
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    return await _build_digest(
        channel_id, DigestPeriod.weekly, week_start, week_end,
        f"{week_start.strftime('%Y-%m-%d')} to {week_end.strftime('%Y-%m-%d')}",
    )


async def build_monthly_digest(channel_id: int, month_start: datetime) -> Digest | None:
    month_start = month_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_end = month_start + relativedelta(months=1)
    return await _build_digest(
        channel_id, DigestPeriod.monthly, month_start, month_end,
        month_start.strftime("%B %Y"),
    )


async def backfill_digests(channel_id: int) -> None:
    """Tiered digest backfill:
    - Monthly digests for everything older than 1 year
    - Weekly digests from 1 year ago to today
    - Daily digests not generated here (run going forward)
    """
    session_factory = get_session_factory()
    now = datetime.now(timezone.utc)
    one_year_ago = (now - relativedelta(years=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async with session_factory() as session:
        ch = await session.get(Channel, channel_id)
        if not ch:
            logger.error(f"Channel {channel_id} not found")
            return

        # Find date range of messages
        stmt = select(
            func.min(Message.created_at),
            func.max(Message.created_at),
        ).where(Message.channel_id == channel_id)
        result = await session.execute(stmt)
        earliest, latest = result.one()

    if not earliest:
        logger.info("No messages found")
        return

    logger.info(f"Messages span {earliest.date()} to {latest.date()}")

    # --- Monthly digests for older than 1 year ---
    if earliest < one_year_ago:
        month = earliest.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while month < one_year_ago:
            await build_monthly_digest(channel_id, month)
            month = month + relativedelta(months=1)

    # --- Weekly digests from 1 year ago to today ---
    # Start from Monday of the week containing one_year_ago
    week_start = one_year_ago - timedelta(days=one_year_ago.weekday())
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while week_start < today:
        await build_weekly_digest(channel_id, week_start)
        week_start += timedelta(days=7)

    logger.info("Digest backfill complete")


async def build_server_daily_digest(server_id: int, day: datetime) -> str | None:
    """Build daily digests for all channels in a server, return combined text."""
    session_factory = get_session_factory()
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    async with session_factory() as session:
        srv = await session.get(Server, server_id)
        if not srv:
            logger.error(f"Server {server_id} not found")
            return None

        stmt = (
            select(Message.channel_id, func.count().label("cnt"))
            .join(Channel, Message.channel_id == Channel.id)
            .where(
                and_(
                    Channel.server_id == server_id,
                    Message.created_at >= day_start,
                    Message.created_at < day_end,
                    Message.deleted_at.is_(None),
                )
            )
            .group_by(Message.channel_id)
            .order_by(func.count().desc())
        )
        result = await session.execute(stmt)
        active_channels = result.all()

    if not active_channels:
        logger.info(f"No activity in {srv.name} on {day_start.date()}")
        return None

    logger.info(f"Building daily digest for {srv.name}: {len(active_channels)} active channels")

    digests = []
    for ch_id, msg_count in active_channels:
        d = await build_daily_digest(ch_id, day)
        if d:
            digests.append(d)

    if not digests:
        return None

    parts = []
    for d in digests:
        meta = d.metadata_ or {}
        ch_name = meta.get("channel", str(d.channel_id))
        parts.append(f"## #{ch_name} ({d.message_count} msgs)\n\n{d.content}")

    header = f"# {srv.name} — {day_start.strftime('%A %B %d, %Y')}\n\n"
    return header + "\n\n---\n\n".join(parts)
