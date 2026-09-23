"""Policy tests for the two pacing guards that were missing from `web.jobs`.

PATCHES.md section 7 ships one row for requests - "Max requests | <=30/run,
<=60/h, >=10 min between automated runs". Only the per-run third was enforced
(inside `_run_process`). These tests cover the other two:

* the hourly request budget, summed from the `jobs` rows created in the last hour
  (`job_requests` + `requests_in_window` + `pacing_gate`), and
* the minimum gap since the newest `finished_at` (`seconds_since_last_finish`),
  which replaces the old fixed post-job breather.

Both limits are computed from persisted rows, so a restart cannot forget them,
and a gated job is never dropped: it stays `queued` and visible. `pacing_gate`
runs before every `_claim` in the worker loop, which is what the last few tests
drive end to end.

Everything is offline: the database is a throwaway SQLite file, `_execute` is
stubbed so no CLI is spawned, and `asyncio.sleep` is a recorder that stops the
loop after the sleeps under test. No Discord, no network, no real waiting.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

import pytest
from loguru import logger as loguru_logger
from sqlalchemy import select, update

from discord_recall.db import get_session_factory
from discord_recall.db.models import Base, Job, JobStatus
from discord_recall.web import jobs

# --------------------------------------------------------------------------
# Offline harness: throwaway SQLite store, stubbed run, recorded sleeps
# --------------------------------------------------------------------------


@pytest.fixture()
def job_db(tmp_path, monkeypatch):
    """A throwaway SQLite store holding the real `jobs` table."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pacing.db'}")
    from discord_recall.config import get_settings

    import discord_recall.db as db_module

    get_settings.cache_clear()
    db_module._engine = None
    db_module._session_factory = None
    engine = db_module.get_engine()

    async def create_schema():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    yield get_session_factory()
    asyncio.run(engine.dispose())
    db_module._engine = None
    db_module._session_factory = None
    get_settings.cache_clear()


class _LoopStop(BaseException):
    """Ends `worker_loop`. It must not be an `Exception`: the loop swallows those."""


class _WorkerProbe:
    """Drives the real worker loop with a stubbed run and a recorded sleep."""

    def __init__(self, stop_after_sleeps: int = 1):
        self.sleeps: list[float] = []
        self.claim_calls = 0
        self.claimed: list[Job] = []
        self.executed: list[Job] = []
        self.stop_after_sleeps = stop_after_sleeps

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        if len(self.sleeps) >= self.stop_after_sleeps:
            raise _LoopStop()

    async def execute(self, job: Job) -> bool:
        self.executed.append(job)
        return False  # no 429 in these tests, so no post-429 pause either

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_claim = jobs._claim

        async def counting_claim():
            self.claim_calls += 1
            job = await real_claim()  # the real claim path, real database writes
            if job is not None:
                self.claimed.append(job)
            return job

        monkeypatch.setattr(jobs.asyncio, "sleep", self.sleep)
        monkeypatch.setattr(jobs, "_claim", counting_claim)
        monkeypatch.setattr(jobs, "_execute", self.execute)
        monkeypatch.setattr(jobs, "_stop", False)
        monkeypatch.setattr(jobs, "_auth_failed", False)

    def run(self) -> None:
        with pytest.raises(_LoopStop):
            asyncio.run(jobs.worker_loop())


def _job(payload: dict | None = None, kind: str = "capture") -> Job:
    """A detached Job, the way `job_requests` sees one."""
    job = Job(kind=kind, payload={"channelId": 111} if payload is None else payload)
    job.id = 7
    return job


def _add_job(
    factory,
    *,
    kind: str = "capture",
    payload: dict | None = None,
    status: str = "done",
    created_seconds_ago: float = 0.0,
    finished_seconds_ago: float | None = None,
    messages: int = 0,
) -> int:
    """Insert one persisted job row. Ages are seconds in the past, relative to now."""
    now = datetime.now(timezone.utc)
    created = now - timedelta(seconds=created_seconds_ago)
    finished = None if finished_seconds_ago is None else now - timedelta(seconds=finished_seconds_ago)

    async def go() -> int:
        async with factory() as session:
            job = Job(
                kind=kind,
                payload={"channelId": 111} if payload is None else payload,
                status=status,
                phase="done" if status != JobStatus.queued.value else "queued",
                message="",
                messages=messages,
                channel_id=111,
                created_at=created,
                updated_at=created,
                finished_at=finished,
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job.id

    return asyncio.run(go())


def _set_times(factory, job_id: int, *, created_seconds_ago=None, finished_seconds_ago=None) -> None:
    """Re-age a persisted row: the gates must see the change and no memory of the old value."""
    now = datetime.now(timezone.utc)
    values: dict = {}
    if created_seconds_ago is not None:
        values["created_at"] = now - timedelta(seconds=created_seconds_ago)
    if finished_seconds_ago is not None:
        values["finished_at"] = now - timedelta(seconds=finished_seconds_ago)

    async def go() -> None:
        async with factory() as session:
            await session.execute(update(Job).where(Job.id == job_id).values(**values))
            await session.commit()

    asyncio.run(go())


def _statuses(factory) -> dict[int, str]:
    async def go() -> dict[int, str]:
        async with factory() as session:
            rows = (await session.execute(select(Job))).scalars().all()
            return {row.id: row.status for row in rows}

    return asyncio.run(go())


def _gate() -> tuple[float, str]:
    return asyncio.run(jobs.pacing_gate())


def _used_last_hour() -> int:
    used, _ = asyncio.run(jobs.requests_in_window(datetime.now(timezone.utc) - timedelta(hours=1)))
    return used


# --------------------------------------------------------------------------
# 1. job_requests: the cost of a job, read off its own row
# --------------------------------------------------------------------------


@pytest.mark.parametrize("messages", [1, 99, 100, 101, 250, 1000])
def test_job_requests_capture_is_pages_plus_the_closing_request(messages):
    batch = jobs.PACE_BATCH
    assert jobs.job_requests(_job({"channelId": 111, "maxMessages": messages})) == (
        math.ceil(messages / batch) + 1
    )


def test_job_requests_capture_uses_the_same_cap_and_page_size_as_build_args():
    """The estimate must describe the run that build_args will actually start."""
    payload = {"channelId": 111, "maxMessages": 400}
    args = jobs.build_args(_job(payload))
    cap = int(args[args.index("--max-messages") + 1])
    batch = int(args[args.index("--batch-size") + 1])
    assert jobs.job_requests(_job(payload)) == math.ceil(cap / batch) + 1

    # no cap asked for -> the PACE_MAX_MESSAGES policy cap is what is charged
    assert jobs.job_requests(_job({"channelId": 111})) == (
        math.ceil(jobs.PACE_MAX_MESSAGES / jobs.PACE_BATCH) + 1
    )
    # a request above the cap is clamped before it is charged
    assert jobs.job_requests(_job({"channelId": 111, "maxMessages": jobs.PACE_MAX_MESSAGES * 50})) == (
        jobs.job_requests(_job({"channelId": 111}))
    )


def test_job_requests_capture_rounds_a_partial_page_up():
    """Floor would under-charge: 250 messages at batch 100 is 3 pages plus the closer."""
    assert jobs.PACE_BATCH == 100
    assert jobs.job_requests(_job({"channelId": 111, "maxMessages": 250})) == 4


def test_job_requests_discover_is_one_request_plus_one_per_guild():
    assert jobs.job_requests(_job({"servers": [1, 2, 3]}, kind="discover")) == 4
    assert jobs.job_requests(_job({"servers": []}, kind="discover")) == 1
    assert jobs.job_requests(_job({}, kind="discover")) == 1  # guild list only


def test_job_requests_digest_costs_no_discord_requests():
    digest = _job({"channelId": 111, "from": "2026-09-01", "period": "daily"}, kind="digest")
    assert jobs.job_requests(digest) == 0


def test_job_requests_charges_an_unknown_kind_instead_of_free():
    assert jobs.job_requests(_job({}, kind="something-new")) == 1


# --------------------------------------------------------------------------
# 2. The hourly budget gate
# --------------------------------------------------------------------------


def test_under_budget_claims_immediately(job_db, monkeypatch):
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "60")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "0")
    queued_id = _add_job(job_db, status="queued", created_seconds_ago=0)

    assert _gate() == (0.0, "")  # nothing spent, no finished job, nothing to wait for

    probe = _WorkerProbe()
    probe.install(monkeypatch)
    probe.run()

    assert [job.id for job in probe.claimed] == [queued_id]  # claimed straight away
    assert [job.id for job in probe.executed] == [queued_id]
    assert probe.sleeps == [jobs.POLL_SECONDS]  # the only pause is the idle poll
    assert _statuses(job_db)[queued_id] == JobStatus.running.value  # really claimed


def test_over_budget_does_not_claim_and_keeps_the_job_queued(job_db, monkeypatch):
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "4")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "0")
    charged = {"channelId": 111, "maxMessages": 100}  # 2 requests each
    for _ in range(4):  # 8 requests spent by finished runs
        _add_job(job_db, payload=charged, created_seconds_ago=120, finished_seconds_ago=60)
    queued_id = _add_job(job_db, payload=charged, status="queued", created_seconds_ago=0)
    # the queued job is charged too: a job that will run inside the hour will spend
    assert _used_last_hour() == 10

    wait, reason = _gate()
    assert wait >= jobs.MIN_GATE_SLEEP
    assert wait <= jobs.REQUEST_WINDOW_SECONDS  # it waits for a row to age out, not forever
    assert "hourly budget spent: 10/4" in reason

    probe = _WorkerProbe()
    probe.install(monkeypatch)
    probe.run()

    assert probe.claim_calls == 0  # never reached a claim
    assert probe.executed == []
    assert probe.sleeps and probe.sleeps[0] >= jobs.MIN_GATE_SLEEP
    assert _statuses(job_db)[queued_id] == JobStatus.queued.value  # still queued, not dropped


def test_hourly_budget_counts_only_rows_created_in_the_last_hour(job_db, monkeypatch):
    """Same rows, only their stored `created_at` changes: the window is the row's."""
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "4")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "0")
    ids = [
        _add_job(
            job_db,
            payload={"channelId": 111, "maxMessages": 100},
            created_seconds_ago=60,
            finished_seconds_ago=30,
        )
        for _ in range(4)
    ]
    wait, reason = _gate()
    assert wait >= jobs.MIN_GATE_SLEEP and "hourly budget" in reason

    for job_id in ids:  # an hour later the very same jobs cost nothing
        _set_times(job_db, job_id, created_seconds_ago=7200)
    assert _used_last_hour() == 0
    assert _gate() == (0.0, "")


def test_budget_gate_sleeps_the_remainder_of_the_window_then_clears(job_db, monkeypatch):
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "1")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "0")
    job_id = _add_job(
        job_db,
        payload={"channelId": 111, "maxMessages": 1},  # 2 requests, over a 1-request budget
        created_seconds_ago=3500,
        finished_seconds_ago=3400,
    )
    wait, _ = _gate()
    assert 90 <= wait <= 101  # ~100s until that row leaves the window

    _set_times(job_db, job_id, created_seconds_ago=3601)
    assert _gate() == (0.0, "")


def test_a_gated_worker_never_sleeps_less_than_five_seconds(job_db, monkeypatch):
    """Even when the budget frees up in a fraction of a second, it must not spin."""
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "1")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "0")
    _add_job(
        job_db,
        payload={"channelId": 111, "maxMessages": 1},
        created_seconds_ago=3599.9,
        finished_seconds_ago=3500,
    )
    wait, reason = _gate()
    assert wait == jobs.MIN_GATE_SLEEP
    assert "hourly budget" in reason


def test_discover_jobs_are_charged_one_request_per_guild(job_db, monkeypatch):
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "4")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "0")
    _add_job(job_db, kind="discover", payload={"servers": [1, 2, 3]}, created_seconds_ago=30)

    assert _used_last_hour() == 4  # 1 listing + 3 guilds
    wait, reason = _gate()
    assert wait >= jobs.MIN_GATE_SLEEP and "hourly budget" in reason


def test_digest_jobs_do_not_consume_the_discord_budget(job_db, monkeypatch):
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "1")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "0")
    for _ in range(10):
        _add_job(
            job_db,
            kind="digest",
            payload={"channelId": 111, "from": "2026-09-01"},
            created_seconds_ago=30,
            finished_seconds_ago=20,
        )
    assert _used_last_hour() == 0
    assert _gate() == (0.0, "")


# --------------------------------------------------------------------------
# 3. The minimum gap gate
# --------------------------------------------------------------------------


def test_min_gap_blocks_a_claim_right_after_a_finished_job(job_db, monkeypatch):
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "1000")  # the budget is not under test
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "600")
    _add_job(job_db, created_seconds_ago=300, finished_seconds_ago=30)
    queued_id = _add_job(job_db, status="queued", created_seconds_ago=0)

    wait, reason = _gate()
    assert 560 <= wait <= 571  # ~570s of the 600s gap left
    assert "minimum gap between automated runs" in reason

    probe = _WorkerProbe()
    probe.install(monkeypatch)
    probe.run()

    assert probe.claim_calls == 0
    assert probe.executed == []
    assert probe.sleeps[0] >= jobs.MIN_GATE_SLEEP
    assert _statuses(job_db)[queued_id] == JobStatus.queued.value


def test_min_gap_allows_the_claim_once_the_row_is_old_enough(job_db, monkeypatch):
    """Only the stored `finished_at` changes: no in-process state is reset, so this
    is exactly what a restart sees."""
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "1000")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "600")
    finished_id = _add_job(job_db, created_seconds_ago=900, finished_seconds_ago=5)
    queued_id = _add_job(job_db, status="queued", created_seconds_ago=0)

    wait, reason = _gate()
    assert wait >= 590 and "minimum gap" in reason

    _set_times(job_db, finished_id, finished_seconds_ago=601)
    assert _gate() == (0.0, "")

    probe = _WorkerProbe()
    probe.install(monkeypatch)
    probe.run()
    assert [job.id for job in probe.claimed] == [queued_id]


def test_min_gap_ignores_a_row_that_never_finished(job_db, monkeypatch):
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "1000")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "600")
    _add_job(job_db, status="error", created_seconds_ago=30)  # no finished_at at all
    _add_job(job_db, status="queued", created_seconds_ago=0)
    assert asyncio.run(jobs.seconds_since_last_finish()) is None
    assert _gate() == (0.0, "")


def test_min_gap_uses_the_newest_finish_when_several_jobs_have_run(job_db, monkeypatch):
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "1000")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "600")
    _add_job(job_db, created_seconds_ago=3000, finished_seconds_ago=2400)
    _add_job(job_db, created_seconds_ago=200, finished_seconds_ago=200)
    _add_job(job_db, created_seconds_ago=60, finished_seconds_ago=45)

    seconds = asyncio.run(jobs.seconds_since_last_finish())
    assert 40 <= seconds <= 50
    wait, reason = _gate()
    assert 550 <= wait <= 561 and "minimum gap" in reason


# --------------------------------------------------------------------------
# 4. Both knobs are env-overridable at gate time
# --------------------------------------------------------------------------


def test_env_knobs_override_both_gates(job_db, monkeypatch):
    finished_id = _add_job(job_db, created_seconds_ago=60, finished_seconds_ago=60)
    queued_id = _add_job(job_db, status="queued", created_seconds_ago=0)

    # a 1-request hourly budget makes the two 11-request jobs above too expensive
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "1")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "0")
    wait, reason = _gate()
    assert wait >= jobs.MIN_GATE_SLEEP and "hourly budget spent: 22/1" in reason

    # the default budget is far above what was spent, and now the gap binds
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", str(jobs.PACE_MAX_REQUESTS_PER_HOUR))
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "120")
    wait, reason = _gate()
    assert 50 <= wait <= 61 and "minimum gap" in reason
    assert _statuses(job_db)[queued_id] == JobStatus.queued.value

    # 60s since the finish is far enough for a 10s gap
    _set_times(job_db, finished_id, finished_seconds_ago=60)
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "10")
    assert _gate() == (0.0, "")


def test_a_malformed_env_knob_falls_back_to_the_module_default(monkeypatch):
    assert jobs._knob("PACE_MIN_GAP_SECONDS", jobs.PACE_MIN_GAP_SECONDS) == 600.0
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "")
    assert jobs._knob("PACE_MIN_GAP_SECONDS", jobs.PACE_MIN_GAP_SECONDS) == 600.0
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "not-a-number")
    assert jobs._knob("PACE_MIN_GAP_SECONDS", jobs.PACE_MIN_GAP_SECONDS) == 600.0
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "30")
    assert jobs._knob("PACE_MIN_GAP_SECONDS", jobs.PACE_MIN_GAP_SECONDS) == 30.0


# --------------------------------------------------------------------------
# 5. Logging: a long gate is visible, not chatty
# --------------------------------------------------------------------------


def test_a_gate_is_logged_once_per_interval_not_once_per_poll(monkeypatch):
    lines: list[str] = []
    sink = loguru_logger.add(lambda message: lines.append(message), level="INFO")
    try:
        monkeypatch.setattr(jobs, "_gate_log", None)
        now = datetime.now(timezone.utc)
        for _ in range(50):  # 50 polls inside the same minute
            jobs._log_gate("hourly budget spent: 60/60", 3540.0, now)
        assert sum("pacing gate" in line for line in lines) == 1

        jobs._log_gate("hourly budget spent: 60/60", 3480.0, now + timedelta(seconds=61))
        assert sum("pacing gate" in line for line in lines) == 2
        assert "queued jobs stay queued" in lines[-1]

        # a different gate is a different story and logs right away
        jobs._log_gate("minimum gap between automated runs", 300.0, now + timedelta(seconds=62))
        assert sum("pacing gate" in line for line in lines) == 3
    finally:
        loguru_logger.remove(sink)


def test_clearing_the_gate_resets_the_log_interval(job_db, monkeypatch):
    """A cleared gate ends the episode, so the next one is logged at once instead
    of being swallowed by the once-per-minute rule."""
    monkeypatch.setenv("PACE_MAX_REQUESTS_PER_HOUR", "1")
    monkeypatch.setenv("PACE_MIN_GAP_SECONDS", "0")
    job_id = _add_job(job_db, payload={"channelId": 111, "maxMessages": 1}, created_seconds_ago=30)

    wait, reason = _gate()
    assert wait >= jobs.MIN_GATE_SLEEP
    monkeypatch.setattr(jobs, "_gate_log", (reason, datetime.now(timezone.utc)))  # just logged

    _set_times(job_db, job_id, created_seconds_ago=3601)  # the budget frees up
    assert _gate() == (0.0, "")
    assert jobs._gate_log is None  # episode over

    _set_times(job_db, job_id, created_seconds_ago=30)  # gated again, same reason
    wait, reason = _gate()
    assert wait >= jobs.MIN_GATE_SLEEP

    lines: list[str] = []
    sink = loguru_logger.add(lambda message: lines.append(message), level="INFO")
    try:
        jobs._log_gate(reason, wait, datetime.now(timezone.utc))  # what worker_loop does
        assert sum("pacing gate" in line for line in lines) == 1  # not suppressed
    finally:
        loguru_logger.remove(sink)
