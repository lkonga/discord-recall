"""JSON API for the React frontend.

Grouped/folded channel navigation: servers are the top level, each with channel
and capture counts; channels are listed per server with their message activity
so a date range can be chosen where data actually exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select

from discord_recall.db import get_session_factory
from discord_recall.db.models import Channel, Digest, Message, Server
from discord_recall.web.runner import last_line, run_cli

router = APIRouter(prefix="/api")


class CaptureBody(BaseModel):
    channelId: str
    since: str | None = None
    until: str | None = None
    maxMessages: int = 600


class DigestBody(BaseModel):
    """`from` is a Python keyword, so it is exposed as start with an alias."""

    model_config = ConfigDict(populate_by_name=True)

    channelId: str
    start: str = Field(alias="from")
    to: str | None = None
    period: str = "daily"
    force: bool = False


class AskBody(BaseModel):
    question: str
    channelId: str | None = None


def _msg_count_subquery():
    return (
        select(func.count(Message.id))
        .where(Message.channel_id == Channel.id)
        .correlate(Channel)
        .scalar_subquery()
    )


def _last_msg_subquery():
    return (
        select(func.max(Message.created_at))
        .where(Message.channel_id == Channel.id)
        .correlate(Channel)
        .scalar_subquery()
    )


@router.get("/servers")
async def servers():
    """Top level of the folded navigation: every server, with counts."""
    factory = get_session_factory()
    msg_count = _msg_count_subquery()
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    Server.id,
                    Server.name,
                    func.count(Channel.id).label("channels"),
                )
                .outerjoin(Channel, Channel.server_id == Server.id)
                .group_by(Server.id, Server.name)
                .order_by(Server.name)
            )
        ).all()

        captured = dict(
            (
                await session.execute(
                    select(Channel.server_id, func.count(Channel.id))
                    .where(msg_count > 0)
                    .group_by(Channel.server_id)
                )
            ).all()
        )
        digest_counts = dict(
            (
                await session.execute(
                    select(Channel.server_id, func.count(Digest.id))
                    .join(Digest, Digest.channel_id == Channel.id)
                    .group_by(Channel.server_id)
                )
            ).all()
        )

    return {
        "servers": [
            {
                "id": str(sid),
                "name": name,
                "channels": channels,
                "captured": int(captured.get(sid, 0)),
                "digests": int(digest_counts.get(sid, 0)),
            }
            for sid, name, channels in rows
        ]
    }


@router.get("/channels")
async def channels(
    server: str | None = Query(None, description="Server (guild) id"),
    q: str | None = Query(None, description="Case-insensitive name filter"),
    limit: int = Query(300, ge=1, le=2000),
):
    """Channels within one server (or all), with activity and digest counts."""
    factory = get_session_factory()
    msg_count = _msg_count_subquery()
    last_msg = _last_msg_subquery()
    async with factory() as session:
        stmt = (
            select(
                Channel.id,
                Channel.name,
                Server.id,
                Server.name,
                msg_count,
                last_msg,
                func.count(Digest.id),
                func.max(Digest.period_start),
            )
            .join(Server, Channel.server_id == Server.id)
            .outerjoin(Digest, Digest.channel_id == Channel.id)
            .group_by(Channel.id, Channel.name, Server.id, Server.name)
        )
        if server:
            stmt = stmt.where(Channel.server_id == int(server))
        if q:
            needle = f"%{q.lower()}%"
            stmt = stmt.where(func.lower(Channel.name).like(needle))
        rows = (
            await session.execute(
                stmt.order_by(desc(msg_count), Channel.name).limit(limit)
            )
        ).all()

    return {
        "channels": [
            {
                "id": str(cid),
                "name": name,
                "serverId": str(sid),
                "serverName": sname,
                "messages": int(msgs or 0),
                "lastMessage": last.isoformat() if last else None,
                "digests": int(digs or 0),
                "latestDigest": dig.isoformat() if dig else None,
            }
            for cid, name, sid, sname, msgs, last, digs, dig in rows
        ]
    }


@router.get("/channel/{channel_id}/activity")
async def activity(channel_id: str, days: int = Query(90, ge=1, le=3650)):
    """Per-day message counts, so the date picker can target days with data."""
    factory = get_session_factory()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with factory() as session:
        rows = (
            await session.execute(
                select(func.date(Message.created_at), func.count(Message.id))
                .where(Message.channel_id == int(channel_id))
                .where(Message.created_at >= since)
                .group_by(func.date(Message.created_at))
                .order_by(func.date(Message.created_at))
            )
        ).all()
        span = (
            await session.execute(
                select(func.min(Message.created_at), func.max(Message.created_at)).where(
                    Message.channel_id == int(channel_id)
                )
            )
        ).one()

    return {
        "days": [{"date": str(day), "messages": int(count)} for day, count in rows],
        "first": span[0].isoformat() if span[0] else None,
        "last": span[1].isoformat() if span[1] else None,
    }


@router.get("/channel/{channel_id}/digests")
async def digests(channel_id: str, limit: int = Query(50, ge=1, le=500)):
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(Digest)
                    .where(Digest.channel_id == int(channel_id))
                    .order_by(desc(Digest.period_start))
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return {
        "digests": [
            {
                "id": d.id,
                "period": d.period,
                "start": d.period_start.isoformat(),
                "end": d.period_end.isoformat(),
                "messages": d.message_count,
                "content": d.content,
            }
            for d in rows
        ]
    }


@router.post("/capture")
async def capture(body: CaptureBody):
    if not body.channelId.isdigit():
        raise HTTPException(status_code=400, detail="channelId must be a Discord snowflake")
    args = [
        "backfill",
        "-c",
        body.channelId,
        "--max-messages",
        str(body.maxMessages),
    ]
    if body.since:
        args += ["--since", body.since]
    if body.until:
        args += ["--until", body.until]
    rc, out = await run_cli(args)
    return {"ok": rc == 0, "status": last_line(out)}


async def _coverage(channel_id: int, start: str, end: str) -> tuple[int, str | None]:
    """Messages in [start, end] and the most recent day that has any."""
    factory = get_session_factory()
    async with factory() as session:
        count = (
            await session.execute(
                select(func.count(Message.id))
                .where(Message.channel_id == channel_id)
                .where(func.date(Message.created_at) >= start)
                .where(func.date(Message.created_at) <= end)
            )
        ).scalar_one()
        latest = (
            await session.execute(
                select(func.max(func.date(Message.created_at))).where(
                    Message.channel_id == channel_id
                )
            )
        ).scalar_one()
    return int(count or 0), (str(latest) if latest else None)


@router.post("/digest")
async def build_digest(body: DigestBody):
    if not body.channelId.isdigit():
        raise HTTPException(status_code=400, detail="channelId must be a Discord snowflake")
    if body.period not in ("daily", "weekly", "monthly"):
        raise HTTPException(status_code=400, detail="period must be daily, weekly or monthly")

    end = body.to or body.start
    messages, latest_day = await _coverage(int(body.channelId), body.start, end)
    if messages == 0:
        hint = (
            f"latest captured day is {latest_day} - capture that range first"
            if latest_day
            else "this channel has no captured messages yet - run Capture first"
        )
        return {
            "ok": False,
            "status": f"no messages between {body.start} and {end}: {hint}",
            "messages": 0,
            "latestDay": latest_day,
        }
    args = [
        "digest-range",
        "-c",
        body.channelId,
        "--from",
        body.start,
        "--period",
        body.period,
    ]
    if body.to:
        args += ["--to", body.to]
    if body.force:
        args += ["--force"]
    rc, out = await run_cli(args)
    return {"ok": rc == 0, "status": last_line(out), "messages": messages}


@router.post("/discover")
async def discover():
    rc, out = await run_cli(["discover", "--delay", "0.3"], timeout=900)
    return {"ok": rc == 0, "status": last_line(out)}


@router.post("/ask")
async def ask(body: AskBody):
    from discord_recall.digest.query import ask as ask_query

    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    answer = await ask_query(
        question=body.question,
        channel_id=int(body.channelId) if body.channelId and body.channelId.isdigit() else None,
    )
    return {"answer": answer}


@router.get("/health")
async def health():
    return {"ok": True}
