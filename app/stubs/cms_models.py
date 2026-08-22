"""Stub CMS (Filevine-like) data model.

This is the system of record: firms, cases, tasks, contacts, and chat.
It is deliberately generic - field names mirror Filevine concepts but nothing
here is Filevine-specific, since the CMS changes between firms.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CmsBase(DeclarativeBase):
    pass


class Firm(CmsBase):
    __tablename__ = "cms_firms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    cases: Mapped[list[Case]] = relationship(back_populates="firm")


class Case(CmsBase):
    """The main thing. A task is a sub data point of a case."""

    __tablename__ = "cms_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("cms_firms.id"), index=True)
    case_number: Mapped[str] = mapped_column(String(50))
    client_name: Mapped[str] = mapped_column(String(200))
    case_type: Mapped[str] = mapped_column(String(100), default="personal_injury")
    status: Mapped[str] = mapped_column(String(50), default="open")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    firm: Mapped[Firm] = relationship(back_populates="cases")
    tasks: Mapped[list[Task]] = relationship(back_populates="case")
    contacts: Mapped[list[Contact]] = relationship(back_populates="case")


class Task(CmsBase):
    __tablename__ = "cms_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cms_cases.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(50), default="open")   # open / in_progress / done
    notes: Mapped[str] = mapped_column(Text, default="")              # agents write outcomes back here
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    case: Mapped[Case] = relationship(back_populates="tasks")
    threads: Mapped[list[ChatThread]] = relationship(back_populates="task")


class Contact(CmsBase):
    """Provider, hospital, client, adjuster... anyone attached to a case."""

    __tablename__ = "cms_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cms_cases.id"), index=True)
    role: Mapped[str] = mapped_column(String(50))        # provider / hospital / client / adjuster
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(50), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    fax: Mapped[str] = mapped_column(String(50), default="")
    details: Mapped[str] = mapped_column(Text, default="")  # free-form; schema varies per firm/CMS

    case: Mapped[Case] = relationship(back_populates="contacts")


class ChatThread(CmsBase):
    __tablename__ = "cms_chat_threads"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("cms_tasks.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[Task] = relationship(back_populates="threads")
    messages: Mapped[list[ChatMessage]] = relationship(back_populates="thread", order_by="ChatMessage.id")


class ChatMessage(CmsBase):
    __tablename__ = "cms_chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("cms_chat_threads.id"), index=True)
    author: Mapped[str] = mapped_column(String(100))     # staff name / "agent" / "system"
    body: Mapped[str] = mapped_column(Text)
    mentions_agent: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    thread: Mapped[ChatThread] = relationship(back_populates="messages")
