import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class BackfillStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    complete = "complete"
    failed = "failed"


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(200))
    icon_url: Mapped[str | None] = mapped_column(Text)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime)

    channels: Mapped[list["Channel"]] = relationship(back_populates="server")


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (Index("ix_channels_server_id", "server_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    server_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("servers.id"))
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(50))
    topic: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int | None] = mapped_column(Integer)

    server: Mapped["Server"] = relationship(back_populates="channels")
    threads: Mapped[list["Thread"]] = relationship(back_populates="channel")
    messages: Mapped[list["Message"]] = relationship(back_populates="channel")


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("channels.id"))
    name: Mapped[str] = mapped_column(String(200))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    thread_created_at: Mapped[datetime | None] = mapped_column(DateTime)

    channel: Mapped["Channel"] = relationship(back_populates="threads")
    messages: Mapped[list["Message"]] = relationship(back_populates="thread")


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)

    messages: Mapped[list["Message"]] = relationship(back_populates="author")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_channel_id_created_at", "channel_id", "created_at"),
        Index("ix_messages_author_id", "author_id"),
        Index("ix_messages_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("channels.id"))
    thread_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("threads.id"))
    author_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("authors.id"))
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    reply_to_id: Mapped[int | None] = mapped_column(BigInteger)
    embeds: Mapped[dict | None] = mapped_column(JSON)

    channel: Mapped["Channel"] = relationship(back_populates="messages")
    thread: Mapped["Thread | None"] = relationship(back_populates="messages")
    author: Mapped["Author"] = relationship(back_populates="messages")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="message")
    reactions: Mapped[list["Reaction"]] = relationship(back_populates="message")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("messages.id"))
    filename: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(200))
    size: Mapped[int | None] = mapped_column(Integer)
    local_path: Mapped[str | None] = mapped_column(Text)

    message: Mapped["Message"] = relationship(back_populates="attachments")


class Reaction(Base):
    __tablename__ = "reactions"
    __table_args__ = (
        UniqueConstraint("message_id", "emoji", "user_id", name="uq_reactions_message_emoji_user"),
    )

    message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("messages.id"), primary_key=True)
    emoji: Mapped[str] = mapped_column(String(200), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)

    message: Mapped["Message"] = relationship(back_populates="reactions")


class DigestPeriod(str, enum.Enum):
    hourly = "hourly"
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


class WikiPageType(str, enum.Enum):
    user_profile = "user_profile"
    topic = "topic"
    channel_overview = "channel_overview"
    server_overview = "server_overview"


class Digest(Base):
    __tablename__ = "digests"
    __table_args__ = (
        Index("ix_digests_channel_id_period_start", "channel_id", "period", "period_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    period: Mapped[str] = mapped_column(String(20))
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    content: Mapped[str] = mapped_column(Text)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (
        Index("ix_wiki_pages_page_type", "page_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(300), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    page_type: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    server_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    author_id: Mapped[int | None] = mapped_column(BigInteger)
    source_refs: Mapped[dict | None] = mapped_column(JSON)
    last_updated: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class ChannelBackfillState(Base):
    __tablename__ = "channel_backfill_state"

    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    backfilled_through: Mapped[datetime | None] = mapped_column(DateTime)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="pending")
