"""Policy tests for the pacing/backoff logic in :mod:`discord_recall.web.jobs`.

These are *policy* tests derived from PATCHES.md section 7 ("Pacing policy for
the self-bot token"): page size 100, a 2.5 s base delay plus +0..+1.5 s jitter
that never dips below the base, a 1000-message cap per run, `retry_after + 1 s`
before retrying a 429 with the wait doubling per consecutive strike, a hard stop
after three strikes, an abort with a long pause on a global/Cloudflare block,
and the post-429 signal the worker uses to pause between runs.

Everything here is offline and instantiates nothing: `asyncio.sleep` is replaced
by a recorder and `asyncio.create_subprocess_exec` by a fake process whose stdout
is a canned list of lines. No Discord, no network, no subprocess, no database and
no real waiting. Tests named ``test_characterization_*`` pin down behaviour that
the PATCHES.md table describes differently - those are evidence of a
policy/implementation disagreement, not assertions that the policy is met.
"""

from __future__ import annotations

import asyncio

import pytest

from discord_recall.db.models import Job
from discord_recall.web import jobs

# --------------------------------------------------------------------------
# Offline harness: no subprocess, no sleeps, no database
# --------------------------------------------------------------------------


class _FakeProc:
    """Stand-in for an asyncio subprocess: canned stdout lines, no process."""

    def __init__(self, lines: list[str], returncode: int = 0):
        self.lines = lines
        self.returncode = returncode
        self.stdout = self._stream()
        self.killed = False

    async def _stream(self):
        for line in self.lines:
            yield line.encode()

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


class _Harness:
    """Records what the job code did instead of doing it for real."""

    def __init__(self, lines: list[str]):
        self.lines = lines
        self.sleeps: list[float] = []
        self.updates: list[tuple[int, dict]] = []
        self.argv: list[str] = []
        self.proc: _FakeProc | None = None

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)

    async def spawn(self, *argv: str, **_kwargs) -> _FakeProc:
        self.argv = list(argv)
        self.proc = _FakeProc(self.lines)
        return self.proc

    async def update(self, job_id: int, **fields) -> None:
        self.updates.append((job_id, fields))

    def phases(self) -> list[str]:
        return [fields["phase"] for _, fields in self.updates if "phase" in fields]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jobs.asyncio, "sleep", self.sleep)
        monkeypatch.setattr(jobs.asyncio, "create_subprocess_exec", self.spawn)
        monkeypatch.setattr(jobs, "_update", self.update)


def _job(payload: dict | None = None, kind: str = "capture") -> Job:
    job = Job(kind=kind, payload={"channelId": 111} if payload is None else payload)
    job.id = 7
    return job


def _flag(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def _run(monkeypatch, lines: list[str], payload: dict | None = None) -> tuple:
    harness = _Harness(lines)
    harness.install(monkeypatch)
    rc, output = asyncio.run(jobs._run_process(_job(payload), ["backfill"]))
    return harness, rc, output


# --------------------------------------------------------------------------
# 1. build_args: page size, jittered delay window, message cap
# --------------------------------------------------------------------------


def test_build_args_capture_uses_page_size_and_jittered_delay():
    for _ in range(50):
        args = jobs.build_args(_job({"channelId": 111, "maxMessages": 400}))
        assert args[0] == "backfill"
        assert _flag(args, "-c") == "111"
        assert _flag(args, "--batch-size") == str(jobs.PACE_BATCH)
        delay = float(_flag(args, "--delay"))
        assert jobs.PACE_DELAY <= delay
        assert delay <= jobs.PACE_DELAY + jobs.PACE_JITTER_EXTRA


def test_build_args_delay_stays_inside_the_jitter_window_at_both_extremes(monkeypatch):
    monkeypatch.setattr(jobs.random, "uniform", lambda low, high: low)  # no jitter
    assert float(_flag(jobs.build_args(_job()), "--delay")) == jobs.PACE_DELAY

    monkeypatch.setattr(jobs.random, "uniform", lambda low, high: high)  # max jitter
    top = float(_flag(jobs.build_args(_job()), "--delay"))
    assert top == pytest.approx(jobs.PACE_DELAY + jobs.PACE_JITTER_EXTRA, abs=0.005)


def test_build_args_clamps_max_messages_to_the_pace_cap():
    over = jobs.PACE_MAX_MESSAGES + 5000
    args = jobs.build_args(_job({"channelId": 1, "maxMessages": over}))
    assert _flag(args, "--max-messages") == str(jobs.PACE_MAX_MESSAGES)
    # absent cap -> the policy default, never "unbounded"
    assert _flag(jobs.build_args(_job({"channelId": 1})), "--max-messages") == str(
        jobs.PACE_MAX_MESSAGES
    )
    # a smaller explicit request is honoured as-is
    assert _flag(
        jobs.build_args(_job({"channelId": 1, "maxMessages": 250})), "--max-messages"
    ) == "250"


# --------------------------------------------------------------------------
# 2. _jittered: never below the base, never above base + jitter cap
# --------------------------------------------------------------------------


def test_jittered_over_many_samples_stays_in_range():
    samples: list[float] = []
    for base in (0.0, 1.0, jobs.PACE_DELAY, 30.0):
        samples = [jobs._jittered(base) for _ in range(500)]
        assert min(samples) >= base
        assert max(samples) <= base + jobs.PACE_JITTER_EXTRA
    assert len(set(samples)) > 1  # the extra is actually random, not fixed


def test_jittered_boundaries_are_inclusive(monkeypatch):
    monkeypatch.setattr(jobs.random, "uniform", lambda low, high: low)
    assert jobs._jittered(jobs.PACE_DELAY) == jobs.PACE_DELAY
    monkeypatch.setattr(jobs.random, "uniform", lambda low, high: high)
    assert jobs._jittered(jobs.PACE_DELAY) == jobs.PACE_DELAY + jobs.PACE_JITTER_EXTRA


# --------------------------------------------------------------------------
# 3. 429 handling inside _run_process
# --------------------------------------------------------------------------


def test_rate_limit_waits_retry_after_plus_one_then_continues(monkeypatch):
    retry_after = 5
    lines = [
        f"backfill | 429 rate limited, retry_after {retry_after}",
        "backfill | 300 messages so far",
    ]
    harness, rc, output = _run(monkeypatch, lines)

    assert harness.sleeps, "a 429 must pause the run"
    assert max(harness.sleeps) >= retry_after + 1  # retry_after + 1s buffer
    assert "cooling down" in harness.phases()  # job row shows the cooldown
    assert rc == 0  # returns normally once the run finishes
    assert "__RATE_LIMITED__" in output  # the worker's post-429 signal


def test_retry_after_longer_than_the_base_cooldown_wins(monkeypatch):
    retry_after = int(jobs.COOLDOWN_429) + 17  # above COOLDOWN_429 * 2**0
    lines = [f"backfill | 429 rate limited, retry_after {retry_after}"]
    harness, _, _ = _run(monkeypatch, lines)
    assert harness.sleeps == [float(retry_after + 1)]


def test_rate_limit_without_retry_after_aborts_instead_of_retrying(monkeypatch):
    """PATCHES.md: with no Retry-After you must not retry programmatically."""
    harness = _Harness(["backfill | 429 rate limited (no Retry-After header)"])
    harness.install(monkeypatch)

    with pytest.raises(jobs.RateLimitAbort):
        asyncio.run(jobs._run_process(_job(), ["backfill"]))

    assert harness.proc is not None and harness.proc.killed
    assert harness.sleeps == []  # no wait, no retry
    assert "aborted" in harness.phases()


def test_characterization_retry_after_above_the_ceiling_breaks_the_floor(monkeypatch):
    """PATCHES.md: wait ``retry_after + 1 s``. Here the 900 s ceiling wins."""
    retry_after = 1200
    lines = [f"backfill | 429 rate limited, retry_after {retry_after}"]
    harness, _, _ = _run(monkeypatch, lines)
    assert harness.sleeps == [900.0]
    assert harness.sleeps[0] < retry_after + 1  # documented floor does not hold


# --------------------------------------------------------------------------
# 4. Hard stop: consecutive 429s must abort, not sleep forever
# --------------------------------------------------------------------------


def test_four_consecutive_rate_limits_abort_the_run(monkeypatch):
    lines = ["backfill | 429 rate limited, retry_after 1"] * 4
    harness = _Harness(lines)
    harness.install(monkeypatch)

    with pytest.raises(jobs.RateLimitAbort):
        asyncio.run(jobs._run_process(_job(), ["backfill"]))

    assert harness.proc is not None and harness.proc.killed
    assert "aborted" in harness.phases()
    # three strikes are slept through with a doubling wait, then the fourth stops
    # the run after the long pause instead of sleeping forever
    assert harness.sleeps == [
        jobs.COOLDOWN_429,
        jobs.COOLDOWN_429 * 2,
        jobs.COOLDOWN_429 * 4,
        jobs.GLOBAL_COOLDOWN,
    ]


def test_characterization_strike_out_pauses_longer_than_the_policy_row(monkeypatch):
    """`_run_process` sleeps GLOBAL_COOLDOWN on *every* abort, not only on a
    global/Cloudflare block, and it then reports True, so `worker_loop` adds
    POST_429_PAUSE on top. PATCHES.md assigns the 15 min pause to the global row
    only, so the effective strike-out pause is ~25 min."""
    lines = ["backfill | 429 rate limited, retry_after 1"] * 4
    harness = _Harness(lines)
    harness.install(monkeypatch)

    assert asyncio.run(jobs._execute(_job())) is True  # -> worker pauses too
    in_run_pause = sum(harness.sleeps)
    assert in_run_pause == (
        jobs.COOLDOWN_429
        + jobs.COOLDOWN_429 * 2
        + jobs.COOLDOWN_429 * 4
        + jobs.GLOBAL_COOLDOWN
    )


# --------------------------------------------------------------------------
# 5. Global / Cloudflare blocks: abort after the long pause
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "backfill | 429 global rate limit hit, backing off",
        "backfill | 429 blocked by Cloudflare, try again later",
    ],
)
def test_global_or_cloudflare_block_aborts_after_the_long_pause(monkeypatch, line):
    harness = _Harness([line])
    harness.install(monkeypatch)

    with pytest.raises(jobs.RateLimitAbort):
        asyncio.run(jobs._run_process(_job(), ["backfill"]))

    assert harness.proc is not None and harness.proc.killed
    assert harness.sleeps == [jobs.GLOBAL_COOLDOWN]  # 15 min, per the policy
    assert "aborted" in harness.phases()
    assert len(harness.sleeps) == 1  # no doubling, no retry


def test_characterization_cloudflare_without_a_429_marker_is_not_an_abort(monkeypatch):
    """The policy row is "Global / Cloudflare 429 -> abort run". The detector
    requires /rate limited|429/ as well, so a bare Cloudflare line is treated as
    ordinary chatter and the run keeps going."""
    lines = [
        "capture | Cloudflare returned an unexpected challenge",
        "capture | 50 messages so far",
    ]
    harness, rc, output = _run(monkeypatch, lines)

    assert rc == 0
    assert harness.sleeps == []
    assert "aborted" not in harness.phases()
    assert "__RATE_LIMITED__" not in output


# --------------------------------------------------------------------------
# 6. _execute: the rate-limit signal handed back to the worker
# --------------------------------------------------------------------------


def test_execute_returns_true_when_a_rate_limit_was_seen(monkeypatch):
    lines = [
        "backfill | 429 rate limited, retry_after 5",
        "backfill | 300 messages so far",
    ]
    harness = _Harness(lines)
    harness.install(monkeypatch)

    assert asyncio.run(jobs._execute(_job())) is True
    assert "cooling down" in harness.phases()
    assert harness.updates[-1][1]["status"] == jobs.JobStatus.done.value


def test_execute_returns_false_on_a_clean_run(monkeypatch):
    lines = [
        "backfill | 300 messages so far",
        "backfill | Backfill complete - 300 messages",
    ]
    harness = _Harness(lines)
    harness.install(monkeypatch)

    assert asyncio.run(jobs._execute(_job())) is False
    last = harness.updates[-1][1]
    assert last["status"] == jobs.JobStatus.done.value
    assert last["phase"] == "done"
    assert last["messages"] == 300
    assert harness.sleeps == []


def test_execute_returns_true_when_the_run_was_aborted(monkeypatch):
    lines = ["backfill | 429 rate limited, retry_after 1"] * 4
    harness = _Harness(lines)
    harness.install(monkeypatch)

    assert asyncio.run(jobs._execute(_job())) is True  # worker applies its pause
    last = harness.updates[-1][1]
    assert last["status"] == jobs.JobStatus.error.value
    assert last["phase"] == "stopped"


def test_characterization_rate_limit_sentinel_leaks_into_the_job_message(monkeypatch):
    """The internal marker is appended to the captured output and _execute takes
    the human-visible summary from the last line, so a throttled-but-successful
    run shows the raw sentinel in the job row."""
    lines = [
        "backfill | 429 rate limited, retry_after 5",
        "backfill | 300 messages so far",
    ]
    harness = _Harness(lines)
    harness.install(monkeypatch)

    assert asyncio.run(jobs._execute(_job())) is True
    assert harness.updates[-1][1]["message"] == "__RATE_LIMITED__"
