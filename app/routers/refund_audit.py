"""GET /accounts/{account_id}/refund-audit -- attribute refunds to incomes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..schemas import (
    AttributedRefund,
    IncomeRefundAudit,
    RefundAnomaly,
    RefundAuditResponse,
)
from ..services import refund_audit
from .webhooks import get_db

router = APIRouter(tags=["refund-audit"])


@router.get("/accounts/{account_id}/refund-audit", response_model=RefundAuditResponse)
def account_refund_audit(
    account_id: str,
    as_of: datetime = Query(
        ...,
        description="Cut-off business time (inclusive); only events with occurred_at <= as_of are replayed",
    ),
    db: Session = Depends(get_db),
):
    # Normalize to naive UTC to match stored occurred_at values.
    if as_of.tzinfo is not None:
        as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)

    audit = refund_audit(db, account_id, as_of)
    return RefundAuditResponse(
        account_id=audit["account_id"],
        as_of=audit["as_of"],
        incomes=[
            IncomeRefundAudit(
                income_event_id=row["income_event_id"],
                currency=row["currency"],
                amount=row["amount"],
                occurred_at=row["occurred_at"],
                refunded_amount=row["refunded_amount"],
                remaining_amount=row["remaining_amount"],
                refunds=[AttributedRefund(**r) for r in row["refunds"]],
            )
            for row in audit["incomes"]
        ],
        anomalies=[RefundAnomaly(**a) for a in audit["anomalies"]],
    )
