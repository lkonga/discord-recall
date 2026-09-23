"""Query engine — ask questions about channels, servers, and users."""


from loguru import logger
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from discord_recall.db import get_session_factory
from discord_recall.db.models import (
    Author,
    Channel,
    Digest,
    Message,
    Server,
    WikiPage,
)

QUERY_SYSTEM = """You are a Discord intelligence analyst with deep knowledge of the conversations in these servers. Answer the user's question based on the context provided.

Rules:
- Be specific — cite usernames, dates, and quote notable messages when relevant
- If the context doesn't contain enough information to fully answer, say so and suggest what might help
- Distinguish between what you know from the data and what you're inferring
- If asked about a user, focus on their actual statements and positions, not assumptions
- Be direct and concise"""


async def _search_messages(
    session: AsyncSession,
    query_terms: list[str],
    channel_id: int | None = None,
    server_id: int | None = None,
    author_id: int | None = None,
    limit: int = 200,
) -> list:
    """Search messages by content keywords."""
    conditions = [Message.deleted_at.is_(None)]

    if channel_id:
        conditions.append(Message.channel_id == channel_id)
    if server_id:
        conditions.append(
            Message.channel_id.in_(
                select(Channel.id).where(Channel.server_id == server_id)
            )
        )
    if author_id:
        conditions.append(Message.author_id == author_id)

    # Text search — match any term
    if query_terms:
        term_conditions = [Message.content.ilike(f"%{term}%") for term in query_terms]
        conditions.append(or_(*term_conditions))

    stmt = (
        select(
            Author.username,
            Author.display_name,
            Message.content,
            Message.created_at,
            Channel.name.label("channel_name"),
        )
        .join(Author, Message.author_id == Author.id)
        .join(Channel, Message.channel_id == Channel.id)
        .where(and_(*conditions))
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.all()


async def _get_wiki_context(
    session: AsyncSession,
    server_id: int | None = None,
    channel_id: int | None = None,
    author_id: int | None = None,
) -> list[WikiPage]:
    """Fetch relevant wiki pages."""
    conditions = []
    if server_id:
        conditions.append(WikiPage.server_id == server_id)
    if channel_id:
        conditions.append(WikiPage.channel_id == channel_id)
    if author_id:
        conditions.append(WikiPage.author_id == author_id)

    if not conditions:
        return []

    stmt = select(WikiPage).where(or_(*conditions)).order_by(WikiPage.last_updated.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


async def _get_digest_context(
    session: AsyncSession,
    channel_id: int | None = None,
    server_id: int | None = None,
) -> list[Digest]:
    """Fetch digests for context — prefers weekly/monthly for breadth, recent daily for depth."""
    conditions = []
    if channel_id:
        conditions.append(Digest.channel_id == channel_id)
    if server_id:
        conditions.append(
            Digest.channel_id.in_(
                select(Channel.id).where(Channel.server_id == server_id)
            )
        )

    if not conditions:
        return []

    # Get the most recent digests, preferring coarser granularity first
    # so we cover more history within the token budget
    stmt = (
        select(Digest)
        .where(and_(*conditions))
        .order_by(Digest.period_start.desc())
        .limit(30)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def _resolve_author(session: AsyncSession, username: str) -> Author | None:
    """Find an author by username (case-insensitive partial match)."""
    stmt = (
        select(Author)
        .where(
            or_(
                Author.username.ilike(f"%{username}%"),
                Author.display_name.ilike(f"%{username}%"),
            )
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _build_context(
    wiki_pages: list[WikiPage],
    digests: list[Digest],
    messages: list,
) -> str:
    """Build the context string for Claude."""
    parts = []

    if wiki_pages:
        parts.append("## Knowledge Base\n")
        for page in wiki_pages:
            parts.append(f"### {page.title}\n{page.content}\n")

    if digests:
        parts.append("## Recent Digests\n")
        for d in digests:
            meta = d.metadata_ or {}
            ch = meta.get("channel", "?")
            parts.append(
                f"### #{ch} — {d.period_start.strftime('%Y-%m-%d')} ({d.message_count} msgs)\n"
                f"{d.content}\n"
            )

    if messages:
        parts.append("## Relevant Messages\n")
        for username, display_name, content, created_at, channel_name in messages:
            name = display_name or username
            ts = created_at.strftime("%Y-%m-%d %H:%M")
            if content.strip():
                parts.append(f"[{ts} #{channel_name}] {name}: {content}")

    return "\n".join(parts)


async def ask(
    question: str,
    channel_id: int | None = None,
    server_id: int | None = None,
    username: str | None = None,
) -> str:
    """Ask a question about Discord conversations."""
    session_factory = get_session_factory()

    author_id = None
    query_terms = question.lower().split()
    # Filter out very common short words for search
    query_terms = [t for t in query_terms if len(t) > 3]

    async with session_factory() as session:
        if username:
            author = await _resolve_author(session, username)
            if author:
                author_id = author.id
                logger.info(f"Resolved user '{username}' to {author.username} ({author.id})")
            else:
                logger.warning(f"Could not find user matching '{username}'")

        wiki_pages = await _get_wiki_context(session, server_id, channel_id, author_id)
        digests = await _get_digest_context(session, channel_id, server_id)
        messages = await _search_messages(
            session, query_terms, channel_id, server_id, author_id
        )

    context = _build_context(wiki_pages, digests, messages)

    if not context.strip():
        return "No relevant data found. Try broadening your search or building digests first."

    scope_parts = []
    if server_id:
        async with session_factory() as session:
            srv = await session.get(Server, server_id)
            if srv:
                scope_parts.append(f"Server: {srv.name}")
    if channel_id:
        async with session_factory() as session:
            ch = await session.get(Channel, channel_id)
            if ch:
                scope_parts.append(f"Channel: #{ch.name}")
    if username:
        scope_parts.append(f"User filter: {username}")

    scope = "\n".join(scope_parts) if scope_parts else "All servers"

    prompt = f"Scope: {scope}\n\nQuestion: {question}\n\n--- Context ---\n{context}"

    logger.info(
        f"Querying LLM with {len(wiki_pages)} wiki pages, "
        f"{len(digests)} digests, {len(messages)} messages"
    )

    from discord_recall.llm import complete

    return await complete(QUERY_SYSTEM, prompt)
