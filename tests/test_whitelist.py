"""Guild whitelist: parsing and the allow/deny decision.

The whitelist is the guard that keeps the token away from guilds the user did not
opt into, so both halves (parse the env, decide per channel) are tested directly.
"""

from __future__ import annotations

import pytest

from discord_recall.config import Settings, whitelisted_guilds
from discord_recall.web.api import whitelist_decision


def settings_with(value: str) -> Settings:
    return Settings(_env_file=None, guild_whitelist=value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", set()),
        ("   ", set()),
        ("1", {1}),
        ("1,2,3", {1, 2, 3}),
        ("1, 2 ,  3", {1, 2, 3}),
        ("[1, 2]", {1, 2}),
        ('["1","2"]', {1, 2}),
        ("1,notanid,2", {1, 2}),
    ],
)
def test_whitelisted_guilds_parsing(raw, expected):
    assert whitelisted_guilds(settings_with(raw)) == expected


def test_no_whitelist_allows_everything():
    allowed, reason = whitelist_decision(123, set())
    assert allowed and "no guild whitelist" in reason


def test_whitelisted_guild_is_allowed():
    allowed, reason = whitelist_decision(1426301184846594282, {1426301184846594282})
    assert allowed and reason == "guild is whitelisted"


def test_outside_whitelist_is_refused():
    allowed, reason = whitelist_decision(999, {1426301184846594282})
    assert not allowed and "outside GUILD_WHITELIST" in reason


def test_unknown_channel_is_refused_when_whitelist_is_set():
    """A channel we cannot place in a guild must not be touched."""
    allowed, reason = whitelist_decision(None, {1426301184846594282})
    assert not allowed and "not in any known guild" in reason


def test_unknown_channel_is_allowed_without_whitelist():
    allowed, _ = whitelist_decision(None, set())
    assert allowed
