"""Offline tests for ``scripts/burner_prime.py``.

Everything here runs without Discord, without a token and without waiting: the
HTTP client is replaced by a routing fake, and the pacer's ``sleep`` is replaced
by a recorder (via the module's own ``asyncio.sleep`` lookup, so the script's
default sleep function is what gets patched - not a copy of it).

Covered on purpose:
  * dry run resolves a plan and sends zero write requests,
  * the live happy path is exactly invite -> accept -> delete,
  * a 403 on invite creation is reported and never retried,
  * 429 handling: wait on ``retry_after`` (with the jobs.py floor), abort when the
    header is missing, hard stop after three strikes,
  * 401 stops the whole run, and every requested server still gets a row,
  * blockers (verification level, rules gate, member cap, invalid invite,
    already-member) are named and their response body is reported,
  * no token ever appears in the report text, in stdout or in stderr.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import burner_prime  # noqa: E402 - the script is loaded from its path, not a package

SOURCE_TOKEN = "source-token-9f3c7a11"
BURNER_TOKEN = "burner-token-4b2e88d0"
TOKENS = {SOURCE_TOKEN: "source", BURNER_TOKEN: "burner"}

GUILD = 1426301184846594282
OTHER_GUILD = 1391832426048651334
CHANNEL = 111111111111111111
CHANNEL_2 = 222222222222222222
VOICE = 333333333333333333
CATEGORY = 444444444444444444
INVITE_CODE = "aBcD1234"
INVITE_PATH = f"/channels/{CHANNEL}/invites"
ACCEPT_PATH = f"/invites/{INVITE_CODE}"


# ---------------------------------------------------------------------------
# Fakes: no network, no tokens, no waiting
# ---------------------------------------------------------------------------


class Call:
    """One recorded request, with the role whose token was used."""

    def __init__(self, method: str, path: str, role: str, body: object) -> None:
        self.method = method
        self.path = path
        self.role = role
        self.body = body

    @property
    def is_write(self) -> bool:
        return self.method in burner_prime.WRITE_METHODS

    def __repr__(self) -> str:  # pragma: no cover - assertion helper
        return f"{self.method} {self.path} ({self.role})"


class FakeHTTP:
    """Stands in for :class:`burner_prime.DiscordHTTP`.

    Routes are keyed ``(METHOD, path, role)``, falling back to
    ``(METHOD, path)``, and a list of replies is served in order (the last one
    repeats) so a single route can say "429, then 200" or "429 forever".
    """

    def __init__(self, routes: dict, tokens: dict[str, str] | None = None) -> None:
        self.routes = dict(routes)
        self.tokens = dict(TOKENS if tokens is None else tokens)
        self.calls: list[Call] = []
        self._cursor: dict[tuple, int] = {}

    async def __aenter__(self) -> FakeHTTP:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def _lookup(self, method: str, path: str, role: str):
        for key in ((method, path, role), (method, path)):
            if key in self.routes:
                return self.routes[key]
        raise AssertionError(f"unexpected request: {method} {path} ({role})")

    async def request(self, method: str, path: str, *, token=None, json=None):
        method = method.upper()
        role = self.tokens.get(token)
        assert role is not None, "a request carried a token the fake does not know"
        self.calls.append(Call(method, path, role, json))
        canned = self._lookup(method, path, role)
        if isinstance(canned, list):
            index = self._cursor.get((method, path, role), 0)
            self._cursor[(method, path, role)] = min(index + 1, len(canned) - 1)
            canned = canned[index]
        return replace(canned, method=method, path=path)

    def writes(self) -> list[Call]:
        return [call for call in self.calls if call.is_write]

    def paths(self) -> list[str]:
        return [call.path for call in self.calls]


def reply(status: int, data: object = None, retry_after: float | None = None):
    return burner_prime.Reply(
        status=status, method="GET", path="", data=data, retry_after=retry_after
    )


def source_guilds(*guild_ids: int, name: str = "Demo Lab", permissions: int = 0x1):
    return [
        {
            "id": str(guild_id),
            "name": name if guild_id == GUILD else f"{name}-{guild_id}",
            "permissions": str(permissions),
        }
        for guild_id in (guild_ids or (GUILD,))
    ]


def channel(cid: int = CHANNEL, name: str = "general", ctype: int = 0) -> dict:
    return {"id": str(cid), "name": name, "type": ctype}


def routes(extra: dict | None = None) -> dict:
    """The three read routes every run needs, plus per-test overrides."""
    base = {
        ("GET", "/users/@me/guilds", "source"): reply(200, source_guilds(GUILD)),
        ("GET", "/users/@me/guilds", "burner"): reply(200, []),
        ("GET", f"/guilds/{GUILD}/channels", "source"): reply(200, [channel()]),
    }
    base.update(extra or {})
    return base


def run(fake: FakeHTTP, *, guild_ids: tuple[int, ...] = (GUILD,), **kwargs):
    kwargs.setdefault("dry_run", False)
    return asyncio.run(
        burner_prime.prime(
            source_token=SOURCE_TOKEN,
            burner_token=BURNER_TOKEN,
            guild_ids=list(guild_ids),
            client=fake,
            **kwargs,
        )
    )


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Replaces the sleep function: everything the pacer sleeps is recorded."""
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(burner_prime.asyncio, "sleep", fake_sleep)
    return recorded


@pytest.fixture
def no_jitter(monkeypatch) -> None:
    """Deterministic pacing: jitter sits at its lower bound (0 s)."""
    monkeypatch.setattr(burner_prime.random, "uniform", lambda low, high: low)


# ---------------------------------------------------------------------------
# 1. Dry run: a plan, and not one write request
# ---------------------------------------------------------------------------


def test_dry_run_plans_and_sends_no_write_request(sleeps, no_jitter):
    fake = FakeHTTP(routes())
    report = run(fake, dry_run=True)

    assert fake.writes() == [], "a dry run must not send any write request"
    assert {call.method for call in fake.calls} == {"GET"}

    guild = report.guilds[0]
    assert guild.planned == [f"#general ({CHANNEL})"]
    assert guild.verdict.startswith("PLANNED")
    assert report.exit_code() == 0

    text = burner_prime.report_text(report)
    assert "dry run (no write request is sent)" in text
    assert f"#general ({CHANNEL})" in text
    assert "burner join" in text
    assert "not attempted (dry run)" in text


def test_dry_run_keeps_one_invite_per_server_and_ignores_voice_and_categories(
    sleeps, no_jitter
):
    fake = FakeHTTP(
        routes(
            extra={
                ("GET", f"/guilds/{GUILD}/channels", "source"): reply(
                    200,
                    [
                        channel(VOICE, "voice", 2),
                        channel(CATEGORY, "Category", 4),
                        channel(CHANNEL, "general"),
                        channel(CHANNEL_2, "chat"),
                    ],
                )
            }
        )
    )
    report = run(fake, dry_run=True)

    guild = report.guilds[0]
    assert guild.planned == [f"#general ({CHANNEL})"]
    assert any("other invite-capable text channel" in note for note in guild.notes)
    text = burner_prime.report_text(report)
    assert "voice" not in text and "Category" not in text


def test_dry_run_honours_channel_pins_and_wildcards(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("GET", "/users/@me/guilds", "source"): reply(
                    200, source_guilds(GUILD, OTHER_GUILD)
                ),
                ("GET", f"/guilds/{GUILD}/channels", "source"): reply(
                    200, [channel(CHANNEL, "general"), channel(CHANNEL_2, "chat")]
                ),
                ("GET", f"/guilds/{OTHER_GUILD}/channels", "source"): reply(
                    200, [channel(CHANNEL, "general"), channel(CHANNEL_2, "chat")]
                ),
            }
        )
    )
    report = run(
        fake,
        guild_ids=(GUILD, OTHER_GUILD),
        dry_run=True,
        channels={GUILD: [CHANNEL_2], 0: [CHANNEL]},
    )

    assert report.guilds[0].planned == [f"#chat ({CHANNEL_2})"]  # pinned per server
    assert report.guilds[1].planned == [f"#general ({CHANNEL})"]  # wildcard group
    assert fake.writes() == []


def test_dry_run_reports_a_server_the_source_cannot_read(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("GET", f"/guilds/{GUILD}/channels", "source"): reply(
                    403, {"message": "Missing Access", "code": 50001}
                )
            }
        )
    )
    report = run(fake, dry_run=True)

    assert report.guilds[0].verdict.startswith("ABORTED")
    assert "403" in report.guilds[0].verdict
    # the server's answer is reported verbatim, so the reason is checkable
    assert "Missing Access" in burner_prime.report_text(report)
    assert fake.writes() == []


# ---------------------------------------------------------------------------
# 2. Live happy path
# ---------------------------------------------------------------------------


def test_live_run_creates_accepts_and_deletes_the_invite(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): reply(200, {"code": INVITE_CODE}),
                ("POST", ACCEPT_PATH, "burner"): reply(
                    200, {"guild": {"id": str(GUILD)}}
                ),
                ("DELETE", ACCEPT_PATH, "source"): reply(200, {"code": INVITE_CODE}),
            }
        )
    )
    report = run(fake)

    assert [(c.method, c.path, c.role) for c in fake.writes()] == [
        ("POST", INVITE_PATH, "source"),
        ("POST", ACCEPT_PATH, "burner"),
        ("DELETE", ACCEPT_PATH, "source"),
    ]
    assert fake.writes()[0].body == {
        "max_age": 86400,
        "max_uses": 1,
        "unique": True,
        "temporary": False,
    }
    assert fake.writes()[1].body is None, "accepting an invite carries no payload"

    attempt = report.guilds[0].attempts[0]
    assert (attempt.invite, attempt.join, attempt.verdict) == (
        INVITE_CODE,
        "joined",
        "OK",
    )
    assert report.guilds[0].deleted == [INVITE_CODE]
    assert report.guilds[0].verdict.startswith("OK")
    assert report.exit_code() == 0

    # one request at a time, each completed step followed by 2.5 s + jitter
    assert sleeps and all(delay >= burner_prime.PACE_DELAY for delay in sleeps)
    assert burner_prime.PACE_JITTER_EXTRA > 0

    text = burner_prime.report_text(report)
    assert INVITE_CODE in text
    assert "verdict: OK" in text
    assert "1/1 servers satisfied" in text


def test_burner_already_in_the_server_creates_no_invite(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("GET", "/users/@me/guilds", "burner"): reply(
                    200, source_guilds(GUILD, name="Burner Side")
                )
            }
        )
    )
    report = run(fake)

    assert fake.writes() == []
    assert report.guilds[0].verdict.startswith("NO-OP")
    assert report.guilds[0].attempts[0].join == "already member"
    assert report.exit_code() == 0


def test_max_joins_caps_the_run(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("GET", "/users/@me/guilds", "source"): reply(
                    200, source_guilds(GUILD, OTHER_GUILD)
                ),
                ("GET", f"/guilds/{OTHER_GUILD}/channels", "source"): reply(
                    200, [channel(CHANNEL_2, "chat")]
                ),
                ("POST", INVITE_PATH, "source"): reply(200, {"code": INVITE_CODE}),
                ("POST", ACCEPT_PATH, "burner"): reply(200, {"guild": {}}),
                ("DELETE", ACCEPT_PATH, "source"): reply(200, {}),
            }
        )
    )
    report = run(fake, guild_ids=(GUILD, OTHER_GUILD), max_joins=1)

    assert report.guilds[0].verdict.startswith("OK")
    assert report.guilds[1].verdict == "SKIPPED - --max-joins 1 already reached"
    assert not any(c.path == f"/channels/{CHANNEL_2}/invites" for c in fake.writes())


# ---------------------------------------------------------------------------
# 3. 403 on invite creation: reported, never retried
# ---------------------------------------------------------------------------


def test_403_on_invite_creation_is_reported_and_not_retried(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): reply(
                    403, {"message": "Missing Permissions", "code": 50013}
                )
            }
        )
    )
    report = run(fake)

    posts = [call for call in fake.calls if call.method == "POST"]
    assert len(posts) == 1, "a 403 is deterministic: one POST, no retry"
    assert not any(call.path.startswith("/invites/") for call in fake.calls)

    attempt = report.guilds[0].attempts[0]
    assert attempt.invite == "-"
    assert attempt.verdict == "BLOCKED"
    assert "permission" in attempt.join
    assert report.guilds[0].verdict.startswith("BLOCKED")
    assert report.exit_code() == 1

    text = burner_prime.report_text(report)
    assert "Missing Permissions" in text, "the response body is the evidence"
    assert "HTTP 403" in text, "the status is named next to the body"
    assert "invite creation in #general" in text


def test_source_without_invite_permission_is_blocked_before_any_write(
    sleeps, no_jitter
):
    fake = FakeHTTP(
        routes(
            extra={
                ("GET", "/users/@me/guilds", "source"): reply(
                    200, source_guilds(GUILD, permissions=0)
                )
            }
        )
    )
    report = run(fake)

    assert fake.writes() == []
    assert report.guilds[0].verdict.startswith("BLOCKED")
    assert "CREATE_INSTANT_INVITE" in report.guilds[0].verdict
    assert report.guilds[0].attempts == []


# ---------------------------------------------------------------------------
# 4/5. 429 handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("retry_after", "expected_wait"),
    [
        (5.0, 60.0),  # COOLDOWN_429 (60 s) beats retry_after + 1 s
        (300.0, 301.0),  # a long retry_after wins over the 60 s doubling
    ],
)
def test_429_with_retry_after_waits_then_continues(
    sleeps, no_jitter, retry_after, expected_wait
):
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): [
                    reply(
                        429,
                        {
                            "message": "You are being rate limited.",
                            "retry_after": retry_after,
                        },
                        retry_after=retry_after,
                    ),
                    reply(200, {"code": INVITE_CODE}),
                ],
                ("POST", ACCEPT_PATH, "burner"): reply(200, {"guild": {}}),
                ("DELETE", ACCEPT_PATH, "source"): reply(200, {}),
            }
        )
    )
    report = run(fake)

    assert report.waits == [expected_wait]
    assert expected_wait in sleeps, "the wait must be taken (recorded, not real)"
    assert len([c for c in fake.calls if c.path == INVITE_PATH]) == 2  # retried once
    assert report.guilds[0].verdict.startswith("OK")
    assert f"{expected_wait:.0f}s" in burner_prime.report_text(report)


def test_429_without_retry_after_aborts_the_run(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): reply(
                    429, {"message": "You are being rate limited.", "global": True}
                )
            }
        )
    )
    report = run(fake)

    assert "Retry-After" in report.aborted
    assert report.waits == [], "no Retry-After means no programmatic retry"
    assert len([c for c in fake.calls if c.path == INVITE_PATH]) == 1
    assert report.guilds[0].verdict.startswith("ABORTED")
    assert report.exit_code() == 1
    assert "stopped: 429 without a Retry-After header" in burner_prime.report_text(
        report
    )


def test_four_consecutive_429s_hard_stop_the_run(sleeps, no_jitter):
    limited = reply(
        429,
        {"message": "You are being rate limited.", "retry_after": 1.0},
        retry_after=1.0,
    )
    fake = FakeHTTP(routes(extra={("POST", INVITE_PATH, "source"): limited}))
    report = run(fake)

    # three waits (60 s doubling: 60, 120, 240) then a hard stop, exactly like
    # jobs.py, where MAX_429_STRIKES strikes are waited out and the next aborts
    assert report.waits == [60.0, 120.0, 240.0]
    assert len([c for c in fake.calls if c.path == INVITE_PATH]) == 4
    assert "consecutive 429s" in report.aborted
    assert f"PACE_MAX_429_STRIKES={burner_prime.MAX_429_STRIKES}" in report.aborted


# ---------------------------------------------------------------------------
# 401 stops everything, and every requested server still gets a row
# ---------------------------------------------------------------------------


def test_401_stops_the_whole_run_but_all_servers_are_reported(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("GET", "/users/@me/guilds", "source"): reply(
                    200, source_guilds(GUILD, OTHER_GUILD)
                ),
                ("GET", f"/guilds/{OTHER_GUILD}/channels", "source"): reply(
                    401, {"message": "401: Unauthorized", "code": 0}
                ),
                ("POST", INVITE_PATH, "source"): reply(200, {"code": INVITE_CODE}),
                ("POST", ACCEPT_PATH, "burner"): reply(200, {"guild": {}}),
                ("DELETE", ACCEPT_PATH, "source"): reply(200, {}),
            }
        )
    )
    report = run(fake, guild_ids=(GUILD, OTHER_GUILD))

    assert report.aborted.startswith("401")
    assert report.guilds[0].verdict.startswith("OK")
    assert report.guilds[1].verdict.startswith("ABORTED")
    assert len(report.guilds) == 2, "an aborted run still accounts for every server"
    assert not any(
        call.path == f"/channels/{CHANNEL_2}/invites" for call in fake.calls
    ), "nothing may be sent after a 401"


def test_401_on_the_burner_token_stops_the_run(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): reply(200, {"code": INVITE_CODE}),
                ("POST", ACCEPT_PATH, "burner"): reply(
                    401, {"message": "Unauthorized"}
                ),
            }
        )
    )
    report = run(fake)

    assert "401" in report.aborted and "burner" in report.aborted
    assert report.guilds[0].verdict.startswith("ABORTED")


# ---------------------------------------------------------------------------
# Blockers: named, with the response body attached
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, {}, "403 accept invite (no permission"),
        (403, {"message": "Missing Permissions", "code": 50013}, "permission"),
        (
            400,
            {"message": "Verification level too high", "code": 40002},
            "verification level",
        ),
        (
            400,
            {"message": "The server has reached its maximum number of members."},
            "member cap",
        ),
        (
            400,
            {"message": "Unknown Invite", "code": 10013},
            "invalid or expired invite",
        ),
        (400, {"message": "You are already a member of this server"}, "already member"),
        (
            400,
            {"message": "something Discord has never said before"},
            "400 accept invite",
        ),
        (404, {"message": "Not Found"}, "404"),
        (500, {"message": "Internal Server Error"}, "HTTP 500"),
    ],
)
def test_blockers_are_named(status, body, expected):
    assert expected in burner_prime.classify(status, body, target="accept invite")


def test_accept_failure_is_reported_with_the_body_and_the_invite_is_cleaned_up(
    sleeps, no_jitter
):
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): reply(200, {"code": INVITE_CODE}),
                ("POST", ACCEPT_PATH, "burner"): reply(
                    400, {"message": "Verification level too high", "code": 40002}
                ),
                ("DELETE", ACCEPT_PATH, "source"): reply(200, {}),
            }
        )
    )
    report = run(fake)

    attempt = report.guilds[0].attempts[0]
    assert attempt.verdict == "BLOCKED"
    assert attempt.join.startswith("blocked: ") and "verification" in attempt.join
    assert report.guilds[0].deleted == [INVITE_CODE], (
        "the invite is deleted on failure too"
    )
    text = burner_prime.report_text(report)
    assert "Verification level too high" in text
    assert report.exit_code() == 1


def test_rules_gate_on_a_200_join_is_a_partial(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): reply(200, {"code": INVITE_CODE}),
                ("POST", ACCEPT_PATH, "burner"): reply(
                    200, {"guild": {"id": str(GUILD)}, "show_verification_form": True}
                ),
                ("DELETE", ACCEPT_PATH, "source"): reply(200, {}),
            }
        )
    )
    report = run(fake)

    attempt = report.guilds[0].attempts[0]
    assert attempt.verdict == "PARTIAL"
    assert "rules gate" in attempt.join
    assert report.guilds[0].verdict.startswith("PARTIAL")
    text = burner_prime.report_text(report)
    assert "show_verification_form" in text
    assert report.exit_code() == 1


def test_invalid_invite_is_not_retried(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): reply(200, {"code": INVITE_CODE}),
                ("POST", ACCEPT_PATH, "burner"): reply(
                    400, {"message": "Unknown Invite", "code": 10013}
                ),
                ("DELETE", ACCEPT_PATH, "source"): reply(
                    404, {"message": "Unknown Invite"}
                ),
            }
        )
    )
    report = run(fake)

    assert (
        len([c for c in fake.calls if c.path == ACCEPT_PATH and c.role == "burner"])
        == 1
    )
    assert "invalid or expired invite" in report.guilds[0].attempts[0].join
    assert any("could not delete invite" in note for note in report.guilds[0].notes)


def test_5xx_accept_is_retried_once_before_giving_up(sleeps, no_jitter):
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): reply(200, {"code": INVITE_CODE}),
                ("POST", ACCEPT_PATH, "burner"): [
                    reply(503, {"message": "Service Unavailable"}),
                    reply(200, {"guild": {}}),
                ],
                ("DELETE", ACCEPT_PATH, "source"): reply(200, {}),
            }
        )
    )
    report = run(fake)

    assert report.guilds[0].verdict.startswith("OK")
    assert (
        len([c for c in fake.calls if c.path == ACCEPT_PATH and c.role == "burner"])
        == 2
    )
    assert burner_prime.JOIN_ATTEMPTS == 2


# ---------------------------------------------------------------------------
# Tokens never reach the report or the terminal
# ---------------------------------------------------------------------------


def test_no_token_appears_in_the_report_text(sleeps, no_jitter):
    """A body that echoes the token back must still come out redacted."""
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): reply(
                    400,
                    {
                        "message": f"invalid token {SOURCE_TOKEN} (burner {BURNER_TOKEN})"
                    },
                )
            }
        )
    )
    report = run(fake)
    text = burner_prime.report_text(report)

    assert SOURCE_TOKEN not in text
    assert BURNER_TOKEN not in text
    assert "***REDACTED***" in text
    assert "invalid token" in text, "the failure is still reported, just not the token"


def test_cli_prints_a_report_without_a_token(
    tmp_path, monkeypatch, sleeps, no_jitter, capsys
):
    fake = FakeHTTP(
        routes(
            extra={
                ("POST", INVITE_PATH, "source"): reply(
                    400, {"message": f"nope: {SOURCE_TOKEN} / {BURNER_TOKEN}"}
                )
            }
        )
    )
    monkeypatch.setattr(burner_prime, "DiscordHTTP", lambda **_kwargs: fake)

    source_env = tmp_path / "source.env"
    source_env.write_text(f"# comment\nDISCORD_TOKEN={SOURCE_TOKEN}\nSERVER_IDS=[]\n")
    burner_env = tmp_path / "burner.env"
    burner_env.write_text(f'DISCORD_TOKEN="{BURNER_TOKEN}"\n')

    code = burner_prime.main(
        [
            "--source-env",
            str(source_env),
            "--burner-env",
            str(burner_env),
            "--guilds",
            str(GUILD),
        ]
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert code == 1
    assert SOURCE_TOKEN not in output and BURNER_TOKEN not in output
    assert "***REDACTED***" in output
    assert f"Demo Lab ({GUILD})" in output


def test_cli_refuses_two_identical_tokens(tmp_path, monkeypatch, sleeps, no_jitter):
    monkeypatch.setattr(burner_prime, "DiscordHTTP", lambda **_kwargs: FakeHTTP({}))
    source_env = tmp_path / "source.env"
    burner_env = tmp_path / "burner.env"
    source_env.write_text(f"DISCORD_TOKEN={SOURCE_TOKEN}\n")
    burner_env.write_text(f"DISCORD_TOKEN={SOURCE_TOKEN}\n")

    with pytest.raises(SystemExit) as excinfo:
        burner_prime.main(
            [
                "--source-env",
                str(source_env),
                "--burner-env",
                str(burner_env),
                "--guilds",
                str(GUILD),
            ]
        )
    assert "same token" in str(excinfo.value)
    assert SOURCE_TOKEN not in str(excinfo.value)


def test_read_token_reports_the_file_but_never_the_value(tmp_path):
    good = tmp_path / "good.env"
    good.write_text(f'# discord\nDISCORD_TOKEN="{BURNER_TOKEN}"\n')
    assert burner_prime.read_token(good) == BURNER_TOKEN

    empty = tmp_path / "empty.env"
    empty.write_text("DISCORD_TOKEN=\n")
    with pytest.raises(SystemExit) as excinfo:
        burner_prime.read_token(empty)
    assert str(empty) in str(excinfo.value)

    with pytest.raises(SystemExit) as excinfo:
        burner_prime.read_token(tmp_path / "absent.env")
    assert "absent.env" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Planning helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("permissions", [0x20, 0x8, 0x1, 0x29])
def test_invite_permission_bits_allow_creation(permissions):
    allowed, why = burner_prime.can_invite({"permissions": str(permissions)})
    assert allowed and why == ""


def test_missing_invite_permission_and_missing_guild_are_refused():
    allowed, why = burner_prime.can_invite({"permissions": "0"})
    assert not allowed and "CREATE_INSTANT_INVITE" in why

    allowed, why = burner_prime.can_invite(None)
    assert not allowed and "not in this server" in why

    allowed, _ = burner_prime.can_invite({"permissions": None})
    assert allowed, "an unreported permission set is not proof of a block"


def test_pick_channels_prefers_text_and_reports_what_it_skipped():
    channels = [
        channel(VOICE, "voice", 2),
        channel(CHANNEL, "general"),
        channel(CHANNEL_2, "chat"),
    ]

    chosen, notes = burner_prime.pick_channels(channels, None)
    assert [c["id"] for c in chosen] == [str(CHANNEL)]
    assert any("other invite-capable text channel" in note for note in notes)

    chosen, notes = burner_prime.pick_channels(channels, [CHANNEL_2, VOICE, 999])
    assert [c["id"] for c in chosen] == [str(CHANNEL_2)]
    assert any("not visible to the source" in note for note in notes)
    assert any("not text" in note for note in notes)


def test_parse_channel_map_handles_guild_groups_and_a_bare_wildcard():
    assert burner_prime.parse_channel_map("1426301184846594282:111,222;333") == {
        1426301184846594282: [111, 222],
        0: [333],
    }
    assert burner_prime.parse_channel_map(None) == {}
    with pytest.raises(SystemExit):
        burner_prime.parse_channel_map("not-a-guild:111")


def test_parse_guild_ids_dedupes_and_rejects_junk():
    assert burner_prime.parse_guild_ids(f"{GUILD},{OTHER_GUILD},{GUILD}") == [
        GUILD,
        OTHER_GUILD,
    ]
    with pytest.raises(SystemExit):
        burner_prime.parse_guild_ids("1426301184846594282,oops")


def test_pacing_numbers_come_from_the_jobs_module():
    """The policy lives in one place: no PACE_* number is redefined here."""
    from discord_recall.web import jobs

    assert burner_prime.PACE_DELAY == jobs.PACE_DELAY
    assert burner_prime.PACE_JITTER_EXTRA == jobs.PACE_JITTER_EXTRA
    assert burner_prime.COOLDOWN_429 == jobs.COOLDOWN_429
    assert burner_prime.MAX_429_STRIKES == jobs.MAX_429_STRIKES
