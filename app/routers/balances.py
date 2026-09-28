"""GET /accounts/{account_id}/balance -- current balance per account."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..schemas import AccountBalanceOut, BalanceOut
from ..services import get_balances
from .webhooks import get_db

router = APIRouter(tags=["balances"])


@router.get("/accounts/{account_id}/balance", response_model=AccountBalanceOut)
def account_balance(
    account_id: str,
    request: Request,
    recompute: bool = Query(
        default=False,
        description="Fold the full event log from scratch instead of reading the cached projection",
    ),
    db: Session = Depends(get_db),
):
    balances = get_balances(db, account_id, recompute=recompute)
    return AccountBalanceOut(
        account_id=account_id,
        balances=[
            BalanceOut(currency=cur, balance=b["balance"], event_count=b["event_count"])
            for cur, b in balances.items()
        ],
        as_of=datetime.now(timezone.utc),
        source="folded" if recompute else "projection",
    )
