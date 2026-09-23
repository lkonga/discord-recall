"""Unit + integration tests for the date-range digest runner and LLM wiring.

Run with:  uv run pytest tests/ -q
The integration test uses a throwaway SQLite database and a stubbed LLM, so
it needs no Discord token and no API key.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
from datetime import date, datetime, timezone

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Period math (pure, no DB)
# --------------------------------------------------------------------------


def test_align_snaps_to_period_start():
    from discord_recall.digest.range import align

    wednesday = date(2026, 9, 16)  # a Wednesday
    assert align(wednesday, "daily") == wednesday
    assert align(wednesday, "weekly") == date(2026, 9, 14)  # Monday
    assert align(wednesday, "monthly") == date(2026, 9, 1)


def test_iter_period_starts_daily_weekly_monthly():
    from discord_recall.digest.range import iter_period_starts

    daily = list(iter_period_starts(date(2026, 9, 18), date(2026, 9, 20), "daily"))
    assert daily == [date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 20)]

    # A range that starts mid-week still emits whole weeks, Monday-anchored.
    weekly = list(iter_period_starts(date(2026, 9, 16), date(2026, 10, 1), "weekly"))
    assert weekly == [date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28)]

    monthly = list(iter_period_starts(date(2026, 8, 5), date(2026, 10, 2), "monthly"))
    assert monthly == [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]


def test_iter_period_starts_single_day_and_errors():
    from discord_recall.digest.range import RangeError, iter_period_starts

    assert list(iter_period_starts(date(2026, 9, 18), date(2026, 9, 18), "daily")) == [
        date(2026, 9, 18)
    ]

    with pytest.raises(RangeError):
        list(iter_period_starts(date(2026, 9, 20), date(2026, 9, 18), "daily"))

    with pytest.raises(RangeError):
        list(iter_period_starts(date(2026, 9, 18), date(2026, 9, 20), "hourly"))


def test_bounds_are_utc_half_open_windows():
    from discord_recall.digest.range import _bounds

    start, end, label = _bounds(date(2026, 9, 18), "daily")
    assert (start, end) == (
        datetime(2026, 9, 18, tzinfo=timezone.utc),
        datetime(2026, 9, 19, tzinfo=timezone.utc),
    )
    assert label == "2026-09-18"

    start, end, label = _bounds(date(2026, 9, 14), "weekly")
    assert (end - start).days == 7
    assert label == "2026-09-14 to 2026-09-21"

    start, end, label = _bounds(date(2026, 2, 1), "monthly")
    assert end == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert label == "February 2026"


# --------------------------------------------------------------------------
# LLM endpoint resolution (no network)
# --------------------------------------------------------------------------


@pytest.fixture()
def clear_settings():
    from discord_recall.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _resolve_with(monkeypatch, **env):
    """Resolve the endpoint against explicit settings, ignoring .env files."""
    from discord_recall.config import Settings
    from discord_recall import llm as llm_module

    values = {
        "llm_base_url": "",
        "llm_api_key": "",
        "llm_model": "",
        "openrouter_api_key": "",
        "openrouter_model": "vendor/default-model",
        **env,
    }
    monkeypatch.setattr(
        llm_module, "get_settings", lambda: Settings(_env_file=None, **values)
    )
    return llm_module.resolve_endpoint()


def test_resolve_endpoint_openrouter_default(monkeypatch, clear_settings):
    url, key, model = _resolve_with(
        monkeypatch, openrouter_api_key="or-key", openrouter_model="vendor/model"
    )
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert (key, model) == ("or-key", "vendor/model")


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://api.deepseek.com", "https://api.deepseek.com/chat/completions"),
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
        (
            "https://api.deepseek.com/v1/chat/completions",
            "https://api.deepseek.com/v1/chat/completions",
        ),
        ("http://127.0.0.1:8080/v1/", "http://127.0.0.1:8080/v1/chat/completions"),
    ],
)
def test_resolve_endpoint_normalizes_base_url(monkeypatch, clear_settings, base, expected):
    url, key, model = _resolve_with(
        monkeypatch, llm_base_url=base, llm_api_key="k", llm_model="deepseek-chat"
    )
    assert (url, key, model) == (expected, "k", "deepseek-chat")


def test_resolve_endpoint_requires_a_key(monkeypatch, clear_settings):
    with pytest.raises(SystemExit):
        _resolve_with(monkeypatch)


def test_extract_text_handles_list_content_and_reasoning():
    from discord_recall.llm import _extract_text

    assert _extract_text({"content": "hello"}) == "hello"
    assert _extract_text({"content": [{"text": "a"}, {"text": "b"}]}) == "ab"
    assert _extract_text({"content": "", "reasoning_content": "thinking"}) == "thinking"


# --------------------------------------------------------------------------
# Integration: run_range against a throwaway SQLite store with a stubbed LLM
# --------------------------------------------------------------------------

DAY1 = date(2026, 9, 18)
DAY2 = date(2026, 9, 19)
DAY3 = date(2026, 9, 20)  # deliberately left empty for the "no activity" case


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "range-test.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
    )

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LLM_BASE_URL", "http://stub.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "stub")
    monkeypatch.setenv("LLM_MODEL", "stub-model")

    from discord_recall.config import get_settings

    get_settings.cache_clear()
    import discord_recall.db as db_module

    db_module._engine = None
    db_module._session_factory = None

    from discord_recall.db import get_session_factory
    from discord_recall.db.models import Author, Channel, Message, Server

    async def seed():
        session_factory = get_session_factory()
        async with session_factory() as session:
            session.add(Server(id=1, name="Stub Server"))
            session.add(Channel(id=10, server_id=1, name="general", type="text"))
            session.add(Channel(id=11, server_id=1, name="quiet", type="text"))
            session.add(Author(id=100, username="asha", display_name="Asha"))
            mid = 1000
            for day in (DAY1, DAY2):
                for i in range(3):
                    session.add(
                        Message(
                            id=mid,
                            channel_id=10,
                            author_id=100,
                            content=f"message {i} on {day}",
                            created_at=datetime.combine(
                                day, datetime.min.time()
                            ).replace(tzinfo=timezone.utc),
                        )
                    )
                    mid += 1
            await session.commit()

    asyncio.run(seed())
    yield url

    db_module._engine = None
    db_module._session_factory = None
    get_settings.cache_clear()


@pytest.fixture()
def stub_llm(monkeypatch):
    calls = {"n": 0}

    async def fake_complete(system: str, user_prompt: str, max_tokens: int | None = None):
        calls["n"] += 1
        assert "message 0 on" in user_prompt
        return "STUB SUMMARY"

    monkeypatch.setattr("discord_recall.digest.builder.complete", fake_complete)
    return calls


def test_run_range_daily_builds_then_reuses(seeded_db, stub_llm):
    from discord_recall.digest.range import run_range

    first = asyncio.run(
        run_range(channel_ids=[10], date_from=DAY1, date_to=DAY2, period="daily")
    )
    assert [b[:3] for b in first.built] == [
        (10, "general", "2026-09-18"),
        (10, "general", "2026-09-19"),
    ]
    assert first.reused == [] and first.failed == []
    assert stub_llm["n"] == 2

    second = asyncio.run(
        run_range(channel_ids=[10], date_from=DAY1, date_to=DAY2, period="daily")
    )
    assert second.built == [] and second.failed == []
    assert [r[:3] for r in second.reused] == [
        (10, "general", "2026-09-18"),
        (10, "general", "2026-09-19"),
    ]
    assert stub_llm["n"] == 2  # no further LLM calls


def test_run_range_force_recomputes_and_reports_empty(seeded_db, stub_llm):
    from discord_recall.digest.range import run_range

    # A range that includes an empty day: the empty day is reported, not failed.
    result = asyncio.run(
        run_range(channel_ids=[10, 11], date_from=DAY2, date_to=DAY3, period="daily")
    )
    assert {b[:3] for b in result.built} == {(10, "general", "2026-09-19")}
    assert {e[:3] for e in result.empty} == {
        (10, "general", "2026-09-20"),
        (11, "quiet", "2026-09-19"),
        (11, "quiet", "2026-09-20"),
    }
    assert result.failed == []

    forced = asyncio.run(
        run_range(channel_ids=[10], date_from=DAY2, date_to=DAY2, period="daily", force=True)
    )
    assert [b[:3] for b in forced.built] == [(10, "general", "2026-09-19")]

    # Build both days, then force one of them: the row is replaced, not duplicated.
    asyncio.run(
        run_range(channel_ids=[10], date_from=DAY1, date_to=DAY2, period="daily")
    )
    asyncio.run(
        run_range(channel_ids=[10], date_from=DAY2, date_to=DAY2, period="daily", force=True)
    )

    from discord_recall.db import get_session_factory
    from discord_recall.db.models import Digest
    from sqlalchemy import func, select

    async def count_rows():
        async with get_session_factory()() as session:
            stmt = select(func.count()).select_from(Digest).where(Digest.channel_id == 10)
            return int((await session.execute(stmt)).scalar_one())

    assert asyncio.run(count_rows()) == 2  # force replaced, never duplicated


def test_run_range_dry_run_makes_no_llm_calls(seeded_db, stub_llm):
    from discord_recall.digest.range import run_range

    result = asyncio.run(
        run_range(channel_ids=[10], date_from=DAY1, date_to=DAY3, period="daily", dry_run=True)
    )
    assert [(p.label, p.message_count) for p in result.planned] == [
        ("2026-09-18", 3),
        ("2026-09-19", 3),
    ]
    assert result.built == [] and stub_llm["n"] == 0


def test_run_range_rejects_bad_input(seeded_db):
    from discord_recall.digest.range import RangeError, run_range

    with pytest.raises(RangeError):
        asyncio.run(run_range(channel_ids=[10], date_from=DAY2, date_to=DAY1))

    with pytest.raises(RangeError):
        asyncio.run(run_range(channel_ids=[10], date_from=DAY1, period="hourly"))

    with pytest.raises(RangeError):
        asyncio.run(run_range(date_from=DAY1))  # no channel selection

    with pytest.raises(RangeError):
        asyncio.run(run_range(channel_ids=[999], date_from=DAY1))  # unknown channel


def test_builder_period_enum_binds_to_string_column(seeded_db, stub_llm):
    """Regression: DigestPeriod must bind to the String column (upstream crash)."""
    from discord_recall.db.models import DigestPeriod
    from discord_recall.digest.builder import build_daily_digest

    digest = asyncio.run(
        build_daily_digest(10, datetime.combine(DAY1, datetime.min.time()).replace(tzinfo=timezone.utc))
    )
    assert digest is not None
    assert digest.period == DigestPeriod.daily
    assert digest.period == "daily"
    assert digest.content == "STUB SUMMARY"


def test_backfill_status_compares_equal_to_stored_string(seeded_db):
    """Regression: resume logic compares a stored str to the enum.

    With a plain ``enum.Enum`` this comparison was always False, so a
    completed backfill was never skipped.
    """
    from sqlalchemy import select

    from discord_recall.db import get_session_factory
    from discord_recall.db.models import BackfillStatus, ChannelBackfillState

    async def scenario():
        async with get_session_factory()() as session:
            session.add(
                ChannelBackfillState(
                    channel_id=10, status=BackfillStatus.complete.value, last_message_id=1
                )
            )
            await session.commit()
        async with get_session_factory()() as session:
            row = (await session.execute(select(ChannelBackfillState))).scalar_one()
            return row.status

    stored = asyncio.run(scenario())
    assert stored == BackfillStatus.complete
    assert stored != BackfillStatus.in_progress
