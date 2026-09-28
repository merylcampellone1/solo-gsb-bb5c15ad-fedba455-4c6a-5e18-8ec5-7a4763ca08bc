"""POST /webhooks/events -- receive signed income/refund events."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..schemas import BalanceOut, EventIn, WebhookResult
from ..security import verify_signature
from ..services import apply_event, get_balances

router = APIRouter(tags=["webhooks"])


def get_db(request: Request):
    factory = request.app.state.session_factory
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


@router.post("/webhooks/events", response_model=WebhookResult, status_code=201)
async def receive_event(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    raw_body = await request.body()

    verify_signature(
        secret=settings.webhook_secret,
        timestamp_header=request.headers.get("X-Webhook-Timestamp"),
        signature_header=request.headers.get("X-Webhook-Signature"),
        body=raw_body,
        tolerance_seconds=settings.signature_tolerance_seconds,
    )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="request body is not valid JSON")
    try:
        event = EventIn.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json()))

    result = apply_event(db, event, raw_body)
    balances = get_balances(db, event.account_id)
    current = balances[event.currency]

    return WebhookResult(
        status="processed" if result.created else "duplicate",
        event_id=event.event_id,
        account_id=event.account_id,
        balance=BalanceOut(
            currency=event.currency,
            balance=current["balance"],
            event_count=current["event_count"],
        ),
    )
