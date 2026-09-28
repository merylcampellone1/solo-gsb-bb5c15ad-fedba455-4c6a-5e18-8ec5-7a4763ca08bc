"""GET /accounts/{account_id}/refund-audit -- attribute refunds to incomes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..schemas import RefundAuditResponse
from ..services import refund_audit
from .webhooks import get_db

router = APIRouter(tags=["refund-audit"])


@router.get("/accounts/{account_id}/refund-audit", response_model=RefundAuditResponse)
def account_refund_audit(
    account_id: str,
    as_of: datetime = Query(
        ...,
        description="Business-time cutoff (inclusive); only events with occurred_at <= as_of are visible",
    ),
    db: Session = Depends(get_db),
):
    # Normalize the query parameter to naive UTC for storage/comparison.
    if as_of.tzinfo is not None:
        cutoff = as_of.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        cutoff = as_of.replace(tzinfo=None)
    result = refund_audit(db, account_id, cutoff)
    # Echo back an aware UTC timestamp.
    result["as_of"] = cutoff.replace(tzinfo=timezone.utc)
    return result
