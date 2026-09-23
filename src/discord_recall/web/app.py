"""Read-only web UI for consuming digests.

Serves the local store: channels, digests per channel/date, an ask box, and
a per-cell "generate digest" action. Designed to sit behind loopback +
`tailscale serve`, so it has no authentication of its own.

Run:  uv run discord-recall-web        (or uv run uvicorn discord_recall.web.app:app)
"""

from __future__ import annotations

import html
import asyncio
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from discord_recall.db import get_session_factory
from discord_recall.db.models import Channel, Digest, Message, Server
from sqlalchemy import desc, func, select

app = FastAPI(title="Discord Recall", docs_url=None, redoc_url=None)

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
async def index():
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    Channel.id,
                    Channel.name,
                    Server.name,
                    func.count(Digest.id),
                    func.max(Digest.period_start),
                )
                .join(Server, Channel.server_id == Server.id)
                .outerjoin(Digest, Digest.channel_id == Channel.id)
                .group_by(Channel.id, Channel.name, Server.name)
                .order_by(desc(func.count(Digest.id)))
            )
        ).all()
        totals = (
            await session.execute(
                select(func.count(Digest.id), func.count(Message.id)).select_from(Digest)
            )
        ).one()
        msg_total = (await session.execute(select(func.count(Message.id)))).scalar_one()

    body = [f"<p class='muted'>{totals[0]} digests · {msg_total} captured messages</p>"]
    body.append("<table><tr><th>Server</th><th>Channel</th><th>Digests</th><th>Latest</th><th></th></tr>")
    for cid, name, server, count, latest in rows:
        if not count:
            continue
        link = f"<a href='/channel/{cid}'>#{html.escape(name)}</a>"
        when = latest.strftime("%Y-%m-%d") if latest else "-"
        gen = (
            f"<form method='post' action='/generate/{cid}'>"
            f"<input type='date' name='day' value='{when}'><button>digest that day</button></form>"
        )
        body.append(
            f"<tr><td>{html.escape(server)}</td><td>{link}</td><td>{count}</td>"
            f"<td>{when}</td><td>{gen}</td></tr>"
        )
    body.append("</table>")
    body.append(
        "<h3>Ask</h3><form method='post' action='/ask'>"
        "<input name='question' size='52' placeholder='what happened with X this week?'>"
        "<select name='channel_id'><option value=''>all channels</option>"
        + "".join(
            f"<option value='{cid}'>#{html.escape(name)}</option>"
            for cid, name, _, count, _ in rows
            if count
        )
        + "</select><button>ask</button></form>"
    )
    return render("Discord Recall", "".join(body), "local digests")


@app.get("/channel/{channel_id}", response_class=HTMLResponse)
async def channel(channel_id: int, limit: int = 30):
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
        "<table><tr><th>Period</th><th>Start</th><th>Messages</th></tr>",
    ]
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
    import os

    import uvicorn

    uvicorn.run(
        "discord_recall.web.app:app",
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEB_PORT", "9879")),
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
