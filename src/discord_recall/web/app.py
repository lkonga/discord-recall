"""Read-only web UI for consuming digests.

Serves the local store: channels, digests per channel/date, an ask box, and
a per-cell "generate digest" action. Designed to sit behind loopback +
`tailscale serve`, so it has no authentication of its own.

Run:  uv run discord-recall-web        (or uv run uvicorn discord_recall.web.app:app)
"""

from __future__ import annotations

import html
import os
import pathlib
from datetime import datetime, timezone

from fastapi import FastAPI, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from urllib.parse import quote_plus

from discord_recall.db import get_session_factory
from discord_recall.db.models import Channel, Digest, Message, Server
from sqlalchemy import desc, func, or_, select

app = FastAPI(title="Discord Recall", docs_url=None, redoc_url=None)

from discord_recall.web.api import router as api_router  # noqa: E402

app.include_router(api_router)

_DIST = pathlib.Path(
    os.environ.get(
        "WEB_DIST",
        str(pathlib.Path(__file__).resolve().parents[3] / "web" / "dist"),
    )
)



from discord_recall.web.runner import last_line as _last_line  # noqa: E402
from discord_recall.web.runner import run_cli  # noqa: E402

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ font: 15px/1.55 system-ui, sans-serif; background:#1e1f22; color:#dbdee1; margin:0; }}
a {{ color:#00a8fc; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
header {{ padding:14px 20px; border-bottom:1px solid #2b2d31; display:flex; gap:18px; align-items:center; }}
header b {{ color:#f2f3f5; }} .muted {{ color:#949ba4; font-size:13px; }}
main {{ padding:20px; max-width:900px; }}
table {{ border-collapse:collapse; width:100%; }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #2b2d31; }}
th {{ color:#949ba4; font-weight:600; font-size:13px; }}
.digest {{ white-space:pre-wrap; background:#2b2d31; border-left:3px solid #5865f2; padding:14px 16px; border-radius:6px; margin:10px 0 26px; }}
form {{ margin:18px 0; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
input, select, button {{ background:#313338; color:#dbdee1; border:1px solid #3f4147; border-radius:5px; padding:7px 9px; font-size:14px; }}
button {{ background:#5865f2; border-color:#5865f2; color:#fff; cursor:pointer; }}
pre {{ background:#2b2d31; padding:12px; border-radius:6px; white-space:pre-wrap; }}
</style></head><body>
<header><b><a href="/">Discord Recall</a></b>
<span class="muted">{subtitle}</span></header>
<main>{body}</main></body></html>"""


def render(title: str, body: str, subtitle: str = "") -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body, subtitle=subtitle))


def md_to_html(text: str) -> str:
    """Minimal markdown: headers, bold, bullets, links, code spans."""
    out = []
    for line in html.escape(text).splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            out.append(f"<b>{stripped[4:]}</b>")
        elif stripped.startswith("## "):
            out.append(f"<b>{stripped[3:]}</b>")
        elif stripped.startswith("# "):
            out.append(f"<b>{stripped[2:]}</b>")
        elif stripped.startswith(("- ", "* ")):
            out.append(f"&nbsp;&nbsp;• {stripped[2:]}")
        else:
            out.append(line)
    body = "\n".join(out)
    return body.replace("**", "<b>", 1).replace("**", "</b>", 1) if body.count("**") >= 2 else body


@app.get("/healthz")
async def healthz():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


@app.get("/", response_class=HTMLResponse)
async def index(msg: str = "", q: str = ""):
    """Serve the built React app when present, else the HTML fallback."""
    if (_DIST / "index.html").exists():
        # Never let a browser cache the shell: hashed assets are immutable, the
        # index must always point at the current build.
        return FileResponse(
            _DIST / "index.html", headers={"Cache-Control": "no-store, must-revalidate"}
        )
    return await html_index(msg=msg, q=q)


async def html_index(msg: str = "", q: str = ""):
    """Server-rendered fallback picker (used only without a frontend build)."""
    factory = get_session_factory()
    async with factory() as session:
        msg_count = (
            select(func.count(Message.id))
            .where(Message.channel_id == Channel.id)
            .correlate(Channel)
            .scalar_subquery()
        )
        last_msg = (
            select(func.max(Message.created_at))
            .where(Message.channel_id == Channel.id)
            .correlate(Channel)
            .scalar_subquery()
        )
        stmt = (
            select(
                Channel.id,
                Channel.name,
                Server.name,
                func.count(Digest.id),
                func.max(Digest.period_start),
                msg_count,
                last_msg,
            )
            .join(Server, Channel.server_id == Server.id)
            .outerjoin(Digest, Digest.channel_id == Channel.id)
            .group_by(Channel.id, Channel.name, Server.name)
        )
        if q:
            needle = f"%{q.lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Channel.name).like(needle),
                    func.lower(Server.name).like(needle),
                )
            )
        rows = (
            await session.execute(
                stmt.order_by(desc(msg_count), Server.name, Channel.name).limit(500)
            )
        ).all()
        channel_total = (
            await session.execute(select(func.count(Channel.id)))
        ).scalar_one()
        digest_total = (await session.execute(select(func.count(Digest.id)))).scalar_one()
        msg_total = (await session.execute(select(func.count(Message.id)))).scalar_one()

    body = [
        f"<p class='muted'>{channel_total} channels known · {msg_total} captured messages · "
        f"{digest_total} digests</p>"
    ]
    body.append(
        "<h3>1. Pick a channel</h3>"
        "<form method='get' action='/'>"
        f"<input name='q' size='28' placeholder='filter by channel or server name' value='{html.escape(q)}'>"
        "<button>filter</button></form>"
        "<form method='post' action='/discover'>"
        "<button>refresh channel list from Discord</button>"
        "<span class='muted'> (lists every text channel the account can see; ~1 request per server)</span>"
        "</form>"
    )
    if msg:
        body.append(f"<p class='muted'>{html.escape(msg)}</p>")

    shown = len(rows)
    body.append(
        f"<p class='muted'>showing {shown} channel(s)"
        + (f" matching {html.escape(q)!r}" if q else "")
        + (", capped at 500 - use the filter" if shown == 500 else "")
        + "</p>"
    )
    body.append(
        "<table><tr><th>Server</th><th>Channel</th><th>Messages</th><th>Last message</th>"
        "<th>Digests</th><th>Latest digest</th></tr>"
    )
    for cid, name, server, digests, latest, msgs, last_seen in rows:
        link = f"<a href='/channel/{cid}'>#{html.escape(name)}</a>"
        when = latest.strftime("%Y-%m-%d") if latest else "-"
        seen = last_seen.strftime("%Y-%m-%d") if last_seen else "-"
        body.append(
            f"<tr><td>{html.escape(server)}</td><td>{link}</td><td>{msgs}</td>"
            f"<td>{seen}</td><td>{digests}</td><td>{when}</td></tr>"
        )
    body.append("</table>")
    body.append(
        "<p class='muted'>Open a channel to run the flow: <b>1.</b> capture messages "
        "for a date range, <b>2.</b> build the digest for the days you want.</p>"
    )
    body.append(
        "<h3>Ask</h3><form method='post' action='/ask'>"
        "<input name='question' size='52' placeholder='what happened with X this week?'>"
        "<select name='channel_id'><option value=''>all channels</option>"
        + "".join(
            f"<option value='{cid}'>#{html.escape(name)}</option>"
            for cid, name, _, _, _, msgs, _ in rows
            if msgs
        )
        + "</select><button>ask</button></form>"
    )
    return render("Discord Recall", "".join(body), "channel picker")


@app.post("/discover")
async def discover_ui(server_id: str = Form("")):
    """Refresh the channel catalogue from Discord."""
    args = ["discover"]
    for sid in [s.strip() for s in server_id.split(",") if s.strip()]:
        args += ["--server", sid]
    rc, out = await run_cli(args)
    return RedirectResponse(
        f"/?msg={quote_plus(f'discover rc={rc}: {_last_line(out)}')}", status_code=303
    )


@app.get("/channel/{channel_id}", response_class=HTMLResponse)
async def channel(channel_id: int, limit: int = 30, msg: str = ""):
    factory = get_session_factory()
    async with factory() as session:
        ch = await session.get(Channel, channel_id)
        if ch is None:
            return render("Not found", "<p>Unknown channel.</p>")
        digests = (
            (
                await session.execute(
                    select(Digest)
                    .where(Digest.channel_id == channel_id)
                    .order_by(desc(Digest.period_start))
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        msg_total = (
            await session.execute(
                select(func.count(Message.id)).where(Message.channel_id == channel_id)
            )
        ).scalar_one()

    body = [
        f"<p class='muted'>{msg_total} captured messages · {len(digests)} digests</p>",
    ]
    if msg:
        body.append(f"<p class='muted'>{html.escape(msg)}</p>")
    body.append(
        "<h3>1. Capture messages</h3>"
        f"<form method='post' action='/capture/{channel_id}'>"
        "<label>since <input type='date' name='since'></label>"
        "<label>until <input type='date' name='until'></label>"
        "<label>max <input type='number' name='max_messages' value='600' min='50' "
        "step='50'></label><button>capture</button></form>"
    )
    body.append(
        "<h3>2. Build digest</h3>"
        f"<form method='post' action='/digest/{channel_id}'>"
        "<input type='date' name='date_from' required>"
        "<input type='date' name='date_to'>"
        "<select name='period'><option>daily</option><option>weekly</option>"
        "<option>monthly</option></select>"
        "<label><input type='checkbox' name='force' value='true'> rebuild</label>"
        "<button>build</button></form>"
    )
    body.append("<h3>Digests</h3>")
    body.append("<table><tr><th>Period</th><th>Start</th><th>Messages</th></tr>")
    for d in digests:
        body.append(
            f"<tr><td>{d.period}</td>"
            f"<td><a href='#d{d.id}'>{d.period_start.strftime('%Y-%m-%d')}</a></td>"
            f"<td>{d.message_count}</td></tr>"
        )
    body.append("</table>")
    for d in digests:
        body.append(f"<h3 id='d{d.id}'>{d.period} · {d.period_start.strftime('%Y-%m-%d')}</h3>")
        body.append(f"<div class='digest'>{md_to_html(d.content)}</div>")
    return render(f"#{ch.name}", "".join(body), f"#{html.escape(ch.name)}")


@app.post("/generate/{channel_id}")
async def generate(channel_id: int, day: str = Form(...)):
    """Build (or rebuild) one daily digest for a channel and date."""
    from discord_recall.digest.builder import build_daily_digest

    try:
        when = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return render("Bad date", "<p>Date must be YYYY-MM-DD.</p>")
    await build_daily_digest(channel_id, when)
    return RedirectResponse(f"/channel/{channel_id}", status_code=303)


async def _capture(channel_id: int, since: str, until: str, max_messages: int) -> str:
    args = ["backfill", "-c", str(channel_id), "--max-messages", str(max_messages)]
    if since:
        args += ["--since", since]
    if until:
        args += ["--until", until]
    rc, out = await run_cli(args)
    return f"capture rc={rc}: {_last_line(out)}"


@app.post("/capture/{channel_id}")
async def capture(
    channel_id: int,
    since: str = Form(""),
    until: str = Form(""),
    max_messages: int = Form(600),
):
    """Pull a bounded window of history for a channel (pick channel -> capture)."""
    status = await _capture(channel_id, since, until, max_messages)
    return RedirectResponse(
        f"/channel/{channel_id}?msg={quote_plus(status)}", status_code=303
    )


@app.post("/capture-new")
async def capture_new(
    channel_id: str = Form(...),
    since: str = Form(""),
    max_messages: int = Form(600),
):
    """Capture a channel by ID that is not in the store yet."""
    raw = channel_id.strip().split("/")[-1].split("?")[0]
    if not raw.isdigit():
        return render("Bad channel", "<p>Channel ID must be numeric.</p>")
    cid = int(raw)
    status = await _capture(cid, since, "", max_messages)
    return RedirectResponse(f"/channel/{cid}?msg={quote_plus(status)}", status_code=303)


@app.post("/digest/{channel_id}")
async def digest_range_ui(
    channel_id: int,
    date_from: str = Form(...),
    date_to: str = Form(""),
    period: str = Form("daily"),
    force: bool = Form(False),
):
    """Build digests for a channel across a date range."""
    args = ["digest-range", "-c", str(channel_id), "--from", date_from, "--period", period]
    if date_to:
        args += ["--to", date_to]
    if force:
        args += ["--force"]
    rc, out = await run_cli(args)
    return RedirectResponse(
        f"/channel/{channel_id}?msg={quote_plus(f'digest rc={rc}: {_last_line(out)}')}",
        status_code=303,
    )


@app.post("/ask", response_class=HTMLResponse)
async def ask(question: str = Form(...), channel_id: str = Form("")):
    from discord_recall.digest.query import ask as ask_query

    answer = await ask_query(
        question=question, channel_id=int(channel_id) if channel_id else None
    )
    body = (
        f"<p><b>{html.escape(question)}</b></p>"
        f"<div class='digest'>{md_to_html(answer)}</div>"
        f"<p><a href='/'>&larr; back</a></p>"
    )
    return render("Answer", body)


def main() -> None:  # pragma: no cover - entrypoint

    import uvicorn

    uvicorn.run(
        "discord_recall.web.app:app",
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEB_PORT", "9879")),
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()

# --- SPA hosting -----------------------------------------------------------
# The built React/shadcn app is mounted last so every /api route and /healthz
# keeps priority; its index.html is served for "/" by the root route above.
if (_DIST / "index.html").exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="spa")
