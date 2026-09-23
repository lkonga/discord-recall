"""Background job queue for paced Discord work.

Long capture/digest runs are queued here and executed **one at a time** with
conservative pacing, so:

* an HTTP request never waits on a multi-minute backfill (the UI polls progress),
* a browser reload or a dropped connection cannot orphan the work,
* the token sees a steady, jittered request rate instead of bursts, and
* a 429 pauses everything rather than retrying immediately.

Pacing comes from the rate-limit research captured in PATCHES.md: small pages,
multi-second jittered delays, a hard per-run cap, sequential channels only.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess  # noqa: F401 - asyncio.subprocess is not auto-imported
import os
import random
import re
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select, update

from discord_recall.db import get_session_factory
from discord_recall.db.models import Job, JobStatus

PACE_BATCH = int(os.environ.get("PACE_BATCH", "100"))
PACE_DELAY = float(os.environ.get("PACE_DELAY", "2.5"))
# Jitter only ever ADDS to the base delay: a fixed pace is the pattern that gets
# flagged, but going below the researched floor is worse.
PACE_JITTER_EXTRA = float(os.environ.get("PACE_JITTER_EXTRA", "1.5"))
PACE_MAX_MESSAGES = int(os.environ.get("PACE_MAX_MESSAGES", "1000"))
PACE_MAX_REQUESTS = int(os.environ.get("PACE_MAX_REQUESTS", "30"))
COOLDOWN_429 = float(os.environ.get("PACE_429_COOLDOWN", "60"))
GLOBAL_COOLDOWN = float(os.environ.get("PACE_GLOBAL_COOLDOWN", "900"))
MAX_429_STRIKES = int(os.environ.get("PACE_MAX_429_STRIKES", "3"))
POST_429_PAUSE = float(os.environ.get("PACE_POST_429_PAUSE", "600"))
POLL_SECONDS = float(os.environ.get("JOB_POLL_SECONDS", "2"))

_MESSAGES_RE = re.compile(r"(\d+) messages so far")
_COMPLETE_RE = re.compile(r"Backfill complete[^\d]{0,6}(\d+) messages")
_DISCOVER_RE = re.compile(r"discovered (\d+) servers, (\d+) text channels")
_DIGEST_RE = re.compile(r"(\d+) built, (\d+) reused")
_LIMIT_RE = re.compile(r"rate limited|429", re.IGNORECASE)
# A rejected token must stop the queue: every further attempt is an invalid
# request, and Discord bans on 10k invalid requests per 10 minutes.
_AUTH_RE = re.compile(r"\b401\b|unauthori[sz]ed|improper token|invalid token", re.I)

_stop = False
_auth_failed = False


def auth_failed() -> bool:
    """True once the token was rejected; the queue stays paused until restart."""
    return _auth_failed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jittered(delay: float) -> float:
    """Base delay plus uniform extra, never below the base."""
    return delay + random.uniform(0, PACE_JITTER_EXTRA)


async def _update(job_id: int, **fields) -> None:
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            update(Job).where(Job.id == job_id).values(updated_at=_now(), **fields)
        )
        await session.commit()


async def enqueue(kind: str, payload: dict, channel_id: int | None = None) -> int:
    factory = get_session_factory()
    async with factory() as session:
        job = Job(
            kind=kind,
            payload=payload,
            status=JobStatus.queued.value,
            phase="queued",
            message="",
            messages=0,
            channel_id=channel_id,
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job.id


async def reap_interrupted() -> int:
    """Jobs left 'running' by a restart can never finish; mark them failed."""
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(Job).where(Job.status.in_([JobStatus.running.value]))
                )
            )
            .scalars()
            .all()
        )
        for job in rows:
            job.status = JobStatus.error.value
            job.phase = "interrupted"
            job.message = "server restarted while this job was running"
            job.finished_at = _now()
        await session.commit()
        if rows:
            logger.warning(f"marked {len(rows)} interrupted job(s) as failed")
        return len(rows)


async def _claim() -> Job | None:
    factory = get_session_factory()
    async with factory() as session:
        job = (
            await session.execute(
                select(Job)
                .where(Job.status == JobStatus.queued.value)
                .order_by(Job.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        job.status = JobStatus.running.value
        job.phase = "starting"
        job.updated_at = _now()
        await session.commit()
        await session.refresh(job)
        session.expunge(job)
        return job


def build_args(job: Job) -> list[str]:
    """CLI arguments for a job, with the pacing policy applied."""
    payload = job.payload or {}
    delay = _jittered(PACE_DELAY)
    if job.kind == "capture":
        cap = min(int(payload.get("maxMessages") or PACE_MAX_MESSAGES), PACE_MAX_MESSAGES)
        args = [
            "backfill",
            "-c",
            str(payload["channelId"]),
            "--max-messages",
            str(cap),
            "--batch-size",
            str(PACE_BATCH),
            "--delay",
            f"{delay:.2f}",
        ]
        if payload.get("since"):
            args += ["--since", str(payload["since"])]
        if payload.get("until"):
            args += ["--until", str(payload["until"])]
        return args

    if job.kind == "digest":
        args = [
            "digest-range",
            "-c",
            str(payload["channelId"]),
            "--from",
            str(payload["from"]),
            "--period",
            str(payload.get("period") or "daily"),
        ]
        if payload.get("to"):
            args += ["--to", str(payload["to"])]
        if payload.get("force"):
            args += ["--force"]
        return args

    if job.kind == "discover":
        return ["discover", "--delay", f"{_jittered(1.0):.2f}"]

    raise ValueError(f"unknown job kind {job.kind!r}")


class RateLimitAbort(RuntimeError):
    """Raised when the run must stop: too many 429s, a global/CF block, or the
    per-run request budget being spent, or a rejected token."""


class RequestBudgetExceeded(RateLimitAbort):
    """The run hit PACE_MAX_REQUESTS before reaching its message cap."""


async def _run_process(job: Job, args: list[str]) -> tuple[int, str]:
    """Run the CLI, streaming progress into the job row and honouring 429s.

    Policy (from the rate-limit research in PATCHES.md): exponential spaced
    waits starting at retry_after + 1s buffer, hard stop after three consecutive
    429s, and an immediate abort plus a long pause on a global/Cloudflare block.
    """
    proc = await asyncio.create_subprocess_exec(
        "discord-recall",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    lines: list[str] = []
    cooldowns = 0
    rate_limited = False
    requests_est = 0
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        lines.append(line)
        messages = _MESSAGES_RE.search(line)
        if messages:
            fetched = int(messages.group(1))
            # Requests, not messages, are the scarce unit: a page is PACE_BATCH
            # messages, so bound the run by the researched request budget too.
            requests_est = fetched // max(1, PACE_BATCH) + 1
            if requests_est > PACE_MAX_REQUESTS:
                proc.kill()
                await _update(
                    job.id,
                    phase="aborted",
                    message=(
                        f"stopped at {requests_est} requests "
                        f"(budget {PACE_MAX_REQUESTS}/run); lower the cap or raise "
                        f"PACE_MAX_REQUESTS deliberately"
                    ),
                )
                logger.error(
                    f"job {job.id}: request budget exceeded ({requests_est} > {PACE_MAX_REQUESTS})"
                )
                raise RequestBudgetExceeded(f"{requests_est} requests > {PACE_MAX_REQUESTS}")
            await _update(
                job.id,
                phase="capturing",
                messages=fetched,
                message=line[-200:],
            )
        elif _AUTH_RE.search(line):
            global _auth_failed
            _auth_failed = True
            proc.kill()
            await _update(
                job.id,
                phase="token rejected",
                message="Discord rejected the token (401): queue paused, fix DISCORD_TOKEN and restart",
            )
            logger.error(f"job {job.id}: token rejected - pausing the queue")
            raise RateLimitAbort("token rejected (401) - queue paused")
        elif _LIMIT_RE.search(line):
            cooldowns += 1
            rate_limited = True
            global_block = bool(re.search(r"global|cloudflare|ban", line, re.I))
            retry_after = re.search(r"retry.?after[^0-9]{0,4}(\d+(?:\.\d+)?)", line, re.I)

            if cooldowns > MAX_429_STRIKES or global_block:
                proc.kill()
                reason = (
                    "global/Cloudflare rate-limit block" if global_block
                    else f"{cooldowns - 1} consecutive rate limits"
                )
                await _update(
                    job.id,
                    phase="aborted",
                    message=f"stopped: {reason}; pausing {int(GLOBAL_COOLDOWN / 60)}min",
                )
                logger.error(f"job {job.id}: aborting run ({reason})")
                await asyncio.sleep(GLOBAL_COOLDOWN)
                raise RateLimitAbort(reason)

            # exponential spaced waits, never shorter than retry_after + 1s buffer
            wait = COOLDOWN_429 * (2 ** (cooldowns - 1))
            if retry_after:
                wait = max(wait, min(float(retry_after.group(1)), 900) + 1.0)
            wait = min(wait, 900)
            await _update(
                job.id,
                phase="cooling down",
                message=f"rate limited (strike {cooldowns}); pausing {int(wait)}s",
            )
            logger.warning(f"job {job.id}: {line[-160:]} - sleeping {int(wait)}s")
            await asyncio.sleep(wait)
        else:
            tail = line.split(" - ", 1)[-1] if "|" in line else line
            await _update(job.id, message=tail[-200:])

    rc = await proc.wait()
    if rate_limited:
        lines.append("__RATE_LIMITED__")
    return rc or 0, "\n".join(lines)


async def _execute(job: Job) -> bool:
    """Run one job. Returns True when the run saw a rate limit."""
    args = build_args(job)
    logger.info(f"job {job.id} ({job.kind}) starting: {' '.join(args)}")
    await _update(job.id, phase="running", message=f"discord-recall {' '.join(args)}")
    try:
        rc, output = await _run_process(job, args)
    except RateLimitAbort as exc:
        await _update(
            job.id,
            status=JobStatus.error.value,
            phase="stopped",
            message=f"stopped to protect the token: {exc}",
            finished_at=_now(),
        )
        logger.error(f"job {job.id} aborted: {exc}")
        return True
    except Exception as exc:  # noqa: BLE001 - surfaced to the job row
        await _update(
            job.id,
            status=JobStatus.error.value,
            phase="error",
            message=str(exc)[:300],
            finished_at=_now(),
        )
        logger.exception(f"job {job.id} crashed")
        return False

    final = [line for line in output.splitlines() if line.strip()][-1:] or [""]
    summary = final[0].split(" - ", 1)[-1] if "|" in final[0] else final[0]
    # Prefer the completion line ("Backfill complete — N messages") over the last
    # progress line, which can lag one batch behind.
    complete = _COMPLETE_RE.search(output)
    progress = _MESSAGES_RE.search(output)
    discover = _DISCOVER_RE.search(output)
    if complete:
        total = int(complete.group(1))
    elif progress:
        total = int(progress.group(1))
    elif discover:
        total = int(discover.group(2))
    else:
        total = 0

    await _update(
        job.id,
        status=(JobStatus.done.value if rc == 0 else JobStatus.error.value),
        phase="done" if rc == 0 else "failed",
        message=summary[-300:],
        messages=total,
        finished_at=_now(),
    )
    logger.info(f"job {job.id} finished rc={rc}: {summary[-160:]}")
    return "__RATE_LIMITED__" in output


async def worker_loop() -> None:
    """Single worker: one Discord-touching job at a time, forever."""
    logger.info(
        f"job worker started (batch={PACE_BATCH} delay={PACE_DELAY}s"
        f"+0-{PACE_JITTER_EXTRA}s jitter cap={PACE_MAX_MESSAGES} msgs "
        f"budget={PACE_MAX_REQUESTS} requests/run max-429-strikes={MAX_429_STRIKES})"
    )
    while not _stop:
        try:
            if _auth_failed:
                # Never keep issuing requests with a rejected token.
                await asyncio.sleep(60)
                continue
            job = await _claim()
            if job is None:
                await asyncio.sleep(POLL_SECONDS)
                continue
            limited = await _execute(job)
            # A run that hit a 429 buys a long pause before the next one, so the
            # token is never run in a tight loop even across different channels.
            await asyncio.sleep(POST_429_PAUSE if limited else _jittered(3))
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # noqa: BLE001 - the loop must survive
            logger.exception("job worker iteration failed")
            await asyncio.sleep(POLL_SECONDS)


def stop() -> None:
    global _stop
    _stop = True
