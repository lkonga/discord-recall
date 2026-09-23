"""Message ingestion — upsert logic for Discord messages into SQLite."""

from datetime import datetime, timezone

import discord
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from discord_recall.db.models import Attachment as AttachmentRow
from discord_recall.db.models import Author, Channel, Message, Server, Thread


async def upsert_server(session: AsyncSession, guild: discord.Guild) -> None:
    stmt = insert(Server).values(
        id=guild.id,
        name=guild.name,
        icon_url=str(guild.icon) if guild.icon else None,
        joined_at=guild.me.joined_at if guild.me else None,
    ).on_conflict_do_update(
        index_elements=[Server.id],
        set_=dict(
            name=guild.name,
            icon_url=str(guild.icon) if guild.icon else None,
        ),
    )
    await session.execute(stmt)


async def upsert_channel(session: AsyncSession, channel: discord.abc.GuildChannel) -> None:
    guild = channel.guild
    if guild is None:
        return

    await upsert_server(session, guild)

    stmt = insert(Channel).values(
        id=channel.id,
        server_id=guild.id,
        name=channel.name,
        type=str(channel.type),
        topic=getattr(channel, "topic", None),
        position=getattr(channel, "position", None),
    ).on_conflict_do_update(
        index_elements=[Channel.id],
        set_=dict(
            name=channel.name,
            type=str(channel.type),
            topic=getattr(channel, "topic", None),
            position=getattr(channel, "position", None),
        ),
    )
    await session.execute(stmt)


async def upsert_thread(session: AsyncSession, thread: discord.Thread) -> None:
    await upsert_channel(session, thread.parent)

    stmt = insert(Thread).values(
        id=thread.id,
        channel_id=thread.parent_id,
        name=thread.name,
        archived=thread.archived,
        locked=thread.locked,
        thread_created_at=thread.created_at,
    ).on_conflict_do_update(
        index_elements=[Thread.id],
        set_=dict(
            name=thread.name,
            archived=thread.archived,
            locked=thread.locked,
        ),
    )
    await session.execute(stmt)


async def upsert_author(session: AsyncSession, user: discord.User | discord.Member) -> None:
    stmt = insert(Author).values(
        id=user.id,
        username=user.name,
        display_name=user.display_name,
        avatar_url=str(user.avatar) if user.avatar else None,
        is_bot=user.bot,
    ).on_conflict_do_update(
        index_elements=[Author.id],
        set_=dict(
            username=user.name,
            display_name=user.display_name,
            avatar_url=str(user.avatar) if user.avatar else None,
        ),
    )
    await session.execute(stmt)


async def upsert_message(session: AsyncSession, msg: discord.Message) -> None:
    """Upsert a full Discord message and all its related entities."""
    channel = msg.channel

    is_thread = isinstance(channel, discord.Thread)

    if is_thread:
        await upsert_thread(session, channel)
        channel_id = channel.parent_id
        thread_id = channel.id
    elif hasattr(channel, "guild") and channel.guild is not None:
        await upsert_channel(session, channel)
        channel_id = channel.id
        thread_id = None
    else:
        return

    await upsert_author(session, msg.author)

    reply_to_id = None
    if msg.reference and msg.reference.message_id:
        reply_to_id = msg.reference.message_id

    embeds_data = [e.to_dict() for e in msg.embeds] if msg.embeds else None

    stmt = insert(Message).values(
        id=msg.id,
        channel_id=channel_id,
        thread_id=thread_id,
        author_id=msg.author.id,
        content=msg.content or "",
        created_at=msg.created_at,
        edited_at=msg.edited_at,
        reply_to_id=reply_to_id,
        embeds=embeds_data,
    ).on_conflict_do_update(
        index_elements=[Message.id],
        set_=dict(
            content=msg.content or "",
            edited_at=msg.edited_at,
            embeds=embeds_data,
        ),
    )
    await session.execute(stmt)

    for att in msg.attachments:
        att_stmt = insert(AttachmentRow).values(
            id=att.id,
            message_id=msg.id,
            filename=att.filename,
            url=att.url,
            content_type=att.content_type,
            size=att.size,
        ).on_conflict_do_update(
            index_elements=[AttachmentRow.id],
            set_=dict(
                filename=att.filename,
                url=att.url,
                content_type=att.content_type,
                size=att.size,
            ),
        )
        await session.execute(att_stmt)

    await session.flush()


async def mark_message_deleted(session: AsyncSession, message_id: int) -> None:
    result = await session.execute(select(Message).where(Message.id == message_id))
    row = result.scalar_one_or_none()
    if row:
        row.deleted_at = datetime.now(timezone.utc)


async def mark_message_edited(session: AsyncSession, msg: discord.Message) -> None:
    await upsert_message(session, msg)
