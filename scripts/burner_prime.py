#!/usr/bin/env python3
"""Prime a burner account into the servers a source account already belongs to.

Why: a second account (the "burner") has to end up in the same servers as the
primary one (the "source"). Discord's only self-serve path is an invite, so for
each target server the source mints a single-use invite in a channel it may
invite from, the burner accepts it, and the invite is deleted again.

    # look before you leap: reads only, no write request is ever sent
    uv run python scripts/burner_prime.py \
        --source-env .env --burner-env .env.burner \
        --guilds 1426301184846594282,1391832426048651334 --dry-run

    # for real: one join per server, at most 4 joins overall
    uv run python scripts/burner_prime.py \
        --source-env .env --burner-env .env.burner \
        --guilds 1426301184846594282,1391832426048651334 --max-joins 4

    # pin the channels (GUILD:CHAN,... groups separated by ";")
    uv run python scripts/burner_prime.py \
        --source-env .env --burner-env .env.burner \
        --guilds 1426301184846594282 \
        --channels 1426301184846594282:111111111111111111,222222222222222222

Pacing is not re-invented here: ``PACE_DELAY`` / ``PACE_JITTER_EXTRA`` /
``COOLDOWN_429`` / ``MAX_429_STRIKES`` are imported from
:mod:`discord_recall.web.jobs`, so the policy that protects a self-bot token
lives in exactly one place. One request is in flight at a time, each completed
step is followed by ``PACE_DELAY`` + 0..``PACE_JITTER_EXTRA`` s of jitter
(jitter only ever adds), a 401 stops the whole run, a 403 aborts that server, and
a 429 waits ``max(retry_after + 1 s, COOLDOWN_429 * 2**(strike-1))`` - aborting on
a 429 with no ``Retry-After`` or once the consecutive strikes exceed
``MAX_429_STRIKES``.

Tokens are read from the ``DISCORD_TOKEN=`` line of the two env files and are
never printed, logged or committed to the report: every character of the report
passes through :class:`Redactor`, which scrubs both tokens.

All HTTP lives in :class:`DiscordHTTP`; tests inject a fake with the same
``request`` coroutine and never touch the network or a real token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
# `python scripts/burner_prime.py` must work without an editable install too.
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

import httpx  # noqa: E402 - imported after the src/ path fix above

from discord_recall.web.jobs import (  # noqa: E402 - see above
    COOLDOWN_429,
    MAX_429_STRIKES,
    PACE_DELAY,
    PACE_JITTER_EXTRA,
)

API = "https://discord.com/api/v9"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "chrome/146.0.0.0 Safari/537.36"
)

# Channel types an invite can be minted in: text + announcement.
TEXT_TYPES = {0, 5}
INVITE_MAX_AGE = 86400
INVITE_MAX_USES = 1
JOIN_ATTEMPTS = 2  # only transient (5xx) accept failures are retried

# Guild permission bits that make POST /channels/<id>/invites legal.
CREATE_INSTANT_INVITE = 0x1
MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x8
INVITE_BITS = CREATE_INSTANT_INVITE | MANAGE_GUILD | ADMINISTRATOR

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Discord's join/invite failure signatures, most specific first. The response
# body is always reported next to the label, because the label is a guess and
# the body is the evidence.
BLOCKER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"verification level|must be verified|verified email|phone number", re.I
        ),
        "blocked: the server's verification level is too high for this account",
    ),
    (
        re.compile(
            r"verification form|membership screening|rules (?:gate|screen)|agree to the rules",
            re.I,
        ),
        "blocked: rules/membership-screening gate",
    ),
    (
        re.compile(
            r"maximum number of members|member limit|guild is full|too many members|max_members",
            re.I,
        ),
        "blocked: the server is full (member cap reached)",
    ),
    (
        re.compile(
            r"unknown invite|invalid invite|invite is invalid|has expired|expired", re.I
        ),
        "blocked: invalid or expired invite",
    ),
    (re.compile(r"banned", re.I), "blocked: the burner is banned from this server"),
    (
        re.compile(r"already a member|already in this (?:guild|server)", re.I),
        "already member",
    ),
    (
        re.compile(r"missing permissions|no permission", re.I),
        "blocked: missing permission for this action",
    ),
)


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    """One invite attempt: channel -> invite code -> burner join result."""

    channel: str
    channel_id: int
    invite: str = "-"
    join: str = "-"
    verdict: str = ""
    body: str = ""


@dataclass
class GuildOutcome:
    guild_id: int
    name: str
    attempts: list[Attempt] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    verdict: str = ""
    # populated by --dry-run only
    planned: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    dry_run: bool
    guilds: list[GuildOutcome] = field(default_factory=list)
    secrets: tuple[str, ...] = ()
    requests: int = 0
    waits: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    aborted: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.aborted) or any(
            g.verdict.startswith(("BLOCKED", "PARTIAL", "ABORTED")) for g in self.guilds
        )

    def exit_code(self) -> int:
        return 1 if self.failed else 0


class Redactor:
    """Scrubs secrets from report text; the last line of defence for tokens."""

    def __init__(self, secrets: tuple[str, ...] | list[str]) -> None:
        self._secrets = tuple(s for s in secrets if s)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***REDACTED***")
        return text


def clip(value: object, limit: int = 220) -> str:
    """Compact, single-line rendering of a response body for the report."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def classify(status: int, body: object, *, target: str) -> str:
    """Name the blocker: status + body give the reason, never a bare number."""
    text = clip(body, 400)
    for pattern, label in BLOCKER_PATTERNS:
        if pattern.search(text):
            return label
    if status == 401:
        return "blocked: 401 token rejected"
    if status == 403:
        return f"blocked: 403 {target} (no permission for this account)"
    if status == 400:
        return f"blocked: 400 {target}"
    if status == 404:
        return f"blocked: 404 {target} (unknown invite/channel)"
    return f"blocked: HTTP {status} on {target}"


# ---------------------------------------------------------------------------
# The one place HTTP happens
# ---------------------------------------------------------------------------


@dataclass
class Reply:
    """A minimal, transport-free view of one API response."""

    status: int
    method: str
    path: str
    data: object | None = None
    text: str = ""
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def message(self) -> str:
        if isinstance(self.data, dict):
            return str(self.data.get("message") or "")
        return ""

    @property
    def code(self) -> int | None:
        if isinstance(self.data, dict) and isinstance(self.data.get("code"), int):
            return self.data["code"]
        return None

    def excerpt(self) -> str:
        return clip(self.data if self.data is not None else self.text)


def _retry_after_of(response: httpx.Response, data: object) -> float | None:
    """Seconds to wait, from the Retry-After header or the JSON body."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            return None
    if isinstance(data, dict):
        value = data.get("retry_after")
        if isinstance(value, (int, float)):
            return float(value)
    return None


class DiscordHTTP:
    """The only HTTP client in this script, so tests can inject a fake one."""

    def __init__(
        self,
        *,
        base_url: str = API,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> DiscordHTTP:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        json: dict | None = None,
    ) -> Reply:
        headers = {"Authorization": token, "User-Agent": UA}
        response = await self._client.request(
            method, f"{self._base}{path}", headers=headers, json=json
        )
        try:
            data: object | None = response.json()
        except ValueError:
            data = None
        return Reply(
            status=response.status_code,
            method=method.upper(),
            path=path,
            data=data,
            text=response.text[:400],
            retry_after=_retry_after_of(response, data),
        )


class StopRun(RuntimeError):
    """The whole run must stop: a rejected token, or 429s past the policy."""


class Pacer:
    """Sequential, jittered, 429-aware wrapper around the HTTP client.

    Delays and strike limits come from :mod:`discord_recall.web.jobs`; nothing
    here invents a number. ``sleep`` is injectable so tests never wait.
    """

    def __init__(
        self,
        client: object,
        tokens: dict[str, str],
        *,
        sleep: object | None = None,
        jitter: object | None = None,
        log: object | None = None,
    ) -> None:
        self.client = client
        self.tokens = dict(tokens)
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._jitter = jitter if jitter is not None else random.uniform
        self._log = log if log is not None else (lambda _msg: None)
        self.strikes = 0
        self.requests = 0
        self.waits: list[float] = []
        self.stop_reason = ""

    async def step_pause(self) -> None:
        """Base delay plus jitter that only ever adds (same rule as jobs.py)."""
        await self._sleep(PACE_DELAY + self._jitter(0.0, PACE_JITTER_EXTRA))

    async def call(
        self,
        method: str,
        path: str,
        *,
        role: str = "source",
        json: dict | None = None,
    ) -> Reply:
        """One request at a time, with the 401/429 policy applied."""
        while True:
            reply = await self.client.request(
                method, path, token=self.tokens[role], json=json
            )
            self.requests += 1

            if reply.status == 401:
                self.stop_reason = f"401: Discord rejected the {role} token"
                raise StopRun(self.stop_reason)

            if reply.status == 429:
                self.strikes += 1
                if self.strikes > MAX_429_STRIKES:
                    self.stop_reason = (
                        f"{self.strikes - 1} consecutive 429s "
                        f"(PACE_MAX_429_STRIKES={MAX_429_STRIKES})"
                    )
                    raise StopRun(self.stop_reason)
                if reply.retry_after is None:
                    self.stop_reason = (
                        "429 without a Retry-After header - policy says do not "
                        "retry programmatically"
                    )
                    raise StopRun(self.stop_reason)
                wait = max(
                    float(reply.retry_after) + 1.0,
                    COOLDOWN_429 * (2 ** (self.strikes - 1)),
                )
                self.waits.append(wait)
                self._log(
                    f"rate limited on {method} {path}; waiting {wait:.0f}s "
                    f"(strike {self.strikes}/{MAX_429_STRIKES})"
                )
                await self._sleep(wait)
                continue

            self.strikes = 0  # only consecutive 429s count
            await self.step_pause()
            return reply


# ---------------------------------------------------------------------------
# Planning helpers (pure: no HTTP)
# ---------------------------------------------------------------------------


def read_token(path: Path) -> str:
    """Read DISCORD_TOKEN= from an env file. The value is never logged."""
    if not path.is_file():
        raise SystemExit(f"env file not found: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("DISCORD_TOKEN="):
            continue
        value = line.split("=", 1)[1].strip().strip("'\"")
        if value:
            return value
    raise SystemExit(f"DISCORD_TOKEN= is missing or empty in {path}")


def parse_guild_ids(argument: str) -> list[int]:
    ids: list[int] = []
    for part in argument.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit():
            raise SystemExit(
                f"--guilds expects comma-separated snowflakes, got {part!r}"
            )
        gid = int(part)
        if gid not in ids:
            ids.append(gid)
    if not ids:
        raise SystemExit("--guilds must list at least one server id")
    return ids


def parse_channel_map(argument: str | None) -> dict[int, list[int]]:
    """Parse ``GUILD:CHAN,CHAN;GUILD:CHAN`` (a bare ``CHAN,CHAN`` means all)."""
    if not argument:
        return {}
    mapping: dict[int, list[int]] = {}
    for group in argument.replace(" ", "").split(";"):
        if not group:
            continue
        guild_part, _, channel_part = group.partition(":")
        if not channel_part:  # no guild prefix: applies to every --guilds entry
            guild_part, channel_part = "0", guild_part
        if guild_part != "0" and not guild_part.isdigit():
            raise SystemExit(f"--channels expects GUILD:CHAN,... groups, got {group!r}")
        channels = [int(c) for c in channel_part.split(",") if c]
        if not channels:
            raise SystemExit(f"--channels group {group!r} lists no channel")
        mapping.setdefault(int(guild_part), []).extend(channels)
    return mapping


def can_invite(guild: dict | None) -> tuple[bool, str]:
    """Can the source mint an invite here, judging by /users/@me/guilds?"""
    if guild is None:
        return False, "the source account is not in this server"
    permissions = guild.get("permissions")
    if permissions is None:
        return True, ""
    try:
        bits = int(permissions)
    except (TypeError, ValueError):
        return True, ""
    if bits & INVITE_BITS:
        return True, ""
    return (
        False,
        "no CREATE_INSTANT_INVITE (0x1), MANAGE_GUILD (0x20) or ADMINISTRATOR "
        "(0x8) in this server",
    )


def pick_channels(
    channels: list[dict], wanted: list[int] | None
) -> tuple[list[dict], list[str]]:
    """Choose candidate channels: the explicit list, else the first text channel."""
    by_id = {int(ch["id"]): ch for ch in channels if ch.get("id")}
    text_types = [ch for ch in channels if ch.get("type") in TEXT_TYPES]
    notes: list[str] = []
    if wanted:
        chosen: list[dict] = []
        for cid in wanted:
            channel = by_id.get(cid)
            if channel is None:
                notes.append(f"channel {cid} is not visible to the source (skipped)")
            elif channel.get("type") not in TEXT_TYPES:
                notes.append(
                    f"channel {cid} is type {channel.get('type')}, not text (skipped)"
                )
            else:
                chosen.append(channel)
        return chosen, notes
    if not text_types:
        return [], [
            "no invite-capable text channel (type 0/5) is visible to the source"
        ]
    notes.append(
        f"{len(text_types) - 1} other invite-capable text channel(s) left alone "
        "(one invite per server unless --channels says otherwise)"
    )
    return [text_types[0]], notes


def channel_label(channel: dict) -> str:
    return f"#{channel.get('name') or channel.get('id')} ({channel.get('id')})"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def prime(
    *,
    source_token: str,
    burner_token: str,
    guild_ids: list[int],
    client: object,
    channels: dict[int, list[int]] | None = None,
    dry_run: bool = False,
    max_joins: int | None = None,
    sleep: object | None = None,
    jitter: object | None = None,
    log: object | None = None,
) -> RunReport:
    """Make the burner join each guild, one invite at a time. Returns the report."""
    report = RunReport(
        dry_run=dry_run,
        secrets=(source_token, burner_token),
        waits=[],
    )
    if source_token == burner_token:
        raise SystemExit("--source-env and --burner-env hold the same token; refusing")

    mapping = dict(channels or {})
    # A bare `--channels 111,222` (no guild prefix) is stored under guild id 0,
    # which cannot be a real snowflake, and applies to every server.
    wildcard = mapping.pop(0, [])
    join_budget = len(guild_ids) if max_joins is None else max(0, int(max_joins))
    join_cap = join_budget

    pacer = Pacer(
        client,
        {"source": source_token, "burner": burner_token},
        sleep=sleep,
        jitter=jitter,
        log=log,
    )

    # One row per requested server, so that whatever happens every server the
    # user asked about is accounted for in the report.
    report.guilds = [
        GuildOutcome(guild_id=guild_id, name=f"guild {guild_id}")
        for guild_id in guild_ids
    ]

    try:
        source_reply = await pacer.call("GET", "/users/@me/guilds", role="source")
        if not source_reply.ok:
            raise StopRun(
                "could not read the source's servers: "
                + classify(
                    source_reply.status,
                    source_reply.data,
                    target="GET /users/@me/guilds",
                )
            )
        guild_by_id = {
            int(g["id"]): g
            for g in (source_reply.data or [])
            if isinstance(g, dict) and g.get("id")
        }

        burner_reply = await pacer.call("GET", "/users/@me/guilds", role="burner")
        burner_guilds: set[int] = set()
        if burner_reply.ok:
            burner_guilds = {
                int(g["id"])
                for g in (burner_reply.data or [])
                if isinstance(g, dict) and g.get("id")
            }
        else:
            report.notes.append(
                "the burner's server list could not be read "
                f"(HTTP {burner_reply.status}): already-member pre-check skipped"
            )

        for outcome in report.guilds:
            guild_id = outcome.guild_id
            guild = guild_by_id.get(guild_id)
            outcome.name = str((guild or {}).get("name") or f"guild {guild_id}")

            allowed, why = can_invite(guild)
            if not allowed:
                outcome.verdict = f"BLOCKED - {why}"
                outcome.notes.append(
                    "nothing was sent: the source cannot create invites here"
                )
                continue

            wanted = mapping.get(guild_id) or wildcard or None
            listed = await pacer.call(
                "GET", f"/guilds/{guild_id}/channels", role="source"
            )
            if not listed.ok:
                outcome.verdict = "ABORTED - " + classify(
                    listed.status,
                    listed.data,
                    target="GET /guilds/<id>/channels",
                )
                outcome.notes.append(clip(listed.data))
                continue

            chosen, notes = pick_channels(list(listed.data or []), wanted)
            outcome.notes.extend(notes)
            if not chosen:
                outcome.verdict = "BLOCKED - no usable text channel for the invite"
                continue

            outcome.planned = [channel_label(ch) for ch in chosen]

            if guild_id in burner_guilds:
                outcome.verdict = "NO-OP - burner is already a member"
                outcome.attempts.append(
                    Attempt(
                        channel="(none)",
                        channel_id=0,
                        join="already member",
                        verdict="NO-OP",
                    )
                )
                continue

            if dry_run:
                outcome.verdict = "PLANNED - dry run, nothing written"
                continue

            if join_budget <= 0:
                outcome.verdict = f"SKIPPED - --max-joins {join_cap} already reached"
                continue

            for channel in chosen:
                attempt = Attempt(
                    channel=channel_label(channel), channel_id=int(channel["id"])
                )
                outcome.attempts.append(attempt)

                created = await pacer.call(
                    "POST",
                    f"/channels/{attempt.channel_id}/invites",
                    role="source",
                    json={
                        "max_age": INVITE_MAX_AGE,
                        "max_uses": INVITE_MAX_USES,
                        "unique": True,
                        "temporary": False,
                    },
                )
                if not created.ok:
                    attempt.join = classify(
                        created.status, created.data, target="create invite"
                    )
                    attempt.body = created.excerpt()
                    attempt.verdict = "BLOCKED"
                    # 403 here is deterministic (no permission in that channel):
                    # it is reported and never retried.
                    outcome.notes.append(
                        f"invite creation in {attempt.channel} -> HTTP {created.status}"
                    )
                    continue

                code = str((created.data or {}).get("code") or "")
                attempt.invite = code or "-"
                if not code:
                    attempt.join = "blocked: invite response carried no code"
                    attempt.body = created.excerpt()
                    attempt.verdict = "BLOCKED"
                    continue

                accepted = None
                for attempt_no in range(1, JOIN_ATTEMPTS + 1):
                    accepted = await pacer.call(
                        "POST", f"/invites/{code}", role="burner"
                    )
                    if (
                        accepted.ok
                        or accepted.status < 500
                        or attempt_no == JOIN_ATTEMPTS
                    ):
                        break
                assert accepted is not None

                if accepted.ok:
                    gated = bool(
                        isinstance(accepted.data, dict)
                        and accepted.data.get("show_verification_form")
                    )
                    if gated:
                        attempt.join = "joined (rules gate: membership screening form)"
                        attempt.body = accepted.excerpt()
                        attempt.verdict = "PARTIAL"
                        outcome.verdict = (
                            "PARTIAL - joined, but the server asks for the rules "
                            "form to be accepted in the client"
                        )
                    else:
                        attempt.join = "joined"
                        attempt.verdict = "OK"
                        outcome.verdict = f"OK - burner joined via {attempt.channel}"
                else:
                    attempt.join = classify(
                        accepted.status, accepted.data, target="accept invite"
                    )
                    attempt.body = accepted.excerpt()
                    attempt.verdict = "BLOCKED"
                    outcome.verdict = f"BLOCKED - {attempt.join}"

                dropped = await pacer.call("DELETE", f"/invites/{code}", role="source")
                if dropped.ok:
                    outcome.deleted.append(code)
                    outcome.notes.append(f"invite {code} deleted with the source token")
                else:
                    outcome.notes.append(
                        f"could not delete invite {code}: HTTP {dropped.status}"
                    )

                if attempt.verdict == "OK":
                    join_budget -= 1
                    break
                if attempt.verdict == "PARTIAL":
                    join_budget -= 1
                    break
                # blocked on this channel: try the next candidate, if any

            if not outcome.verdict:
                outcome.verdict = "BLOCKED - no candidate channel worked"

    except StopRun as exc:
        report.aborted = str(exc)
        for outcome in report.guilds:
            if not outcome.verdict:
                outcome.verdict = f"ABORTED - {exc}"
    finally:
        report.requests = pacer.requests
        report.waits = list(pacer.waits)

    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(
            "  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        )
    return lines


def report_text(report: RunReport) -> str:
    """The whole report as text, with both tokens scrubbed out."""
    redact = Redactor(report.secrets)
    lines: list[str] = []
    lines.append(
        "burner-prime: "
        + ("dry run (no write request is sent)" if report.dry_run else "live run")
    )

    for index, guild in enumerate(report.guilds, start=1):
        lines.append("")
        lines.append(
            f"== [{index}/{len(report.guilds)}] {guild.name} ({guild.guild_id}) =="
        )
        if report.dry_run and guild.planned:
            lines.append("  plan: " + ", ".join(guild.planned))
        if guild.attempts:
            rows = [
                [
                    attempt.channel,
                    attempt.invite,
                    attempt.join,
                    attempt.verdict,
                ]
                for attempt in guild.attempts
            ]
            lines.extend(_table(["channel", "invite", "burner join", "verdict"], rows))
            for attempt in guild.attempts:
                if attempt.body:
                    lines.append(f"  body: {attempt.body}")
        elif report.dry_run and guild.planned:
            lines.append("  burner join: not attempted (dry run)")
        for note in guild.notes:
            lines.append(f"  note: {note}")
        if guild.deleted:
            lines.append(f"  invites deleted: {', '.join(guild.deleted)}")
        lines.append(f"  verdict: {guild.verdict or 'nothing attempted'}")

    joined = sum(
        1
        for guild in report.guilds
        if guild.verdict.startswith(("OK", "NO-OP", "PARTIAL", "PLANNED"))
    )
    blocked = sum(1 for guild in report.guilds if guild.verdict.startswith("BLOCKED"))
    for note in report.notes:
        lines.append(f"note: {note}")
    lines.append("")
    lines.append(
        f"summary: {joined}/{len(report.guilds)} servers satisfied, {blocked} blocked, "
        f"{report.requests} request(s), {len(report.waits)} rate-limit wait(s)"
    )
    if report.waits:
        lines.append("  waits: " + ", ".join(f"{wait:.0f}s" for wait in report.waits))
    if report.aborted:
        lines.append(f"  stopped: {report.aborted}")

    return redact("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source-env",
        required=True,
        help="Env file with the source (already-in-the-servers) DISCORD_TOKEN=.",
    )
    parser.add_argument(
        "--burner-env",
        required=True,
        help="Env file with the burner (must-join) DISCORD_TOKEN=.",
    )
    parser.add_argument(
        "--guilds",
        required=True,
        help="Comma-separated server ids to put the burner into.",
    )
    parser.add_argument(
        "--channels",
        default=None,
        help=(
            "Optional channel pins, 'GUILD:CHAN,CHAN;GUILD:CHAN'. A group without "
            "a guild prefix applies to every --guilds entry."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and print the plan; never send a write request.",
    )
    parser.add_argument(
        "--max-joins",
        type=int,
        default=None,
        help="Cap the number of successful joins (default: one per server).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_token = read_token(Path(args.source_env))
    burner_token = read_token(Path(args.burner_env))
    guild_ids = parse_guild_ids(args.guilds)
    channels = parse_channel_map(args.channels)

    async def _run() -> RunReport:
        async with DiscordHTTP() as client:
            return await prime(
                source_token=source_token,
                burner_token=burner_token,
                guild_ids=guild_ids,
                client=client,
                channels=channels,
                dry_run=args.dry_run,
                max_joins=args.max_joins,
                log=lambda message: print(f"  {message}", file=sys.stderr),
            )

    report = asyncio.run(_run())
    print(report_text(report))
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
