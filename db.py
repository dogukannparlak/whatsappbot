from __future__ import annotations
import datetime as dt
from typing import Iterable, Optional

from sqlalchemy import (
    create_engine, Integer, String, Text, Boolean, DateTime, ForeignKey, event
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.exc import OperationalError

import time as _t
import config

# --- Engine & Session (MySQL friendly) ---
# MySQL: utf8mb4, pool_pre_ping (drop stale connections),
# pool_recycle (reduce "server has gone away"), reasonable pool sizes
engine = create_engine(
    config.DATABASE_URL,                 # e.g., "mysql+pymysql://user:pass@host:3306/db?charset=utf8mb4"
    future=True,
    echo=False,                          # set True for SQL echo while debugging
    pool_pre_ping=True,                  # validate connection before use
    pool_recycle=1800,                   # recycle after 30 min
    pool_size=5,                         # tune for your workload
    max_overflow=10,                     # temporary burst capacity
)

# SQLite-specific PRAGMA/connect_args are intentionally not used here.

# (Optional) Per-connection session setup for MySQL
@event.listens_for(engine, "connect")
def _set_mysql_session(dbapi_connection, connection_record):
    """
    Optional MySQL session settings:
      - Force UTC timestamps for consistency: time_zone = '+00:00'
      - Strict modes can be applied if needed (commented below)
    """
    try:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00';")
            # Example: strict modes (enable/disable per your needs)
            # cursor.execute("SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION';")
    except Exception:
        # Non-critical; proceed even if setting fails
        pass

# Session factory
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)

# Base class for all ORM models
class Base(DeclarativeBase):
    pass


# --- Models ---

class Job(Base):
    """
    A top-level send request (job).
    - target_type: single_phone | multi_phone | group
    - raw_target/message_raw: raw inputs as provided by the user (visible in logs/replay)
    - status: overall job state
    - events/targets: related event rows and targets
    """
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    target_type: Mapped[str] = mapped_column(String(20))  # single_phone | multi_phone | group
    raw_target: Mapped[str] = mapped_column(Text)
    message_raw: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|paused|done|failed|canceled
    profile: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # name of the profile executing the job

    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    canceled: Mapped[bool] = mapped_column(Boolean, default=False)

    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # top-level error (if any)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.utcnow())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime,
        default=lambda: dt.datetime.utcnow(),
        onupdate=lambda: dt.datetime.utcnow(),
    )

    # Relationships: a job can have many events and targets
    events: Mapped[list["JobEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    targets: Mapped[list["JobTarget"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobEvent(Base):
    """
    Chronological event for a job.
    - kind: event type (job_created, target_sent, job_failed, ...)
    - detail/extra: human-readable description and optional extra raw data
    """
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(40), ForeignKey("jobs.id", ondelete="CASCADE"))
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.utcnow())
    kind: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    job: Mapped[Job] = relationship(back_populates="events")


class JobTarget(Base):
    """
    Individual target within a job (phone + message).
    - ord: order among targets
    - status: target state (pending/running/sent/failed/canceled)
    """
    __tablename__ = "job_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(40), ForeignKey("jobs.id", ondelete="CASCADE"))
    phone: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    ord: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|sent|failed|canceled
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime,
        default=lambda: dt.datetime.utcnow(),
        onupdate=lambda: dt.datetime.utcnow(),
    )

    job: Mapped[Job] = relationship(back_populates="targets")


class Contact(Base):
    """
    Simple address book.
    - group_name allows grouping multiple numbers under the same logical group.
    """
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    phone: Mapped[str] = mapped_column(String(32))
    group_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.utcnow())


# --- Helpers ---

def init_db() -> None:
    """Create ORM tables if they do not exist (idempotent)."""
    Base.metadata.create_all(bind=engine)


def add_event(session, job: Job, kind: str, detail: str = "", extra: Optional[str] = None) -> None:
    """Append a JobEvent to the session (does not commit)."""
    ev = JobEvent(job_id=job.id, kind=kind, detail=detail, extra=extra)
    session.add(ev)


def create_job(
    session,
    job_id: str,
    target_type: str,
    raw_target: str,
    message_raw: str,
    phones: Iterable[str],
    messages: list[str],
) -> Job:
    """
    Create a new Job and associated JobTarget rows (does not commit).
    - If messages is empty, fill with a single empty message
    - If messages is shorter than phones, the last message is reused for the rest
    """
    job = Job(
        id=job_id,
        target_type=target_type,
        raw_target=raw_target,
        message_raw=message_raw,
        status="queued",
        paused=False,
        canceled=False,
    )
    session.add(job)

    phones = list(phones)
    if len(messages) == 0:
        messages = [""]

    for i, p in enumerate(phones):
        m = messages[i] if i < len(messages) else messages[-1]
        session.add(JobTarget(job=job, phone=p, message=m, ord=i, status="pending"))

    add_event(session, job, "job_queued", detail=f"Queued {len(phones)} target(s)")
    return job


def get_group_phones(session, group_name: str) -> list[str]:
    """Return phone numbers associated with the given group name."""
    rows = session.query(Contact.phone).filter(Contact.group_name == group_name).all()
    return [r[0] for r in rows]


def commit_with_retry(session, retries: int = 5, initial_sleep: float = 0.1) -> None:
    """
    MySQL-friendly retry logic on commit to mitigate intermittent errors like:
     - 'server has gone away'
     - 'lock wait timeout exceeded'
     - 'deadlock found'
    Uses exponential backoff.
    """
    attempt = 0
    retriable = ("server has gone away", "lock wait timeout exceeded", "deadlock found")
    while True:
        try:
            session.commit()
            return
        except OperationalError as e:
            msg = str(e).lower()
            session.rollback()
            attempt += 1
            if attempt > retries or not any(t in msg for t in retriable):
                raise
            _t.sleep(initial_sleep * (2 ** (attempt - 1)))
