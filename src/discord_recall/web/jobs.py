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

PACE_BATCH = int(os.environ.get("PACE_BATCH", "50"))
PACE_DELAY = float(os.environ.get("PACE_DELAY", "2.5"))
PACE_JITTER = float(os.environ.get("PACE_JITTER", "0.3"))
PACE_MAX_MESSAGES = int(os.environ.get("PACE_MAX_MESSAGES", "1000"))
COOLDOWN_429 = float(os.environ.get("PACE_429_COOLDOWN", "90"))
POLL_SECONDS = float(os.environ.get("JOB_POLL_SECONDS", "2"))

_MESSAGES_RE = re.compile(r"(\d+) messages so far")
_DISCOVER_RE = re.compile(r"discovered (\d+) servers, (\d+) text channels")
_DIGEST_RE = re.compile(r"(\d+) built, (\d+) reused")
_LIMIT_RE = re.compile(r"rate limited|429", re.IGNORECASE)

_stop = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jittered(delay: float) -> float:
    return max(0.5, delay * (1 + random.uniform(-PACE_JITTER, PACE_JITTER)))


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


async def _run_process(job: Job, args: list[str]) -> tuple[int, str]:
    """Run the CLI, streaming progress into the job row and honouring 429s."""
    proc = await asyncio.create_subprocess_exec(
        "discord-recall",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    lines: list[str] = []
    cooldowns = 0
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        lines.append(line)
        messages = _MESSAGES_RE.search(line)
        if messages:
            await _update(
                job.id,
                phase="capturing",
                messages=int(messages.group(1)),
                message=line[-200:],
            )
        elif _LIMIT_RE.search(line):
            cooldowns += 1
            wait = min(COOLDOWN_429 * cooldowns, 900)
            await _update(
                job.id,
                phase="cooling down",
                message=f"rate limited (strike {cooldowns}); pausing {int(wait)}s",
            )
            logger.warning(f"job {job.id}: {line[-160:]} - sleeping {int(wait)}s")
            await asyncio.sleep(wait)
            retry_after = re.search(r"retry.?after[^0-9]{0,4}(\d+(?:\.\d+)?)", line, re.I)
            if retry_after:
                await asyncio.sleep(min(float(retry_after.group(1)), 900))
        else:
            tail = line.split(" - ", 1)[-1] if "|" in line else line
            await _update(job.id, message=tail[-200:])

    rc = await proc.wait()
    return rc or 0, "\n".join(lines)


async def _execute(job: Job) -> None:
    args = build_args(job)
    logger.info(f"job {job.id} ({job.kind}) starting: {' '.join(args)}")
    await _update(job.id, phase="running", message=f"discord-recall {' '.join(args)}")
    try:
        rc, output = await _run_process(job, args)
    except Exception as exc:  # noqa: BLE001 - surfaced to the job row
        await _update(
            job.id,
            status=JobStatus.error.value,
            phase="error",
            message=str(exc)[:300],
            finished_at=_now(),
        )
        logger.exception(f"job {job.id} crashed")
        return

    final = [line for line in output.splitlines() if line.strip()][-1:] or [""]
    summary = final[0].split(" - ", 1)[-1] if "|" in final[0] else final[0]
    messages = _MESSAGES_RE.search(output)
    total = int(messages.group(1)) if messages else int(_DISCOVER_RE.search(output).group(2)) if _DISCOVER_RE.search(output) else 0

    await _update(
        job.id,
        status=(JobStatus.done.value if rc == 0 else JobStatus.error.value),
        phase="done" if rc == 0 else "failed",
        message=summary[-300:],
        messages=total,
        finished_at=_now(),
    )
    logger.info(f"job {job.id} finished rc={rc}: {summary[-160:]}")


async def worker_loop() -> None:
    """Single worker: one Discord-touching job at a time, forever."""
    logger.info(
        f"job worker started (batch={PACE_BATCH} delay={PACE_DELAY}s "
        f"jitter=±{int(PACE_JITTER * 100)}% cap={PACE_MAX_MESSAGES})"
    )
    while not _stop:
        try:
            job = await _claim()
            if job is None:
                await asyncio.sleep(POLL_SECONDS)
                continue
            await _execute(job)
            # small breather between jobs keeps bursts from ever forming
            await asyncio.sleep(_jittered(3))
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # noqa: BLE001 - the loop must survive
            logger.exception("job worker iteration failed")
            await asyncio.sleep(POLL_SECONDS)


def stop() -> None:
    global _stop
    _stop = True
