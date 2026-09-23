"""Enforcement tests for the four audit gaps closed in jobs.py.

Each test pins one behaviour an independent conformance audit flagged as MISSING
or PARTIAL, so a regression is visible:

* parent-side enforcement of the per-run message cap (the CLI clamps its own
  argument, but a caller could still ask for more),
* request accounting from real page fetches rather than messages/ page-size,
* the consecutive-429 counter resetting once a page lands, and
* py-self's own global-limit wording being recognised.

Offline by construction: the subprocess is faked, sleeps are recorded, and the
job-row updates are captured - no Discord, no network, no database.
"""

from __future__ import annotations

import asyncio

import pytest

from discord_recall.db.models import Job
from discord_recall.web import jobs


class _FakeProc:
    def __init__(self, lines: list[str]):
        self.stdout = self._stream(lines)
        self.killed = False

    async def _stream(self, lines: list[str]):
        for line in lines:
            yield line.encode()

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return 0


class _Harness:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.sleeps: list[float] = []
        self.updates: list[tuple[int, dict]] = []
        self.proc: _FakeProc | None = None

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)

    async def spawn(self, *_argv, **_kwargs) -> _FakeProc:
        self.proc = _FakeProc(self.lines)
        return self.proc

    async def update(self, job_id: int, **fields) -> None:
        self.updates.append((job_id, fields))

    def messages(self) -> list[str]:
        return [f.get("message", "") for _, f in self.updates if "message" in f]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jobs.asyncio, "sleep", self.sleep)
        monkeypatch.setattr(jobs.asyncio, "create_subprocess_exec", self.spawn)
        monkeypatch.setattr(jobs, "_update", self.update)


def _job() -> Job:
    job = Job(kind="capture", payload={"channelId": 111})
    job.id = 7
    return job


def _progress(n: int) -> str:
    return f"2026-09-24 00:00:00 | INFO | x - [chan] {n} messages so far..."


def _run(monkeypatch, lines: list[str]) -> _Harness:
    harness = _Harness(lines)
    harness.install(monkeypatch)
    asyncio.run(jobs._run_process(_job(), ["backfill"]))
    return harness


def test_parent_enforces_the_message_cap(monkeypatch):
    monkeypatch.setattr(jobs, "PACE_MAX_MESSAGES", 150)
    monkeypatch.setattr(jobs, "PACE_MAX_REQUESTS", 99)
    harness = _Harness([_progress(100), _progress(200)])
    harness.install(monkeypatch)
    with pytest.raises(jobs.RequestBudgetExceeded):
        asyncio.run(jobs._run_process(_job(), ["backfill"]))
    assert harness.proc is not None and harness.proc.killed
    assert any("PACE_MAX_MESSAGES" in m for m in harness.messages())


def test_request_budget_counts_real_fetches_not_messages(monkeypatch):
    """3 page fetches with a budget of 2 must abort even though no message cap is hit."""
    monkeypatch.setattr(jobs, "PACE_MAX_MESSAGES", 1_000_000)
    monkeypatch.setattr(jobs, "PACE_MAX_REQUESTS", 2)
    harness = _Harness([_progress(1), _progress(2), _progress(3)])
    harness.install(monkeypatch)
    with pytest.raises(jobs.RequestBudgetExceeded):
        asyncio.run(jobs._run_process(_job(), ["backfill"]))
    assert any("3 requests" in m for m in harness.messages())


def test_consecutive_429_counter_resets_after_a_page_lands(monkeypatch):
    """With a 1-strike budget, a 429 -> progress -> 429 sequence must not abort."""
    monkeypatch.setattr(jobs, "MAX_429_STRIKES", 1)
    lines = [
        "x | WARNING | y - 429 Too Many Requests, retry_after: 5",
        _progress(50),
        "x | WARNING | y - 429 Too Many Requests, retry_after: 5",
    ]
    harness = _run(monkeypatch, lines)
    assert harness.proc is not None and not harness.proc.killed
    # wait = max(COOLDOWN_429 * 2**(strikes-1), retry_after + 1) -> 60s each time,
    # and crucially the second one is strike 1 again, not strike 2.
    assert [round(s, 1) for s in harness.sleeps] == [float(jobs.COOLDOWN_429)] * 2


def test_pyself_global_wording_triggers_the_global_abort(monkeypatch):
    monkeypatch.setattr(jobs, "GLOBAL_COOLDOWN", 900)
    harness = _Harness(["x | ERROR | y - Global rate limit has been hit."])
    harness.install(monkeypatch)
    with pytest.raises(jobs.RateLimitAbort):
        asyncio.run(jobs._run_process(_job(), ["backfill"]))
    assert 900 in harness.sleeps
    assert any("global" in m.lower() for m in harness.messages())
