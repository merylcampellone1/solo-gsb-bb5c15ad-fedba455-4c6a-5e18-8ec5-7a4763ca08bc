"""Core ledger logic: idempotent event application, balance folding, reconciliation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

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


# ---------------------------------------------------------------- refund audit


def refund_audit(session: Session, account_id: str, as_of: datetime) -> dict[str, Any]:
    """Attribute refunds to the income they reference within the as_of slice.

    The ledger is sliced by business time: only events with ``occurred_at <=
    as_of`` are visible. Refunds are grouped to their referenced income via
    ``reference_event_id`` and accumulated in (occurred_at, event_id) order.
    Because the view is recomputed from the whole slice each time, a refund
    that arrives before its income is backfilled automatically becomes
    attributed once the income exists in a later slice.

    Refunds that cannot be attributed are returned as anomalies:

    * ``missing_reference``        -- no reference_event_id
    * ``reference_not_found``      -- referenced event absent from the slice
    * ``cross_account_or_currency``-- referenced event belongs to another
                                      account or uses another currency
    * ``reference_not_income``     -- referenced event is itself a refund
    * ``over_refund``              -- the refund would make the cumulative
                                      attributed amount exceed the income;
                                      the whole refund is rejected and does
                                      not offset the income's remainder
    """
    # Load the whole as_of slice, across every account, so cross-account
    # references can be distinguished from genuinely missing ones.
    sliced = list(
        session.scalars(
            select(Event)
            .where(Event.occurred_at <= as_of)
            .order_by(Event.occurred_at, Event.event_id)
        )
    )
    by_id: dict[str, Event] = {e.event_id: e for e in sliced}

    incomes = sorted(
        (e for e in sliced if e.account_id == account_id and e.type == "income"),
        key=lambda e: e.event_id,
    )
    # Refunds for this account accumulate in business-time order; event_id
    # breaks ties deterministically.
    refunds = sorted(
        (e for e in sliced if e.account_id == account_id and e.type == "refund"),
        key=lambda e: (e.occurred_at, e.event_id),
    )

    def anomaly(
        refund: Event,
        kind: str,
        detail: str,
        *,
        income: Event | None = None,
        cumulative_minor: int | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "kind": kind,
            "event_id": refund.event_id,
            "reference_event_id": refund.reference_event_id,
            "currency": refund.currency,
            "amount": to_decimal_str(refund.amount_minor),
            "detail": detail,
            "income_event_id": None,
            "income_amount": None,
            "cumulative_amount": None,
        }
        if income is not None:
            entry["income_event_id"] = income.event_id
            entry["income_amount"] = to_decimal_str(income.amount_minor)
        if cumulative_minor is not None:
            entry["cumulative_amount"] = to_decimal_str(cumulative_minor)
        return entry

    # Running attributed total per income event_id, in accumulation order.
    refunded: dict[str, int] = {e.event_id: 0 for e in incomes}
    attributed: dict[str, list[str]] = {e.event_id: [] for e in incomes}
    anomalies: list[dict[str, Any]] = []

    for refund in refunds:
        ref = refund.reference_event_id
        if ref is None:
            anomalies.append(
                anomaly(refund, "missing_reference", "refund has no reference_event_id")
            )
            continue

        target = by_id.get(ref)
        if target is None:
            anomalies.append(
                anomaly(
                    refund,
                    "reference_not_found",
                    f"reference_event_id {ref!r} does not exist in the as_of slice",
                )
            )
            continue
        if target.account_id != account_id:
            anomalies.append(
                anomaly(
                    refund,
                    "cross_account_or_currency",
                    f"referenced event {ref!r} belongs to another account "
                    f"({target.account_id!r})",
                )
            )
            continue
        if target.currency != refund.currency:
            anomalies.append(
                anomaly(
                    refund,
                    "cross_account_or_currency",
                    f"refund currency {refund.currency!r} does not match referenced "
                    f"income currency {target.currency!r}",
                )
            )
            continue
        if target.type != "income":
            anomalies.append(
                anomaly(
                    refund,
                    "reference_not_income",
                    f"referenced event {ref!r} is a {target.type}, not an income",
                    income=target,
                )
            )
            continue

        cumulative = refunded[ref] + refund.amount_minor
        if cumulative > target.amount_minor:
            # The whole refund is rejected: it is not attributed and does not
            # offset the income's remaining amount at all.
            anomalies.append(
                anomaly(
                    refund,
                    "over_refund",
                    (
                        f"refund of {to_decimal_str(refund.amount_minor)} would raise "
                        f"cumulative refunds on {ref!r} to "
                        f"{to_decimal_str(cumulative)}, exceeding the income of "
                        f"{to_decimal_str(target.amount_minor)}; the whole refund is "
                        f"not attributed"
                    ),
                    income=target,
                    cumulative_minor=cumulative,
                )
            )
            continue

        refunded[ref] = cumulative
        attributed[ref].append(refund.event_id)

    income_rows = [
        {
            "income_event_id": e.event_id,
            "currency": e.currency,
            "amount": to_decimal_str(e.amount_minor),
            "refunded_amount": to_decimal_str(refunded[e.event_id]),
            "remaining_amount": to_decimal_str(e.amount_minor - refunded[e.event_id]),
            "refund_event_ids": attributed[e.event_id],
        }
        for e in incomes
    ]
    anomalies.sort(key=lambda a: a["event_id"])

    return {
        "account_id": account_id,
        "as_of": as_of,
        "incomes": income_rows,
        "anomalies": anomalies,
    }


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
