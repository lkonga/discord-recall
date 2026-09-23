"""Channel history backfill — paginate through Discord channel history."""

import asyncio
from datetime import date, datetime, timedelta, timezone

import discord
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from discord_recall.config import get_settings
from discord_recall.db import get_session_factory
from discord_recall.db.ingest import upsert_channel, upsert_thread, upsert_message
from discord_recall.db.models import BackfillStatus, ChannelBackfillState


def window_bounds(
    since: str | date | None = None,
    until: str | date | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Convert inclusive user-facing dates into a half-open UTC window.

    ``since=2026-09-01, until=2026-09-07`` -> ``(2026-09-01T00:00Z,
    2026-09-08T00:00Z)``, so the 7th is fully included. Both ``since`` and
    the returned ``until`` are half-open in the Discord API sense (``after``
    is exclusive, ``before`` is exclusive).
    """

    def _parse(value: str | date) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return datetime.strptime(value, "%Y-%m-%d").date()

    since_dt = None
    if since is not None:
        since_dt = datetime.combine(_parse(since), datetime.min.time()).replace(
            tzinfo=timezone.utc
        )

    until_dt = None
    if until is not None:
        until_dt = datetime.combine(
            _parse(until) + timedelta(days=1), datetime.min.time()
        ).replace(tzinfo=timezone.utc)

    return since_dt, until_dt


class ChannelForbidden(RuntimeError):
    """The account cannot read this channel (403).

    A 403 is a hard stop for that channel: retrying cannot help, and every retry
    is another invalid request against the account's budget.
    """


async def _get_backfill_state(session, channel_id: int) -> ChannelBackfillState | None:
    result = await session.execute(
        select(ChannelBackfillState).where(ChannelBackfillState.channel_id == channel_id)
    )
    return result.scalar_one_or_none()


async def _set_backfill_status(session, channel_id: int, status: BackfillStatus, **kwargs):
    values = dict(channel_id=channel_id, status=status.value, **kwargs)
    stmt = insert(ChannelBackfillState).values(**values).on_conflict_do_update(
        index_elements=[ChannelBackfillState.channel_id],
        set_=dict(status=status.value, **kwargs),
    )
    await session.execute(stmt)
    await session.commit()


async def backfill_channel(
    client: discord.Client,
    channel: discord.TextChannel | discord.Thread,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    max_messages: int | None = None,
    batch_size: int | None = None,
    delay: float | None = None,
):
    """Backfill a single channel's message history.

    ``since`` is inclusive, ``until`` is exclusive (pass the day after the
    last day you want). A windowed run is deliberately stateless: it neither
    resumes from nor writes ``channel_backfill_state``, so a bounded catch-up
    can never mark a channel "complete" or skip what an unbounded run owes.
    """
    settings = get_settings()
    session_factory = get_session_factory()
    batch_size = batch_size or settings.backfill_batch_size
    delay = settings.backfill_delay_seconds if delay is None else delay
    bounded = any(v is not None for v in (since, until, max_messages))

    is_thread = isinstance(channel, discord.Thread)
    channel_name = f"#{channel.name}" if hasattr(channel, "name") else str(channel.id)
    guild_name = channel.guild.name if channel.guild else "unknown"

    # Ensure channel/server rows exist before tracking backfill state
    async with session_factory() as session:
        if is_thread:
            await upsert_thread(session, channel)
        else:
            await upsert_channel(session, channel)
        await session.commit()

    # channel_backfill_state FK references channels table.
    # Threads live in the threads table, so we track by parent channel for threads.
    track_id = channel.parent_id if is_thread else channel.id

    state = None
    after_msg_id = None
    if not bounded:
        # Check existing state for resume
        async with session_factory() as session:
            state = await _get_backfill_state(session, track_id)
            after_msg_id = state.last_message_id if state else None

        if state and state.status == BackfillStatus.complete:
            logger.info(f"[{guild_name}/{channel_name}] Already complete, skipping")
            return

    window = ""
    if bounded:
        window = " (window" + (f" from {since:%Y-%m-%d}" if since else "") + (
            f" until {until:%Y-%m-%d}" if until else ""
        ) + (f", max {max_messages}" if max_messages else "") + ")"

    logger.info(
        f"[{guild_name}/{channel_name}] Starting backfill"
        + (f" (resuming after {after_msg_id})" if after_msg_id else "")
        + window
    )

    # Mark in progress (unbounded runs only: a window must not move the cursor)
    if not bounded:
        async with session_factory() as session:
            await _set_backfill_status(session, track_id, BackfillStatus.in_progress)

    total = 0
    last_id = after_msg_id
    after: datetime | discord.Object | None = since
    if after is None and after_msg_id:
        after = discord.Object(id=after_msg_id)
    before = until

    try:
        while True:
            remaining = None if max_messages is None else max_messages - total
            if remaining is not None and remaining <= 0:
                logger.info(
                    f"[{guild_name}/{channel_name}] Reached message cap ({max_messages})"
                )
                break
            limit = batch_size if remaining is None else min(batch_size, remaining)

            batch = []
            async for msg in channel.history(
                limit=limit, after=after, before=before, oldest_first=True
            ):
                batch.append(msg)

            if not batch:
                break

            async with session_factory() as session:
                for msg in batch:
                    await upsert_message(session, msg)
                await session.commit()

            last_id = batch[-1].id
            total += len(batch)
            after = discord.Object(id=last_id)

            # Save progress (unbounded runs only: a window must not move the cursor)
            if not bounded:
                async with session_factory() as session:
                    await _set_backfill_status(
                        session,
                        track_id,
                        BackfillStatus.in_progress,
                        last_message_id=last_id,
                        backfilled_through=batch[-1].created_at,
                    )

            logger.info(f"[{guild_name}/{channel_name}] {total} messages so far...")

            if len(batch) < limit:
                break

            await asyncio.sleep(delay)

        # Mark complete
        if not bounded:
            async with session_factory() as session:
                await _set_backfill_status(
                    session,
                    track_id,
                    BackfillStatus.complete,
                    last_message_id=last_id,
                    backfilled_through=datetime.now(timezone.utc),
                )

        logger.info(
            f"[{guild_name}/{channel_name}] Backfill complete — {total} messages"
            + (" (bounded run, state untouched)" if bounded else "")
        )

    except discord.Forbidden as exc:
        if not bounded:
            async with session_factory() as session:
                await _set_backfill_status(
                    session, track_id, BackfillStatus.failed, last_message_id=last_id
                )
        logger.error(
            f"[{guild_name}/{channel_name}] dropped: this account cannot read it (403)"
        )
        raise ChannelForbidden(
            f"#{channel_name} is not readable by this account (403) - dropped, not retried"
        ) from exc
    except Exception:
        if not bounded:
            async with session_factory() as session:
                await _set_backfill_status(
                    session,
                    track_id,
                    BackfillStatus.failed,
                    last_message_id=last_id,
                )
        logger.exception(f"[{guild_name}/{channel_name}] Backfill failed after {total} messages")
        raise


async def backfill_server(
    client: discord.Client,
    guild: discord.Guild,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    max_messages: int | None = None,
    batch_size: int | None = None,
    delay: float | None = None,
):
    """Backfill all text channels and threads in a server.

    ``max_messages`` applies per channel, not to the server as a whole.
    """
    logger.info(f"Backfilling server: {guild.name} ({guild.id})")

    # Text channels
    text_channels = [ch for ch in guild.channels if isinstance(ch, discord.TextChannel)]
    text_channels.sort(key=lambda c: c.position or 0)

    for ch in text_channels:
        try:
            await backfill_channel(
                client, ch, since=since, until=until, max_messages=max_messages,
                batch_size=batch_size, delay=delay,
            )
        except discord.Forbidden:
            logger.warning(f"  No access to #{ch.name}, skipping")
        except Exception:
            logger.exception(f"  Failed to backfill #{ch.name}")

    # Active threads
    try:
        threads = await guild.active_threads()
        for thread in threads:
            try:
                await backfill_channel(
                    client, thread, since=since, until=until, max_messages=max_messages,
                    batch_size=batch_size, delay=delay,
                )
            except discord.Forbidden:
                logger.warning(f"  No access to thread #{thread.name}, skipping")
            except Exception:
                logger.exception(f"  Failed to backfill thread #{thread.name}")
    except Exception:
        logger.exception("  Failed to fetch active threads")

    logger.info(f"Server backfill complete: {guild.name}")


class BackfillClient(discord.Client):
    """Ephemeral client that connects, runs backfill, then disconnects."""

    def __init__(
        self,
        channel_id: int | None = None,
        server_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        max_messages: int | None = None,
        batch_size: int | None = None,
        delay: float | None = None,
    ):
        super().__init__()
        self._target_channel_id = channel_id
        self._target_server_id = server_id
        self._window = dict(
            since=since, until=until, max_messages=max_messages,
            batch_size=batch_size, delay=delay,
        )

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} for backfill")
        try:
            if self._target_channel_id:
                channel = self.get_channel(self._target_channel_id)
                if channel is None:
                    channel = await self.fetch_channel(self._target_channel_id)
                await backfill_channel(self, channel, **self._window)

            elif self._target_server_id:
                guild = self.get_guild(self._target_server_id)
                if guild is None:
                    logger.error(f"Server {self._target_server_id} not found")
                else:
                    await backfill_server(self, guild, **self._window)
        except ChannelForbidden as exc:
            # Exit with a distinct code so the job queue can mark the channel
            # dropped instead of retrying it forever.
            logger.error(f"backfill dropped a channel: {exc}")
            self._exit_code = 3
        except Exception:
            logger.exception("Backfill failed")
            self._exit_code = 1
        finally:
            await self.close()


def run_backfill(
    channel_id: int | None = None,
    server_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    max_messages: int | None = None,
    batch_size: int | None = None,
    delay: float | None = None,
):
    settings = get_settings()
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is not set in .env")

    client = BackfillClient(
        channel_id=channel_id,
        server_id=server_id,
        since=since,
        until=until,
        max_messages=max_messages,
        batch_size=batch_size,
        delay=delay,
    )
    client.run(settings.discord_token, log_handler=None)
    code = getattr(client, "_exit_code", 0)
    if code:
        raise SystemExit(code)
