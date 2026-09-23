"""Tests for the bounded (--since/--until/--max-messages) backfill.

These use stubs only: no Discord connection, no database.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from discord_recall.capture import backfill as b
from discord_recall.capture.backfill import window_bounds


def test_window_bounds_inclusive_until():
    since, until = window_bounds("2026-09-01", "2026-09-07")
    assert since == datetime(2026, 9, 1, tzinfo=timezone.utc)
    # 'until' is exclusive, so the 7th is fully covered
    assert until == datetime(2026, 9, 8, tzinfo=timezone.utc)


def test_window_bounds_partial_and_none():
    assert window_bounds(None, None) == (None, None)
    since, until = window_bounds("2026-09-17", None)
    assert since == datetime(2026, 9, 17, tzinfo=timezone.utc) and until is None
    since, until = window_bounds(None, "2026-09-20")
    assert since is None and until == datetime(2026, 9, 21, tzinfo=timezone.utc)


def test_window_bounds_rejects_bad_date():
    with pytest.raises(ValueError):
        window_bounds("17-09-2026", None)


# --------------------------------------------------------------------------
# Bounded backfill behaviour
# --------------------------------------------------------------------------


class _FakeMessage:
    _next_id = 1000

    def __init__(self, created_at: datetime):
        _FakeMessage._next_id += 1
        self.id = _FakeMessage._next_id
        self.created_at = created_at
        self.content = "stub"


class _FakeChannel:
    def __init__(self, messages):
        self.id = 42
        self.name = "general"
        self.guild = type("G", (), {"name": "Stub Guild"})()
        self._messages = messages
        self.calls: list[dict] = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        limit = kwargs.get("limit") or len(self._messages)
        batch, self._messages = self._messages[:limit], self._messages[limit:]

        async def gen():
            for m in batch:
                yield m

        return gen()


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


@pytest.fixture()
def stubs(monkeypatch):
    recorded = {"messages": [], "state_writes": [], "state_reads": 0}

    async def noop(*args, **kwargs):
        return None

    async def upsert_message(session, msg):
        recorded["messages"].append(msg)

    async def set_status(session, channel_id, status, **kwargs):
        recorded["state_writes"].append((channel_id, status, kwargs))

    async def get_state(session, channel_id):
        recorded["state_reads"] += 1
        return None

    settings = type(
        "S", (), {"backfill_batch_size": 2, "backfill_delay_seconds": 0.0}
    )()

    monkeypatch.setattr(b, "get_settings", lambda: settings)
    monkeypatch.setattr(b, "get_session_factory", lambda: (lambda: _FakeSession()))
    monkeypatch.setattr(b, "upsert_channel", noop)
    monkeypatch.setattr(b, "upsert_thread", noop)
    monkeypatch.setattr(b, "upsert_message", upsert_message)
    monkeypatch.setattr(b, "_set_backfill_status", set_status)
    monkeypatch.setattr(b, "_get_backfill_state", get_state)
    return recorded


def _messages(n: int, start=datetime(2026, 9, 18, tzinfo=timezone.utc)):
    return [_FakeMessage(start.replace(hour=h % 24)) for h in range(n)]


def test_bounded_backfill_caps_and_leaves_state_untouched(stubs):
    channel = _FakeChannel(_messages(5))
    since, until = window_bounds("2026-09-18", "2026-09-20")

    asyncio.run(
        b.backfill_channel(
            None, channel, since=since, until=until, max_messages=3, delay=0
        )
    )

    assert len(stubs["messages"]) == 3  # hard cap honoured
    assert stubs["state_reads"] == 0  # no resume cursor consulted
    assert stubs["state_writes"] == []  # nothing marked in-progress/complete/failed
    assert channel.calls[0]["after"] == since
    assert channel.calls[0]["before"] == until
    assert channel.calls[0]["oldest_first"] is True
    # batch_size from settings is 2, cap leaves 3 -> 2 then 1
    assert [c["limit"] for c in channel.calls[:2]] == [2, 1]


def test_bounded_backfill_walks_until_history_is_exhausted(stubs):
    channel = _FakeChannel(_messages(3))

    asyncio.run(b.backfill_channel(None, channel, since=None, max_messages=None, delay=0))

    # No window at all -> unbounded path, which does consult the cursor
    assert stubs["state_reads"] == 1
    assert len(stubs["messages"]) == 3


def test_unbounded_backfill_still_tracks_state(stubs):
    channel = _FakeChannel(_messages(3))

    asyncio.run(b.backfill_channel(None, channel, delay=0))

    statuses = [w[1].value for w in stubs["state_writes"]]
    assert statuses[0] == "in_progress" and statuses[-1] == "complete"
