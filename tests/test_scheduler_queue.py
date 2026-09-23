"""The container scheduler is an enqueue-only loop against the local API.

These tests drive `docker/scheduler.py` offline: the module's HTTP seam
(`scheduler.post_job`) is replaced by a stub, so no socket is opened and no
Discord traffic exists. What matters is the contract with the queue:

* capture then digest, per channel, in that order,
* payloads identical to what the UI posts (`from`, `period`, `maxMessages`),
* a non-2xx or unreachable API is logged and skipped, never raised,
* an `{"ok": false, ...}` refusal is reported as *skipped*, not as a failure,
* the guild whitelist still gates the channel list, and
* `ENQUEUE_MAX_PER_CYCLE` bounds one cycle.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import urllib.error
from datetime import date, timedelta
from pathlib import Path

import pytest

SCHEDULER_PATH = Path(__file__).resolve().parents[1] / "docker" / "scheduler.py"

spec = importlib.util.spec_from_file_location("scheduler_under_test", SCHEDULER_PATH)
assert spec is not None and spec.loader is not None
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)

TODAY = date(2026, 3, 10)


class Recorder:
    """Stub for the HTTP seam: records payloads, answers from a script."""

    def __init__(self, *answers: tuple[int, dict] | Exception) -> None:
        self.answers = list(answers)
        self.payloads: list[dict] = []
        self.urls: list[str] = []

    def __call__(self, payload: dict) -> tuple[int, dict]:
        self.payloads.append(payload)
        self.urls.append(scheduler.jobs_url())
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return 200, {"ok": True, "jobId": len(self.payloads), "status": "queued"}


@pytest.fixture
def two_channels(monkeypatch) -> None:
    """Two allowed channels, no guild whitelist: DATABASE_URL is left alone."""
    monkeypatch.setenv("DIGEST_CHANNELS", "111,222")
    monkeypatch.setenv("GUILD_WHITELIST", "")


@pytest.fixture
def stub(monkeypatch) -> Recorder:
    """Install a recorder as the HTTP seam and cap nothing."""
    recorder = Recorder()
    monkeypatch.setattr(scheduler, "post_job", recorder)
    monkeypatch.setattr(scheduler, "MAX_PER_CYCLE", 10)
    return recorder


def kinds(recorder: Recorder) -> list[tuple[str, str]]:
    return [(p["kind"], p["channelId"]) for p in recorder.payloads]


def test_module_cannot_run_the_cli_itself():
    """The scheduler must never do the Discord work: no CLI process, no shell."""
    source = SCHEDULER_PATH.read_text()
    assert "import subprocess" not in source
    assert not hasattr(scheduler, "subprocess")
    assert "Popen" not in source


def test_cycle_enqueues_capture_then_digest_per_channel(two_channels, stub):
    posted = scheduler.cycle(today=TODAY)

    assert posted == 4
    assert kinds(stub) == [
        ("capture", "111"),
        ("digest", "111"),
        ("capture", "222"),
        ("digest", "222"),
    ]

    capture, digest = stub.payloads[0], stub.payloads[1]
    assert capture == {
        "kind": "capture",
        "channelId": "111",
        "since": (TODAY - timedelta(days=scheduler.BACKFILL_DAYS)).isoformat(),
        "maxMessages": scheduler.MAX_MESSAGES,
    }
    assert digest == {
        "kind": "digest",
        "channelId": "111",
        "from": (TODAY - timedelta(days=scheduler.DIGEST_DAYS)).isoformat(),
        "to": (TODAY - timedelta(days=1)).isoformat(),
        "period": "daily",
    }
    # Loopback API, same port the UI uses.
    assert stub.urls == [scheduler.jobs_url()] * 4
    assert scheduler.jobs_url().endswith("/api/jobs")


def test_scheduler_never_passes_send_flags(two_channels, stub):
    """Delivery belongs to the queue: no --send, no telegram flag in any payload."""
    scheduler.cycle(today=TODAY)

    assert all(isinstance(p["channelId"], str) for p in stub.payloads)
    assert not any("send" in key or "telegram" in key for p in stub.payloads for key in p)


def test_non_2xx_is_logged_and_skipped_without_raising(two_channels, monkeypatch, capsys):
    recorder = Recorder((500, {"detail": "boom"}))
    monkeypatch.setattr(scheduler, "post_job", recorder)

    assert scheduler.cycle(today=TODAY) == 4  # the cycle carries on to channel 222
    out = capsys.readouterr().out
    assert "channel 111 capture: API returned HTTP 500" in out
    assert "boom" in out
    assert kinds(recorder) == [
        ("capture", "111"),
        ("digest", "111"),
        ("capture", "222"),
        ("digest", "222"),
    ]


def test_connection_error_is_logged_and_skipped(two_channels, monkeypatch, capsys):
    recorder = Recorder(ConnectionRefusedError("connection refused"))
    monkeypatch.setattr(scheduler, "post_job", recorder)

    assert scheduler.cycle(today=TODAY) == 4
    out = capsys.readouterr().out
    assert "channel 111 capture: API unreachable" in out
    assert "connection refused" in out
    assert len(recorder.payloads) == 4


def test_whitelist_refusal_is_skipped_not_failed(two_channels, monkeypatch, capsys):
    refusal = {
        "ok": False,
        "status": "refused: guild 999 is not on GUILD_WHITELIST",
        "whitelistBlocked": True,
    }
    recorder = Recorder((200, refusal), (200, {"ok": True, "jobId": 7, "status": "queued"}))
    monkeypatch.setattr(scheduler, "post_job", recorder)

    assert scheduler.cycle(today=TODAY) == 4
    out = capsys.readouterr().out
    assert "channel 111 capture: skipped: refused: guild 999 is not on GUILD_WHITELIST" in out
    assert "channel 111 digest: queued job 7" in out
    assert "failed" not in out


def test_required_argument_errors_are_left_to_the_api(two_channels, monkeypatch):
    """A 400 from the API is a normal skip; the loop keeps going."""
    recorder = Recorder((400, {"detail": "channelId must be a Discord snowflake"}))
    monkeypatch.setattr(scheduler, "post_job", recorder)

    assert scheduler.cycle(today=TODAY) == 4


def test_per_cycle_cap_is_respected(two_channels, monkeypatch, capsys):
    recorder = Recorder()
    monkeypatch.setattr(scheduler, "post_job", recorder)
    monkeypatch.setattr(scheduler, "MAX_PER_CYCLE", 3)

    assert scheduler.cycle(today=TODAY) == 3
    assert kinds(recorder) == [("capture", "111"), ("digest", "111"), ("capture", "222")]
    out = capsys.readouterr().out
    assert "per-cycle cap ENQUEUE_MAX_PER_CYCLE=3 reached" in out
    assert "trim DIGEST_CHANNELS" in out


def make_store(path: Path, rows: list[tuple[int, int]]) -> None:
    con = sqlite3.connect(path)
    con.execute("create table channels (id integer primary key, server_id integer)")
    con.executemany("insert into channels (id, server_id) values (?, ?)", rows)
    con.commit()
    con.close()


def test_whitelist_still_drops_unlisted_channels(tmp_path, monkeypatch, capsys):
    store = tmp_path / "recall.db"
    make_store(store, [(111, 1), (222, 2), (333, 1)])
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{store}")
    monkeypatch.setenv("GUILD_WHITELIST", "1")
    monkeypatch.setenv("DIGEST_CHANNELS", "111,222,333")
    recorder = Recorder()
    monkeypatch.setattr(scheduler, "post_job", recorder)

    assert scheduler.channels() == ["111", "333"]
    assert "skipping 1 channel(s) outside GUILD_WHITELIST" in capsys.readouterr().out

    assert scheduler.cycle(today=TODAY) == 4
    assert kinds(recorder) == [
        ("capture", "111"),
        ("digest", "111"),
        ("capture", "333"),
        ("digest", "333"),
    ]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("", "/data/discord_recall.db"),
        ("sqlite+aiosqlite:///discord_recall.db", "discord_recall.db"),
        ("sqlite+aiosqlite:////data/discord_recall.db", "/data/discord_recall.db"),
        ("postgresql+asyncpg://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
    ],
)
def test_db_path_keeps_absolute_sqlite_paths(monkeypatch, url, expected):
    """The container uses the four-slash absolute form; the gate must find it."""
    monkeypatch.setenv("DATABASE_URL", url)
    assert scheduler.db_path() == expected


def test_unreadable_store_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'missing.db'}")
    monkeypatch.setenv("GUILD_WHITELIST", "1")
    monkeypatch.setenv("DIGEST_CHANNELS", "111")
    recorder = Recorder()
    monkeypatch.setattr(scheduler, "post_job", recorder)

    assert scheduler.channels() == []
    assert scheduler.cycle(today=TODAY) == 0
    assert recorder.payloads == []
    assert "could not read the store" in capsys.readouterr().out


def test_no_channels_means_no_traffic(monkeypatch):
    monkeypatch.setenv("DIGEST_CHANNELS", "")
    monkeypatch.setenv("GUILD_WHITELIST", "")
    recorder = Recorder()
    monkeypatch.setattr(scheduler, "post_job", recorder)

    assert scheduler.cycle(today=TODAY) == 0
    assert recorder.payloads == []


def test_post_job_posts_json_to_the_loopback_api(monkeypatch):
    """The only real-network half: check the request it would send, not send it."""
    captured = {}

    class FakeResponse:
        status = 201
        _body = b'{"ok": true, "jobId": 3}'

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = request.data
        captured["content_type"] = request.get_header("Content-type")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(scheduler.urllib.request, "urlopen", fake_urlopen)
    payload = {"kind": "digest", "channelId": "111", "from": "2026-03-07", "period": "daily"}

    assert scheduler.post_job(payload) == (201, {"ok": True, "jobId": 3})
    assert captured["url"] == scheduler.jobs_url()
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["timeout"] == scheduler.HTTP_TIMEOUT
    assert json.loads(captured["body"]) == payload


def test_http_error_status_is_returned_not_raised(monkeypatch):
    monkeypatch.setattr(
        scheduler.urllib.request,
        "urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(
            urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)
        ),
    )

    status, body = scheduler.post_job({"kind": "capture"})
    assert status == 429
    assert body == {}
