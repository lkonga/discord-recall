"""Date-range digest runner.

The stock CLI digests exactly one day at a time (``--date``). This module
generalizes that to *any* channel selection crossed with *any* date range,
so a cron job or a one-off backfill can cover e.g. "#dev and #general for
2026-09-01 through 2026-09-14" in a single invocation.

Periods are aligned the same way the builder's backfill aligns them:

* ``daily``   — one digest per calendar day (UTC)
* ``weekly``  — one digest per ISO week, anchored on Monday
* ``monthly`` — one digest per calendar month, anchored on the 1st

Everything is idempotent: an existing digest row for the same
``(channel, period, period_start)`` is reused unless ``force`` is set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from dateutil.relativedelta import relativedelta
from loguru import logger
from sqlalchemy import and_, delete, func, select

from discord_recall.db import get_session_factory
from discord_recall.db.models import Channel, Digest, DigestPeriod, Message
from discord_recall.digest.builder import (
    build_daily_digest,
    build_monthly_digest,
    build_weekly_digest,
)

PERIODS: dict[str, DigestPeriod] = {
    "daily": DigestPeriod.daily,
    "weekly": DigestPeriod.weekly,
    "monthly": DigestPeriod.monthly,
}


class RangeError(ValueError):
    """Raised for invalid user input to the range runner."""


@dataclass
class PlannedDigest:
    channel_id: int
    channel_name: str
    period: str
    label: str
    start: datetime
    end: datetime
    message_count: int


@dataclass
class RangeResult:
    planned: list[PlannedDigest] = field(default_factory=list)
    built: list[tuple[int, str, str, int]] = field(default_factory=list)
    reused: list[tuple[int, str, str]] = field(default_factory=list)
    empty: list[tuple[int, str, str]] = field(default_factory=list)
    failed: list[tuple[int, str, str, str]] = field(default_factory=list)
    digits: list[tuple[int, str, str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise RangeError(f"Invalid date {value!r}, expected YYYY-MM-DD") from exc


def align(day: date, period: str) -> date:
    """Snap ``day`` to the period start it belongs to."""
    if period == "daily":
        return day
    if period == "weekly":
        return day - timedelta(days=day.weekday())
    if period == "monthly":
        return day.replace(day=1)
    raise RangeError(f"Unknown period {period!r} (daily | weekly | monthly)")


def iter_period_starts(start: date, end: date, period: str):
    """Yield period-start dates covering ``start``..``end`` inclusive."""
    if start > end:
        raise RangeError(f"--from {start} is after --to {end}")

    cursor = align(start, period)
    step = {
        "daily": lambda d: d + timedelta(days=1),
        "weekly": lambda d: d + timedelta(days=7),
        "monthly": lambda d: d + relativedelta(months=1),
    }[period]

    while cursor <= end:
        yield cursor
        cursor = step(cursor)


def _bounds(period_start: date, period: str) -> tuple[datetime, datetime, str]:
    """Return ``(start, end, label)`` as UTC datetimes for a period start."""
    if period == "daily":
        start_d, end_d = period_start, period_start + timedelta(days=1)
        label = period_start.strftime("%Y-%m-%d")
    elif period == "weekly":
        start_d, end_d = period_start, period_start + timedelta(days=7)
        label = f"{start_d.strftime('%Y-%m-%d')} to {end_d.strftime('%Y-%m-%d')}"
    elif period == "monthly":
        start_d, end_d = period_start, period_start + relativedelta(months=1)
        label = period_start.strftime("%B %Y")
    else:  # pragma: no cover - guarded by PERIODS lookup
        raise RangeError(f"Unknown period {period!r}")

    start = datetime.combine(start_d, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = datetime.combine(end_d, datetime.min.time()).replace(tzinfo=timezone.utc)
    return start, end, label


async def _count_messages(channel_id: int, start: datetime, end: datetime) -> int:
    session_factory = get_session_factory()
    async with session_factory() as session:
        stmt = select(func.count()).where(
            and_(
                Message.channel_id == channel_id,
                Message.created_at >= start,
                Message.created_at < end,
                Message.deleted_at.is_(None),
            )
        )
        return int((await session.execute(stmt)).scalar_one())


async def _existing_digest(channel_id: int, period: str, start: datetime) -> Digest | None:
    session_factory = get_session_factory()
    async with session_factory() as session:
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
        return (await session.execute(stmt)).scalar_one_or_none()


async def _drop_digest(channel_id: int, period: str, start: datetime) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        await session.execute(
            delete(Digest).where(
                and_(
                    Digest.channel_id == channel_id,
                    Digest.period == period,
                    Digest.period_start == start,
                )
            )
        )
        await session.commit()


async def resolve_channels(
    channel_ids: list[int] | None = None,
    server_id: int | None = None,
) -> tuple[list[tuple[int, str]], list[int]]:
    """Return ``([(channel_id, name)], missing_ids)`` for the given selection."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        if channel_ids:
            found: list[tuple[int, str]] = []
            missing: list[int] = []
            for cid in dict.fromkeys(channel_ids):
                ch = await session.get(Channel, cid)
                if ch:
                    found.append((ch.id, ch.name))
                else:
                    missing.append(cid)
            return found, missing

        if server_id:
            stmt = (
                select(Channel.id, Channel.name)
                .where(Channel.server_id == server_id)
                .order_by(Channel.position, Channel.name)
            )
            rows = (await session.execute(stmt)).all()
            return [(r[0], r[1]) for r in rows], []

    raise RangeError("Provide --channel <id> (repeatable) or --server <id>")


async def plan_range(
    channels: list[tuple[int, str]],
    date_from: date,
    date_to: date,
    period: str,
) -> list[PlannedDigest]:
    """Dry-run plan: which digests would be built, and their message counts."""
    planned: list[PlannedDigest] = []
    for period_start in iter_period_starts(date_from, date_to, period):
        start, end, label = _bounds(period_start, period)
        for channel_id, name in channels:
            count = await _count_messages(channel_id, start, end)
            if count == 0:
                continue
            planned.append(
                PlannedDigest(
                    channel_id=channel_id,
                    channel_name=name,
                    period=period,
                    label=label,
                    start=start,
                    end=end,
                    message_count=count,
                )
            )
    return planned


async def run_range(
    channel_ids: list[int] | None = None,
    server_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    period: str = "daily",
    force: bool = False,
    dry_run: bool = False,
) -> RangeResult:
    """Build every digest for the channel selection crossed with the range."""
    if period not in PERIODS:
        raise RangeError(f"Unknown period {period!r} (daily | weekly | monthly)")
    if date_from is None:
        raise RangeError("--from is required")
    date_to = date_to or date_from
    if date_from > date_to:
        raise RangeError(f"--from {date_from} is after --to {date_to}")

    channels, missing = await resolve_channels(channel_ids, server_id)
    if missing:
        logger.warning(f"Channels not found in the local store: {missing}")
    if not channels:
        raise RangeError("No matching channels in the local store — run backfill first")

    result = RangeResult()
    if dry_run:
        result.planned = await plan_range(channels, date_from, date_to, period)
        return result

    builders = {
        "daily": build_daily_digest,
        "weekly": build_weekly_digest,
        "monthly": build_monthly_digest,
    }

    for period_start in iter_period_starts(date_from, date_to, period):
        start, end, label = _bounds(period_start, period)
        for channel_id, name in channels:
            existing = await _existing_digest(channel_id, period, start)
            if existing and not force:
                result.reused.append((channel_id, name, label))
                logger.info(f"[{name}] {period} {label}: reusing existing digest")
                continue
            if existing and force:
                await _drop_digest(channel_id, period, start)

            try:
                digest = await builders[period](channel_id, start)
            except Exception as exc:  # noqa: BLE001 - reported per cell, run continues
                logger.error(f"[{name}] {period} {label}: FAILED — {exc}")
                result.failed.append((channel_id, name, label, str(exc)))
                continue

            if digest is None:
                result.empty.append((channel_id, name, label))
                logger.info(f"[{name}] {period} {label}: no activity")
                continue

            result.built.append((channel_id, name, label, digest.message_count))
            result.digits.append((channel_id, name, label, digest.content))

    return result
