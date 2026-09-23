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

from discord_recall.config import whitelisted_guilds
from discord_recall.db import get_session_factory
from discord_recall.db.models import Channel, Digest, Job, Message, Server
from discord_recall.web import jobs as job_queue
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


class JobBody(BaseModel):
    """Enqueue a paced background job (see web/jobs.py for the policy)."""

    kind: str
    channelId: str | None = None
    since: str | None = None
    until: str | None = None
    maxMessages: int | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    period: str = "daily"
    force: bool = False

    model_config = ConfigDict(populate_by_name=True)


def whitelist_decision(channel_guild: int | None, whitelist: set[int]) -> tuple[bool, str]:
    """Pure guard: may we act on a channel belonging to this guild?"""
    if not whitelist:
        return True, "no guild whitelist configured"
    if channel_guild is None:
        return False, "channel is not in any known guild"
    if channel_guild in whitelist:
        return True, "guild is whitelisted"
    return False, f"guild {channel_guild} is outside GUILD_WHITELIST"


async def _guard_channel(channel_id: int) -> tuple[bool, str]:
    """Look up the channel's guild and apply the whitelist."""
    factory = get_session_factory()
    async with factory() as session:
        ch = await session.get(Channel, channel_id)
    return whitelist_decision(ch.server_id if ch else None, whitelisted_guilds())


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
async def servers(all: bool = Query(False, description="Ignore the guild whitelist")):
    """Top level of the folded navigation: every server, with counts."""
    wl = whitelisted_guilds()
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

    items = [
        {
            "id": str(sid),
            "name": name,
            "channels": channels,
            "captured": int(captured.get(sid, 0)),
            "digests": int(digest_counts.get(sid, 0)),
            "whitelisted": (not wl) or sid in wl,
        }
        for sid, name, channels in rows
    ]
    if wl and not all:
        items = [i for i in items if i["whitelisted"]]
    return {"servers": items, "whitelist": sorted(wl)}


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
        wl = whitelisted_guilds()
        if wl:
            # Outside the whitelist the app does not even list channels.
            stmt = stmt.where(Server.id.in_(sorted(wl)))
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


def _job_view(job: Job) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "phase": job.phase or "",
        "message": job.message or "",
        "messages": int(job.messages or 0),
        "channelId": str(job.channel_id) if job.channel_id else None,
        "createdAt": job.created_at.isoformat() if job.created_at else None,
        "updatedAt": job.updated_at.isoformat() if job.updated_at else None,
        "finishedAt": job.finished_at.isoformat() if job.finished_at else None,
    }


@router.post("/jobs")
async def create_job(body: JobBody):
    """Queue a job. Returns immediately; poll GET /api/jobs/<id> for progress."""
    kind = body.kind
    if kind not in ("capture", "digest", "discover"):
        raise HTTPException(status_code=400, detail="kind must be capture, digest or discover")

    payload: dict = {}
    channel_id = None
    if kind in ("capture", "digest"):
        if not body.channelId or not body.channelId.isdigit():
            raise HTTPException(status_code=400, detail="channelId must be a Discord snowflake")
        channel_id = int(body.channelId)
        allowed, reason = await _guard_channel(channel_id)
        if not allowed:
            return {"ok": False, "status": f"refused: {reason}", "whitelistBlocked": True}
        payload["channelId"] = body.channelId
    if kind == "capture":
        payload.update(
            since=body.since or None,
            until=body.until or None,
            maxMessages=body.maxMessages,
        )
    if kind == "digest":
        if not body.from_:
            raise HTTPException(status_code=400, detail="from is required for a digest job")
        if body.period not in ("daily", "weekly", "monthly"):
            raise HTTPException(status_code=400, detail="period must be daily, weekly or monthly")
        end = body.to or body.from_
        messages, latest_day = await _coverage(channel_id, body.from_, end)
        if messages == 0:
            hint = (
                f"latest captured day is {latest_day} - capture that range first"
                if latest_day
                else "this channel has no captured messages yet - run Capture first"
            )
            return {
                "ok": False,
                "status": f"no messages between {body.from_} and {end}: {hint}",
                "messages": 0,
                "latestDay": latest_day,
            }
        payload.update(**{"from": body.from_}, to=body.to or None, period=body.period, force=body.force)

    if kind == "discover":
        wl = whitelisted_guilds()
        # Never walk the whole account when a whitelist is configured: one
        # request per whitelisted guild instead of one per guild we are in.
        payload["servers"] = [str(g) for g in sorted(wl)]

    job_id = await job_queue.enqueue(kind, payload, channel_id=channel_id)
    return {"ok": True, "jobId": job_id, "status": f"{kind} queued"}


@router.get("/whitelist")
async def whitelist():
    """What the app is allowed to touch."""
    wl = whitelisted_guilds()
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(Server.id, Server.name).order_by(Server.name)
            )
        ).all() if wl else []
    return {
        "guildWhitelist": sorted(wl),
        "enforced": bool(wl),
        "guilds": [{"id": str(sid), "name": name} for sid, name in rows if sid in wl],
    }


@router.get("/jobs/{job_id}")
async def get_job(job_id: int):
    factory = get_session_factory()
    async with factory() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return _job_view(job)


@router.get("/jobs")
async def list_jobs(limit: int = Query(10, ge=1, le=100)):
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(Job).order_by(desc(Job.created_at)).limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return {"jobs": [_job_view(j) for j in rows]}


@router.post("/capture")
async def capture(body: CaptureBody):
    """Compat shim: enqueues a paced capture job instead of blocking the request."""
    return await create_job(
        JobBody(
            kind="capture",
            channelId=body.channelId,
            since=body.since,
            until=body.until,
            maxMessages=body.maxMessages,
        )
    )


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
    """Compat shim: enqueues a digest job."""
    return await create_job(
        JobBody(
            kind="digest",
            channelId=body.channelId,
            **{"from": body.start},
            to=body.to,
            period=body.period,
            force=body.force,
        )
    )


async def _build_digest_inline(body: DigestBody):
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
    """Compat shim: enqueues a channel-discovery job, scoped to the whitelist."""
    return await create_job(JobBody(kind="discover"))


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
    """Liveness plus the pacing state (a paused queue is visible, not silent)."""
    return {
        "ok": True,
        "tokenAccepted": not job_queue.auth_failed(),
        "pacing": {
            "batch": job_queue.PACE_BATCH,
            "delaySeconds": job_queue.PACE_DELAY,
            "jitterExtraSeconds": job_queue.PACE_JITTER_EXTRA,
            "maxMessagesPerRun": job_queue.PACE_MAX_MESSAGES,
            "maxRequestsPerRun": job_queue.PACE_MAX_REQUESTS,
            "max429Strikes": job_queue.MAX_429_STRIKES,
            "post429PauseSeconds": job_queue.POST_429_PAUSE,
        },
    }
