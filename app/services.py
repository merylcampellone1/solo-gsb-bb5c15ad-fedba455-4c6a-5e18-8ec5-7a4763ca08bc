"""Core ledger logic: idempotent event application, balance folding, reconciliation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AccountBalance, Event, utcnow
from .schemas import EventIn, ReconcileRequest, to_decimal_str, to_minor_units

SIGNED = {"income": 1, "refund": -1}


def canonical_payload(event: EventIn) -> str:
    """Canonical JSON of the business payload, for tamper-evident dedup."""
    return json.dumps(
        {
            "event_id": event.event_id,
            "account_id": event.account_id,
            "type": event.type,
            "amount": str(event.amount),
            "currency": event.currency,
            "occurred_at": event.occurred_at.isoformat(),
            "reference_event_id": event.reference_event_id,
        },
        sort_keys=True,
    )


@dataclass
class ApplyResult:
    event: Event
    created: bool  # False => idempotent replay of an already-applied event


def apply_event(session: Session, event: EventIn, raw_body: bytes) -> ApplyResult:
    """Apply one webhook event idempotently.

    * Same event_id + identical payload  -> no-op, returns the stored event.
    * Same event_id + different payload  -> 409 Conflict (producer bug).
    * New event_id                        -> insert + fold into the balance
      projection. Folding is order-independent, so events may arrive out of
      order (e.g. a refund before its income) and still converge.
    """
    payload_hash = hashlib.sha256(canonical_payload(event).encode()).hexdigest()

    existing = session.scalar(select(Event).where(Event.event_id == event.event_id))
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise HTTPException(
                status_code=409,
                detail=f"event_id {event.event_id!r} already exists with a different payload",
            )
        return ApplyResult(event=existing, created=False)

    row = Event(
        event_id=event.event_id,
        account_id=event.account_id,
        type=event.type,
        amount_minor=to_minor_units(event.amount),
        currency=event.currency,
        occurred_at=event.occurred_at,
        received_at=utcnow(),
        reference_event_id=event.reference_event_id,
        payload_hash=payload_hash,
        raw_payload=raw_body.decode("utf-8", errors="replace"),
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        # Lost a race with a concurrent delivery of the same event_id.
        session.rollback()
        existing = session.scalar(select(Event).where(Event.event_id == event.event_id))
        if existing is not None and existing.payload_hash == payload_hash:
            return ApplyResult(event=existing, created=False)
        raise HTTPException(
            status_code=409,
            detail=f"event_id {event.event_id!r} already exists with a different payload",
        )

    _fold_into_projection(session, row)
    session.commit()
    return ApplyResult(event=row, created=True)


def _fold_into_projection(session: Session, event: Event) -> None:
    """Incrementally fold one event into the cached balance projection."""
    balance = session.scalar(
        select(AccountBalance).where(
            AccountBalance.account_id == event.account_id,
            AccountBalance.currency == event.currency,
        )
    )
    if balance is None:
        balance = AccountBalance(
            account_id=event.account_id, currency=event.currency, balance_minor=0, event_count=0
        )
        session.add(balance)
    balance.balance_minor += SIGNED[event.type] * event.amount_minor
    balance.event_count += 1
    balance.updated_at = utcnow()
    session.flush()


def fold_events(events: list[Event]) -> dict[str, int]:
    """Pure fold of events -> {currency: balance_minor}. Order-independent."""
    totals: dict[str, int] = {}
    for e in events:
        totals[e.currency] = totals.get(e.currency, 0) + SIGNED[e.type] * e.amount_minor
    return totals


def get_balances(session: Session, account_id: str, recompute: bool = False) -> dict[str, dict]:
    """Current balances for an account.

    Default: read the incremental projection. With recompute=True, fold the
    full event log from scratch (proves the projection converges regardless
    of arrival order) and self-heal the cache if it ever drifted.
    """
    if recompute:
        events = list(session.scalars(select(Event).where(Event.account_id == account_id)))
        folded = fold_events(events)
        counts: dict[str, int] = {}
        for e in events:
            counts[e.currency] = counts.get(e.currency, 0) + 1
        # Self-heal the projection if it drifted.
        for currency, minor in folded.items():
            row = session.scalar(
                select(AccountBalance).where(
                    AccountBalance.account_id == account_id,
                    AccountBalance.currency == currency,
                )
            )
            if row is not None and row.balance_minor != minor:
                row.balance_minor = minor
                row.event_count = counts[currency]
        session.commit()
        return {
            cur: {"balance": to_decimal_str(minor), "event_count": counts[cur]}
            for cur, minor in sorted(folded.items())
        }

    rows = session.scalars(
        select(AccountBalance).where(AccountBalance.account_id == account_id)
    ).all()
    return {
        r.currency: {"balance": to_decimal_str(r.balance_minor), "event_count": r.event_count}
        for r in sorted(rows, key=lambda r: r.currency)
    }


def events_in_window(session: Session, account_id: str, start: datetime, end: datetime) -> list[Event]:
    """Replay the ledger for an account within [start, end] (by occurred_at)."""
    return list(
        session.scalars(
            select(Event)
            .where(
                Event.account_id == account_id,
                Event.occurred_at >= start,
                Event.occurred_at <= end,
            )
            .order_by(Event.occurred_at, Event.id)
        )
    )


def reconcile(session: Session, req: ReconcileRequest) -> dict:
    """Replay events in the window and diff against the source system's view."""
    events = events_in_window(session, req.account_id, req.start, req.end)
    net = fold_events(events)
    discrepancies: list[dict] = []

    if req.expected_events is not None:
        ledger_by_id = {e.event_id: e for e in events}
        expected_by_id = {e.event_id: e for e in req.expected_events}

        for event_id in sorted(expected_by_id.keys() - ledger_by_id.keys()):
            discrepancies.append(
                {
                    "kind": "missing_in_ledger",
                    "event_id": event_id,
                    "detail": "source system sent this event but it is not in the ledger window",
                }
            )
        for event_id in sorted(ledger_by_id.keys() - expected_by_id.keys()):
            discrepancies.append(
                {
                    "kind": "missing_in_source",
                    "event_id": event_id,
                    "detail": "ledger contains an event the source system did not report",
                }
            )
        for event_id in sorted(ledger_by_id.keys() & expected_by_id.keys()):
            led = ledger_by_id[event_id]
            exp = expected_by_id[event_id]
            if exp.amount is not None and led.amount_minor != to_minor_units(exp.amount):
                discrepancies.append(
                    {
                        "kind": "amount_mismatch",
                        "event_id": event_id,
                        "detail": "ledger amount differs from source amount",
                        "ledger_value": to_decimal_str(led.amount_minor),
                        "expected_value": str(exp.amount),
                    }
                )
            if exp.type is not None and led.type != exp.type:
                discrepancies.append(
                    {
                        "kind": "type_mismatch",
                        "event_id": event_id,
                        "detail": "ledger event type differs from source type",
                        "ledger_value": led.type,
                        "expected_value": exp.type,
                    }
                )

    if req.expected_balance is not None:
        for currency, expected in sorted(req.expected_balance.items()):
            expected_minor = to_minor_units(expected)
            actual_minor = net.get(currency, 0)
            if actual_minor != expected_minor:
                discrepancies.append(
                    {
                        "kind": "balance_mismatch",
                        "currency": currency,
                        "detail": "net balance change in window differs from expected",
                        "ledger_value": to_decimal_str(actual_minor),
                        "expected_value": to_decimal_str(expected_minor),
                    }
                )

    return {
        "account_id": req.account_id,
        "window": {"start": req.start, "end": req.end},
        "ledger_event_count": len(events),
        "ledger_net": {cur: to_decimal_str(m) for cur, m in sorted(net.items())},
        "discrepancies": discrepancies,
        "in_sync": not discrepancies,
    }


def refund_audit(session: Session, account_id: str, as_of: datetime) -> dict:
    """Attribute refunds to their original incomes within the ledger as of
    ``as_of`` (inclusive, by business ``occurred_at``).

    Refunds are grouped onto the income named by ``reference_event_id`` and,
    for each income, applied in (occurred_at, event_id) order. Because this is
    a pure replay over the as-of slice, a refund that arrived before its
    income was backfilled is attributed automatically once the income is
    present.

    Refunds that cannot be attributed are reported as anomalies:

    * reference_missing           -- no reference_event_id at all;
    * reference_not_found         -- referenced event is absent from the
                                     as-of ledger (unknown id or not yet
                                     recorded by the cut-off);
    * reference_account_mismatch  -- referenced event belongs to another
                                     account;
    * reference_currency_mismatch-- different currency;
    * reference_not_income        -- referenced event is itself a refund;
    * refund_exceeds_income       -- applying the refund would push the
                                     cumulative refunded total above the
                                     original income. Such a refund does not
                                     reduce the income's remaining amount at
                                     all (the whole amount is rejected).

    Incomes are sorted by income event id and anomalies by their refund event
    id, so output is stable across replay.
    """
    events = list(
        session.scalars(
            select(Event)
            .where(
                Event.account_id == account_id,
                Event.occurred_at <= as_of,
            )
            .order_by(Event.occurred_at, Event.event_id)
        )
    )

    # Global index (across accounts) of events within the cut, so a reference
    # to an event on another account can be diagnosed precisely.
    all_events = list(session.scalars(select(Event).where(Event.occurred_at <= as_of)))
    events_by_id = {e.event_id: e for e in all_events}

    incomes = [e for e in events if e.type == "income"]
    refunds_by_ref: dict[str, list[Event]] = {}
    anomalies: list[dict] = []

    def add_anomaly(e: Event, kind: str, detail: str) -> None:
        anomalies.append(
            {
                "event_id": e.event_id,
                "currency": e.currency,
                "amount": to_decimal_str(e.amount_minor),
                "occurred_at": e.occurred_at,
                "reference_event_id": e.reference_event_id,
                "kind": kind,
                "detail": detail,
            }
        )

    for e in events:
        if e.type != "refund":
            continue
        ref = e.reference_event_id
        if ref is None:
            add_anomaly(e, "reference_missing", "refund has no reference_event_id")
        elif ref not in events_by_id:
            add_anomaly(
                e,
                "reference_not_found",
                f"reference_event_id {ref!r} does not exist in the ledger as of the cut-off",
            )
        else:
            target = events_by_id[ref]
            if target.account_id != account_id:
                add_anomaly(
                    e,
                    "reference_account_mismatch",
                    f"referenced event {ref!r} belongs to account {target.account_id!r}, "
                    f"not {account_id!r}",
                )
            elif target.currency != e.currency:
                add_anomaly(
                    e,
                    "reference_currency_mismatch",
                    f"refund currency {e.currency!r} differs from referenced income "
                    f"currency {target.currency!r}",
                )
            elif target.type != "income":
                add_anomaly(
                    e,
                    "reference_not_income",
                    f"referenced event {ref!r} is a {target.type}, not an income",
                )
            else:
                refunds_by_ref.setdefault(ref, []).append(e)

    income_rows: list[dict] = []
    for income in incomes:
        refunded = 0
        attributed: list[dict] = []
        for r in refunds_by_ref.get(income.event_id, []):
            if refunded + r.amount_minor > income.amount_minor:
                # Whole refund is rejected: it does not touch the remaining
                # amount, and later smaller refunds can still be applied.
                add_anomaly(
                    r,
                    "refund_exceeds_income",
                    f"refund of {to_decimal_str(r.amount_minor)} would make cumulative refunds "
                    f"({to_decimal_str(refunded + r.amount_minor)}) exceed income "
                    f"{to_decimal_str(income.amount_minor)}",
                )
                continue
            refunded += r.amount_minor
            attributed.append(
                {
                    "event_id": r.event_id,
                    "amount": to_decimal_str(r.amount_minor),
                    "occurred_at": r.occurred_at,
                }
            )

        income_rows.append(
            {
                "income_event_id": income.event_id,
                "currency": income.currency,
                "amount": to_decimal_str(income.amount_minor),
                "occurred_at": income.occurred_at,
                "refunded_amount": to_decimal_str(refunded),
                "remaining_amount": to_decimal_str(income.amount_minor - refunded),
                "refunds": attributed,
            }
        )

    income_rows.sort(key=lambda row: row["income_event_id"])
    anomalies.sort(key=lambda row: row["event_id"])

    return {
        "account_id": account_id,
        "as_of": as_of,
        "incomes": income_rows,
        "anomalies": anomalies,
    }
