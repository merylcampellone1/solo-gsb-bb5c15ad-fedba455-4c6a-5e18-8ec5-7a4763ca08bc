"""SQLAlchemy models.

Two tables:

* ``events``           -- the immutable, append-only ledger of webhook events.
* ``account_balances`` -- a materialized projection (per account + currency)
                          maintained incrementally as events are applied.
                          Because income/refund folding is commutative, events
                          arriving out of order still converge to the same
                          balance. The projection can always be rebuilt by
                          folding the event log from scratch.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    # Stored as naive UTC so SQLite comparisons stay consistent.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Producer-supplied idempotency key.
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(16))  # "income" | "refund"
    # Money is stored as integer minor units (cents) -- never floats.
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    # Business timestamp supplied by the producer; may arrive out of order.
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Optional link to the income event a refund refers to.
    reference_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # SHA-256 of the canonical payload, used to detect conflicting retries.
    payload_hash: Mapped[str] = mapped_column(String(64))
    # Raw body kept for audit / reconciliation replay.
    raw_payload: Mapped[str] = mapped_column(Text)


class AccountBalance(Base):
    __tablename__ = "account_balances"
    __table_args__ = (UniqueConstraint("account_id", "currency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    balance_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
