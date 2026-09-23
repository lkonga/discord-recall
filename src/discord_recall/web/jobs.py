"""Background job queue for paced Discord work.

Long capture/digest runs are queued here and executed **one at a time** with
conservative pacing, so:

* an HTTP request never waits on a multi-minute backfill (the UI polls progress),
* a browser reload or a dropped connection cannot orphan the work,
* the token sees a steady, jittered request rate instead of bursts, and
* a 429 pauses everything rather than retrying immediately.

Pacing comes from the rate-limit research captured in PATCHES.md: small pages,
multi-second jittered delays, a hard per-run cap, sequential channels only.

Two more rows of that same policy - "<=60 requests/hour" and ">=10 min between
automated runs" - are enforced against the ``jobs`` table itself
(:func:`pacing_gate`), never from process memory, so a restart cannot forget
them. A gated job stays ``queued`` and visible; it is never dropped.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess  # noqa: F401 - asyncio.subprocess is not auto-imported
import math
import os
import random
import re
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select, update

from discord_recall.db import get_session_factory
from discord_recall.db.models import Job, JobStatus

# Evidence status per knob (see PATCHES.md section 7 for the quotes):
#   QUOTED  = a specific upstream value we match
#   MARGIN  = no upstream number exists; conservative engineering choice, with
#             the reasoning stated, and deliberately not claimed as "policy"
PACE_BATCH = int(os.environ.get("PACE_BATCH", "100"))  # QUOTED: DCE + py-self page at 100
PACE_DELAY = float(os.environ.get("PACE_DELAY", "2.5"))  # QUOTED-ish: Undiscord hand-tested floor ~2.1s
# Jitter only ever ADDS to the base delay: a fixed interval is the pattern called
# out in Undiscord #168, but going below the floor is worse. Range is MARGIN.
PACE_JITTER_EXTRA = float(os.environ.get("PACE_JITTER_EXTRA", "1.5"))  # MARGIN
PACE_MAX_MESSAGES = int(os.environ.get("PACE_MAX_MESSAGES", "1000"))  # MARGIN
PACE_MAX_REQUESTS = int(os.environ.get("PACE_MAX_REQUESTS", "30"))  # MARGIN
COOLDOWN_429 = float(os.environ.get("PACE_429_COOLDOWN", "60"))  # MARGIN (DCE only quotes the +1s buffer)
GLOBAL_COOLDOWN = float(os.environ.get("PACE_GLOBAL_COOLDOWN", "900"))  # MARGIN
MAX_429_STRIKES = int(os.environ.get("PACE_MAX_429_STRIKES", "3"))  # MARGIN (py-self's ceiling is 5 attempts)
POST_429_PAUSE = float(os.environ.get("PACE_POST_429_PAUSE", "600"))  # MARGIN
POLL_SECONDS = float(os.environ.get("JOB_POLL_SECONDS", "2"))

# The hourly and interval halves of PATCHES.md's "Max requests | <=30/run,
# <=60/h, >=10 min between automated runs" row. The per-run half lives in
# _run_process; these two are computed from the jobs table by pacing_gate().
PACE_MAX_REQUESTS_PER_HOUR = int(os.environ.get("PACE_MAX_REQUESTS_PER_HOUR", "60"))  # QUOTED
PACE_MIN_GAP_SECONDS = float(os.environ.get("PACE_MIN_GAP_SECONDS", "600"))  # QUOTED: 10 min
REQUEST_WINDOW_SECONDS = 3600.0  # the "per hour" the budget is measured over
MIN_GATE_SLEEP = 5.0  # a gated worker sleeps at least this long: it never spins
GATE_LOG_INTERVAL = 60.0  # one log line per gate per minute, not one per poll

_MESSAGES_RE = re.compile(r"(\d+) messages so far")
_COMPLETE_RE = re.compile(r"Backfill complete[^\d]{0,6}(\d+) messages")
_DISCOVER_RE = re.compile(r"discovered (\d+) servers, (\d+) text channels")
_DIGEST_RE = re.compile(r"(\d+) built, (\d+) reused")
# Matches "429", "rate limited" and py-self's own wording "Global rate limit has
# been hit." (the phrase has no "limited", so the older pattern missed it).
_LIMIT_RE = re.compile(r"rate ?limit|429", re.IGNORECASE)
# A rejected token must stop the queue: every further attempt is an invalid
# request, and Discord bans on 10k invalid requests per 10 minutes.
_AUTH_RE = re.compile(r"\b401\b|unauthori[sz]ed|improper token|invalid token", re.I)
# py-self raises on exactly this string: "Global rate limit has been hit."
_GLOBAL_RE = re.compile(r"global rate limit has been hit|global|cloudflare|ban", re.I)

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


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a stored timestamp.

    SQLite hands datetimes back naive, so a ``created_at``/``finished_at`` read
    from a row cannot be subtracted from ``_now()`` (aware) without this.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _knob(name: str, default: int | float) -> int | float:
    """A pacing knob, re-read from the env with the module constant as default.

    The constants above are the documented defaults (and the import-time parse of
    the same variable already rejects a malformed value loudly at startup).
    Reading the env again per call is what makes the two gates shippable with
    small limits in tests without a reload.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return type(default)(raw)
    except ValueError:
        return default


def _capture_cap(payload: dict) -> int:
    """Messages a capture run will ask for, already clamped to PACE_MAX_MESSAGES."""
    return min(int(payload.get("maxMessages") or PACE_MAX_MESSAGES), PACE_MAX_MESSAGES)


def job_requests(job: Job) -> int:
    """Discord HTTP requests ``job`` costs, measured from its own row alone.

    Requests, not messages, are the scarce unit (PATCHES.md section 7), and a
    queued row is the only thing the worker can read about work it has not run
    yet, so the estimate is deliberately taken at the run's *cap*: a job that
    finishes early is charged what it was allowed to spend, never less.

    * ``capture``  - ``ceil(messages / PACE_BATCH) + 1``. History is paged at
      PACE_BATCH messages per request, and the final page (short, or the empty
      one that ends the run) is a request of its own - the same ``+ 1`` the live
      estimator in ``_run_process`` uses. ``messages`` is the clamped cap, i.e.
      exactly what :func:`build_args` passes as ``--max-messages``.
    * ``discover`` - ``1 + guilds``: one ``/users/@me/guilds`` listing, then one
      ``/guilds/{id}/channels`` call per guild. Only guilds named in the payload
      (``--server``) can be counted from the row; an unqualified discovery cannot
      know how many guilds the token sees, so it is charged the single listing
      request and the hourly gate stays a documented lower bound for it.
    * ``digest``   - ``0``: a digest is LLM work and touches no Discord endpoint.
    * anything else - ``1``: an unrecognised kind must never be free.

    ``ceil`` rather than floor is deliberate: 250 messages at PACE_BATCH=100 is
    three pages plus the closing one, and rounding that down would hand the
    hourly budget requests it does not have.
    """
    payload = job.payload or {}
    if job.kind == "capture":
        return math.ceil(_capture_cap(payload) / max(1, PACE_BATCH)) + 1
    if job.kind == "discover":
        return 1 + len(payload.get("servers") or [])
    if job.kind == "digest":
        return 0
    return 1


# (reason, when) of the last active-gate log line; see _log_gate.
_gate_log: tuple[str, datetime] | None = None


def _log_gate(reason: str, wait: float, now: datetime) -> None:
    """Log an active gate, at most once per interval instead of once per poll."""
    global _gate_log
    if _gate_log is not None and _gate_log[0] == reason:
        if (now - _gate_log[1]).total_seconds() < GATE_LOG_INTERVAL:
            return  # this gate was explained a moment ago
    _gate_log = (reason, now)
    logger.info(f"pacing gate: {reason}; sleeping {int(wait)}s - queued jobs stay queued")


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


async def requests_in_window(start: datetime) -> tuple[int, datetime | None]:
    """Discord requests the jobs created since ``start`` cost, and the oldest of them.

    Every row counts whatever its status: a queued job will spend its requests
    when it is claimed, and a failed or aborted run has already spent some. The
    oldest ``created_at`` is returned too, because that is the row whose expiry
    releases the hourly gate.
    """
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(Job).where(Job.created_at >= start).order_by(Job.created_at)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        return 0, None
    return sum(job_requests(job) for job in rows), _as_utc(rows[0].created_at)


async def seconds_since_last_finish(now: datetime | None = None) -> float | None:
    """Seconds since the newest ``finished_at``, or None when nothing has finished."""
    factory = get_session_factory()
    async with factory() as session:
        newest = (
            await session.execute(
                select(Job.finished_at)
                .where(Job.finished_at.is_not(None))
                .order_by(Job.finished_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if newest is None:
        return None
    return ((now or _now()) - _as_utc(newest)).total_seconds()


async def pacing_gate(now: datetime | None = None) -> tuple[float, str]:
    """Seconds the worker must wait before claiming anything, and why.

    ``(0.0, "")`` means clear to claim. Two PATCHES.md rows are enforced here,
    both read from the jobs table so they survive a restart:

    * **hourly budget** - the sum of :func:`job_requests` over the rows created in
      the last ``REQUEST_WINDOW_SECONDS`` must stay below
      ``PACE_MAX_REQUESTS_PER_HOUR`` (env, default 60). When it is spent, the wait
      is until the oldest charged row ages out of the window, so the gate clears
      by itself instead of polling.
    * **minimum gap** - at least ``PACE_MIN_GAP_SECONDS`` (env, default 600) must
      have passed since the newest ``finished_at``. This replaces an in-process
      breather, which a restart would have forgotten.

    Nothing is dropped: a gated job stays ``queued`` and visible, and the wait
    never goes below ``MIN_GATE_SLEEP`` so a gated worker cannot spin.
    """
    global _gate_log
    now = now or _now()
    budget = int(_knob("PACE_MAX_REQUESTS_PER_HOUR", PACE_MAX_REQUESTS_PER_HOUR))
    gap = float(_knob("PACE_MIN_GAP_SECONDS", PACE_MIN_GAP_SECONDS))

    wait = 0.0
    reason = ""

    used, oldest = await requests_in_window(now - timedelta(seconds=REQUEST_WINDOW_SECONDS))
    if used >= budget:
        release = (oldest or now) + timedelta(seconds=REQUEST_WINDOW_SECONDS)
        wait = max(wait, (release - now).total_seconds())
        reason = f"hourly budget spent: {used}/{budget} Discord requests in the last hour"

    since_finish = await seconds_since_last_finish(now)
    if since_finish is not None and since_finish < gap:
        remaining = gap - since_finish
        if remaining > wait:
            # Whatever binds harder is what gets reported.
            wait = remaining
            reason = (
                f"minimum gap between automated runs: only {int(since_finish)}s of "
                f"{int(gap)}s elapsed since the last finished job"
            )

    if wait <= 0:
        _gate_log = None  # the episode is over; the next gate logs immediately
        return 0.0, ""
    return max(MIN_GATE_SLEEP, wait), reason


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
        cap = _capture_cap(payload)
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
        args = ["discover", "--delay", f"{_jittered(1.0):.2f}"]
        for gid in payload.get("servers") or []:
            args += ["--server", str(gid)]
        return args

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
    # One "N messages so far" line is emitted per page fetch, so this is a real
    # request count rather than an estimate derived from the message count.
    requests = 0
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        lines.append(line)
        messages = _MESSAGES_RE.search(line)
        if messages:
            fetched = int(messages.group(1))
            requests += 1
            # A page landing proves we are not in a hot loop, so consecutive-429
            # bookkeeping restarts here.
            cooldowns = 0

            # The CLI clamps the requested cap, but the parent enforces it too:
            # a changed default or a future caller must not be able to overrun it.
            if fetched > PACE_MAX_MESSAGES:
                proc.kill()
                await _update(
                    job.id,
                    phase="aborted",
                    message=(
                        f"stopped after {fetched} messages, above the "
                        f"PACE_MAX_MESSAGES ceiling ({PACE_MAX_MESSAGES})"
                    ),
                )
                logger.error(f"job {job.id}: message cap exceeded ({fetched})")
                raise RequestBudgetExceeded(f"{fetched} messages > {PACE_MAX_MESSAGES}")

            if requests > PACE_MAX_REQUESTS:
                proc.kill()
                await _update(
                    job.id,
                    phase="aborted",
                    message=(
                        f"stopped at {requests} requests (budget "
                        f"{PACE_MAX_REQUESTS}/run); lower the cap or raise "
                        f"PACE_MAX_REQUESTS deliberately"
                    ),
                )
                logger.error(
                    f"job {job.id}: request budget exceeded ({requests} > {PACE_MAX_REQUESTS})"
                )
                raise RequestBudgetExceeded(f"{requests} requests > {PACE_MAX_REQUESTS}")

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
            global_block = bool(_GLOBAL_RE.search(line))
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

            if not retry_after:
                # Upstream rule (discord-userdoccers rate-limits.mdx): with no
                # Retry-After header you must not retry programmatically.
                proc.kill()
                await _update(
                    job.id,
                    phase="aborted",
                    message=(
                        "stopped: 429 without a Retry-After header - not retried "
                        "by policy; check the token before running again"
                    ),
                )
                logger.error(f"job {job.id}: 429 with no Retry-After - aborting")
                raise RateLimitAbort("429 without Retry-After")

            # exponential spaced waits, never shorter than retry_after + 1s buffer
            wait = COOLDOWN_429 * (2 ** (cooldowns - 1))
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

    # Exit code 3 is the capture path's "this channel is not readable" (403):
    # a dropped channel, not a retryable failure.
    if rc == 3:
        await _update(
            job.id,
            status=JobStatus.error.value,
            phase="dropped",
            message=summary[-300:] or "channel dropped: not readable by this account (403)",
            messages=total,
            finished_at=_now(),
        )
        logger.warning(f"job {job.id} dropped a channel: {summary[-120:]}")
        return False

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
    """Single worker: one Discord-touching job at a time, forever.

    Every iteration runs the pacing gates before claiming anything, so the hourly
    request budget and the minimum gap hold across restarts, and a job that is
    gated out simply stays queued instead of being dropped.
    """
    logger.info(
        f"job worker started (batch={PACE_BATCH} delay={PACE_DELAY}s"
        f"+0-{PACE_JITTER_EXTRA}s jitter cap={PACE_MAX_MESSAGES} msgs "
        f"budget={PACE_MAX_REQUESTS} requests/run "
        f"{PACE_MAX_REQUESTS_PER_HOUR}/h gap>={int(PACE_MIN_GAP_SECONDS)}s "
        f"max-429-strikes={MAX_429_STRIKES})"
    )
    while not _stop:
        try:
            if _auth_failed:
                # Never keep issuing requests with a rejected token.
                await asyncio.sleep(60)
                continue
            wait, reason = await pacing_gate()
            if wait > 0:
                _log_gate(reason, wait, _now())
                await asyncio.sleep(wait)
                continue
            job = await _claim()
            if job is None:
                await asyncio.sleep(POLL_SECONDS)
                continue
            limited = await _execute(job)
            # A run that hit a 429 buys a long pause before the next one, so the
            # token is never run in a tight loop even across different channels.
            # The ordinary breather is not a sleep here any more: pacing_gate()
            # enforces the minimum gap from the persisted finished_at instead.
            if limited:
                await asyncio.sleep(POST_429_PAUSE)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # noqa: BLE001 - the loop must survive
            logger.exception("job worker iteration failed")
            await asyncio.sleep(POLL_SECONDS)


def stop() -> None:
    global _stop
    _stop = True
