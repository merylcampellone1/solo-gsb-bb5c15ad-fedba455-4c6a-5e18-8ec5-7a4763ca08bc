"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CENTS = Decimal("0.01")


def to_minor_units(amount: Decimal) -> int:
    """Convert a decimal amount to integer minor units (cents)."""
    return int((amount * 100).to_integral_exact())


def to_decimal_str(minor: int) -> str:
    return str((Decimal(minor) / 100).quantize(CENTS))


class EventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=64)
    type: Literal["income", "refund"]
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    occurred_at: datetime
    reference_event_id: str | None = Field(default=None, max_length=64)
    metadata: dict[str, Any] | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _parse_amount(cls, v: Any) -> Decimal:
        # Accept strings and numbers; reject anything with >2 decimal places
        # so minor-unit conversion is always exact.
        try:
            d = v if isinstance(v, Decimal) else Decimal(str(v))
        except (InvalidOperation, ValueError):
            raise ValueError("amount must be a decimal number")
        if d != d.quantize(CENTS):
            raise ValueError("amount must have at most 2 decimal places")
        return d

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper()

    @field_validator("occurred_at")
    @classmethod
    def _normalize_ts(cls, v: datetime) -> datetime:
        # Normalize to naive UTC for storage/comparison.
        if v.tzinfo is None:
            return v
        return v.astimezone(timezone.utc).replace(tzinfo=None)


class BalanceOut(BaseModel):
    currency: str
    balance: str
    event_count: int


class AccountBalanceOut(BaseModel):
    account_id: str
    balances: list[BalanceOut]
    as_of: datetime
    source: str  # "projection" or "folded"


class WebhookResult(BaseModel):
    status: Literal["processed", "duplicate"]
    event_id: str
    account_id: str
    balance: BalanceOut


class ExpectedEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=64)
    type: Literal["income", "refund"] | None = None
    amount: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("amount", mode="before")
    @classmethod
    def _parse_amount(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return EventIn._parse_amount(v)


class ReconcileRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    start: datetime
    end: datetime
    # Optional: expected net balance change within the window, per currency.
    expected_balance: dict[str, Decimal] | None = None
    # Optional: events the source system believes it sent within the window.
    expected_events: list[ExpectedEvent] | None = None

    @field_validator("start", "end")
    @classmethod
    def _normalize_ts(cls, v: datetime) -> datetime:
        return EventIn._normalize_ts(v)

    @field_validator("end")
    @classmethod
    def _window_order(cls, v: datetime, info) -> datetime:
        start = info.data.get("start")
        if start is not None and v < start:
            raise ValueError("end must be >= start")
        return v


class Discrepancy(BaseModel):
    kind: Literal[
        "missing_in_ledger",
        "missing_in_source",
        "amount_mismatch",
        "type_mismatch",
        "balance_mismatch",
    ]
    event_id: str | None = None
    currency: str | None = None
    detail: str
    ledger_value: str | None = None
    expected_value: str | None = None


class ReconcileResponse(BaseModel):
    account_id: str
    window: dict[str, datetime]
    ledger_event_count: int
    ledger_net: dict[str, str]
    discrepancies: list[Discrepancy]
    in_sync: bool


class AttributedRefund(BaseModel):
    """A refund successfully attributed to its original income."""

    event_id: str
    amount: str
    occurred_at: datetime


class IncomeRefundAudit(BaseModel):
    """One income plus the refunds attributed to it within the as-of cut."""

    income_event_id: str
    currency: str
    amount: str
    occurred_at: datetime
    refunded_amount: str
    remaining_amount: str
    refunds: list[AttributedRefund]


class RefundAnomaly(BaseModel):
    """A refund that could not be attributed to a valid original income."""

    event_id: str
    currency: str
    amount: str
    occurred_at: datetime
    reference_event_id: str | None
    kind: Literal[
        "reference_missing",
        "reference_not_found",
        "reference_account_mismatch",
        "reference_currency_mismatch",
        "reference_not_income",
        "refund_exceeds_income",
    ]
    detail: str


class RefundAuditResponse(BaseModel):
    account_id: str
    as_of: datetime
    incomes: list[IncomeRefundAudit]
    anomalies: list[RefundAnomaly]
