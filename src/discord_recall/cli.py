"""CLI entry point for discord-recall."""

import asyncio
import subprocess
import sys
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

import typer

app = typer.Typer(name="discord-recall", help="Personal Discord intelligence system.")


@app.command()
def listen():
    """Start real-time Discord message capture."""
    from discord_recall.capture.listener import run_listener

    run_listener()


@app.command()
def backfill(
    channel_id: int = typer.Option(None, "--channel", "-c", help="Backfill a single channel by ID."),
    server_id: int = typer.Option(None, "--server", "-s", help="Backfill all channels in a server by ID."),
    since: str = typer.Option(
        None, "--since", help="Only capture messages on/after this date (YYYY-MM-DD)."
    ),
    until: str = typer.Option(
        None, "--until", help="Only capture messages on/before this date (YYYY-MM-DD)."
    ),
    max_messages: int = typer.Option(
        None, "--max-messages", help="Stop after N messages (per channel for --server)."
    ),
    batch_size: int = typer.Option(
        None, "--batch-size", help="Messages per Discord request (default: config)."
    ),
    delay: float = typer.Option(
        None, "--delay", help="Seconds to wait between requests (default: config)."
    ),
):
    """Backfill message history for a channel or entire server.

    Add --since/--until/--max-messages for a bounded catch-up instead of
    walking the whole history:

      discord-recall backfill -c 111 --since 2026-09-17 --max-messages 1500
      discord-recall backfill -s 999 --until 2026-09-20 --batch-size 50 --delay 1.5
    """
    if not channel_id and not server_id:
        typer.echo("Provide --channel <id> or --server <id>")
        raise typer.Exit(1)

    from discord_recall.capture.backfill import run_backfill, window_bounds

    try:
        since_dt, until_dt = window_bounds(since, until)
    except ValueError:
        typer.echo("Dates must be YYYY-MM-DD", err=True)
        raise typer.Exit(1)

    run_backfill(
        channel_id=channel_id,
        server_id=server_id,
        since=since_dt,
        until=until_dt,
        max_messages=max_messages,
        batch_size=batch_size,
        delay=delay,
    )


@app.command()
def digest(
    server_id: int = typer.Option(None, "--server", "-s", help="Server ID to digest."),
    channel_id: int = typer.Option(None, "--channel", "-c", help="Single channel ID to digest."),
    date: str = typer.Option(None, "--date", "-d", help="Date to digest (YYYY-MM-DD). Default: yesterday."),
    send: bool = typer.Option(False, "--send", help="Send digest via Telegram."),
):
    """Generate a daily digest for a server or channel."""
    from discord_recall.digest.builder import build_daily_digest, build_server_daily_digest
    from discord_recall.digest.telegram import send_digest

    if date:
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        day = datetime.now(timezone.utc) - timedelta(days=1)

    async def _run():
        if server_id:
            text = await build_server_daily_digest(server_id, day)
        elif channel_id:
            d = await build_daily_digest(channel_id, day)
            text = d.content if d else None
        else:
            typer.echo("Provide --server <id> or --channel <id>")
            raise typer.Exit(1)

        if not text:
            typer.echo("No activity found for that period.")
            return

        typer.echo(text)

        if send:
            await send_digest(text)

    asyncio.run(_run())


@app.command(name="digest-range")
def digest_range(
    channel: list[int] = typer.Option(
        None, "--channel", "-c", help="Channel ID to digest. Repeat for several."
    ),
    server_id: int = typer.Option(
        None, "--server", "-s", help="Digest every stored channel of this server."
    ),
    date_from: str = typer.Option(
        None, "--from", help="Start date (YYYY-MM-DD), inclusive."
    ),
    date_to: str = typer.Option(
        None, "--to", help="End date (YYYY-MM-DD), inclusive. Default: same as --from."
    ),
    last: int = typer.Option(
        None, "--last", help="Convenience: digest the last N days, ending yesterday (UTC)."
    ),
    period: str = typer.Option(
        "daily", "--period", "-p", help="daily | weekly | monthly"
    ),
    send: bool = typer.Option(False, "--send", help="Send each digest via Telegram."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be built; no LLM calls."
    ),
    force: bool = typer.Option(
        False, "--force", help="Recompute digests even if one already exists."
    ),
    print_digests: bool = typer.Option(
        False, "--print", help="Print each generated digest to stdout."
    ),
):
    """Digest a channel selection across an entire date range.

    Examples:

      discord-recall digest-range -c 111 -c 222 --from 2026-09-01 --to 2026-09-14
      discord-recall digest-range --server 999 --period weekly --from 2026-06-01
      discord-recall digest-range -c 111 --last 7 --send
    """
    from discord_recall.digest.range import RangeError, parse_date, run_range
    from discord_recall.digest.telegram import send_digest

    def _fail(message: str) -> None:
        typer.echo(message, err=True)
        raise typer.Exit(1)

    today = datetime.now(timezone.utc).date()

    try:
        if last is not None:
            if date_from or date_to:
                _fail("--last cannot be combined with --from/--to")
            if last < 1:
                _fail("--last must be >= 1")
            start_day: date_cls = today - timedelta(days=last)
            end_day: date_cls = today - timedelta(days=1)
        else:
            if not date_from:
                _fail("Provide --from YYYY-MM-DD (with optional --to), or --last N")
            start_day = parse_date(date_from)
            end_day = parse_date(date_to) if date_to else start_day

        result = asyncio.run(
            run_range(
                channel_ids=channel,
                server_id=server_id,
                date_from=start_day,
                date_to=end_day,
                period=period,
                force=force,
                dry_run=dry_run,
            )
        )
    except RangeError as exc:
        _fail(str(exc))
        return

    if dry_run:
        if not result.planned:
            typer.echo("Nothing to do - no messages in that range.")
            return
        total = sum(p.message_count for p in result.planned)
        typer.echo(
            f"Would build {len(result.planned)} {period} digest(s) "
            f"covering {total} messages:"
        )
        for p in result.planned:
            typer.echo(
                f"  #{p.channel_name} ({p.channel_id})\t{p.label}\t{p.message_count} msgs"
            )
        return

    if print_digests:
        for _cid, name, label, content in result.digits:
            typer.echo(f"\n===== #{name} - {label} =====\n{content}")

    if send:
        for _cid, name, label, content in result.digits:
            asyncio.run(send_digest(f"# #{name} - {label}\n\n{content}"))

    typer.echo(
        f"{period} digests {start_day}..{end_day}: "
        f"{len(result.built)} built, {len(result.reused)} reused, "
        f"{len(result.empty)} empty, {len(result.failed)} failed"
    )
    for _cid, name, label, count in result.built:
        typer.echo(f"  built  #{name} {label} ({count} msgs)")
    if result.failed:
        for _cid, name, label, err in result.failed:
            typer.echo(f"  FAILED #{name} {label}: {err}", err=True)
        raise typer.Exit(1)


@app.command()
def ask(
    question: str = typer.Argument(..., help="Question to ask about the conversations."),
    server_id: int = typer.Option(None, "--server", "-s", help="Scope to a server."),
    channel_id: int = typer.Option(None, "--channel", "-c", help="Scope to a channel."),
    user: str = typer.Option(None, "--user", "-u", help="Filter to a specific user."),
):
    """Ask a question about Discord conversations."""
    from discord_recall.digest.query import ask as ask_query

    async def _run():
        answer = await ask_query(
            question=question,
            channel_id=channel_id,
            server_id=server_id,
            username=user,
        )
        typer.echo(answer)

    asyncio.run(_run())


@app.command(name="digest-backfill")
def digest_backfill(
    channel_id: int = typer.Option(..., "--channel", "-c", help="Channel ID to backfill digests for."),
):
    """Backfill digests: monthly (>1yr), weekly (last year), daily (today onward)."""
    from discord_recall.digest.builder import backfill_digests

    asyncio.run(backfill_digests(channel_id))


@app.command()
def discover(
    server_id: list[int] = typer.Option(
        None, "--server", "-s", help="Guild ID to scan. Repeat for several. Default: all."
    ),
    delay: float = typer.Option(0.4, "--delay", help="Seconds between guild requests."),
):
    """List every server and text channel this account can see.

    Unlike backfill, this makes no message requests: it only records the
    channel catalogue so the UI can offer a full picker.
    """
    from discord_recall.capture.discover import discover as run_discover

    servers, channels = asyncio.run(run_discover(server_id, delay=delay))
    typer.echo(f"discovered {servers} servers, {channels} text channels")


@app.command()
def migrate():
    """Run database migrations (alembic upgrade head)."""
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
