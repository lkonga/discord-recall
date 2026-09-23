"""Discover servers and text channels the account can see.

Populates ``servers``/``channels`` from the Discord API so the UI can offer a
full channel picker instead of only channels that already have messages.

    uv run discord-recall discover                 # every guild
    uv run discord-recall discover --server 123    # one guild (repeatable)
"""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger
from sqlalchemy.dialects.sqlite import insert

from discord_recall.config import get_settings
from discord_recall.db import get_session_factory
from discord_recall.db.models import Channel, Server

API = "https://discord.com/api/v9"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "chrome/146.0.0.0 Safari/537.36"
)

# Channel types worth offering as digest sources: text + announcement.
TEXT_TYPES = {0, 5}


async def _get(client: httpx.AsyncClient, path: str, token: str, attempt: int = 1):
    headers = {"Authorization": token, "User-Agent": UA}
    resp = await client.get(f"{API}{path}", headers=headers)
    if resp.status_code == 429 and attempt <= 3:
        retry = float((resp.json() or {}).get("retry_after", 1.0))
        logger.warning(f"rate limited on {path}, sleeping {retry:.1f}s")
        await asyncio.sleep(retry + 0.5)
        return await _get(client, path, token, attempt + 1)
    resp.raise_for_status()
    return resp.json()


async def discover(
    server_ids: list[int] | None = None, delay: float = 0.4
) -> tuple[int, int]:
    """Fetch guilds + their text channels into the local store."""
    settings = get_settings()
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is not set in .env")

    session_factory = get_session_factory()
    wanted = {int(s) for s in (server_ids or [])}

    async with httpx.AsyncClient(timeout=30) as client:
        guilds = await _get(client, "/users/@me/guilds", settings.discord_token)
        if wanted:
            guilds = [g for g in guilds if int(g["id"]) in wanted]

        servers = channels = 0
        for guild in guilds:
            gid = int(guild["id"])
            try:
                chans = await _get(client, f"/guilds/{gid}/channels", settings.discord_token)
            except httpx.HTTPStatusError as exc:
                logger.warning(f"[{guild.get('name')}] channels unavailable: {exc.response.status_code}")
                continue

            async with session_factory() as session:
                await session.execute(
                    insert(Server)
                    .values(id=gid, name=guild.get("name") or str(gid))
                    .on_conflict_do_update(
                        index_elements=[Server.id],
                        set_=dict(name=guild.get("name") or str(gid)),
                    )
                )
                for ch in chans:
                    if ch.get("type") not in TEXT_TYPES:
                        continue
                    await session.execute(
                        insert(Channel)
                        .values(
                            id=int(ch["id"]),
                            server_id=gid,
                            name=ch.get("name") or str(ch["id"]),
                            type=str(ch.get("type")),
                            topic=ch.get("topic"),
                            position=ch.get("position"),
                        )
                        .on_conflict_do_update(
                            index_elements=[Channel.id],
                            set_=dict(
                                name=ch.get("name") or str(ch["id"]),
                                topic=ch.get("topic"),
                                position=ch.get("position"),
                            ),
                        )
                    )
                    channels += 1
                await session.commit()

            servers += 1
            logger.info(f"[{guild.get('name')}] {len(chans)} channels seen")
            await asyncio.sleep(delay)

    return servers, channels
