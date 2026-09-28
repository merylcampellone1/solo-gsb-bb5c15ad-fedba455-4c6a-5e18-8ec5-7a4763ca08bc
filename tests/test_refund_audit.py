"""Tests for GET /accounts/{account_id}/refund-audit."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SECRET = "test-secret"


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        webhook_secret=SECRET,
        signature_tolerance_seconds=300,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def sign(body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = ts if ts is not None else int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Webhook-Timestamp": str(ts), "X-Webhook-Signature": f"sha256={sig}"}


def post_event(client: TestClient, event: dict):
    body = json.dumps(event).encode()
    return client.post("/webhooks/events", content=body, headers=sign(body))


def make_event(event_id: str, account_id: str = "acct-1", type_: str = "income",
               amount: str = "100.00", currency: str = "USD",
               occurred_at: str = "2026-09-10T10:00:00Z", **extra) -> dict:
    return {
        "event_id": event_id,
        "account_id": account_id,
        "type": type_,
        "amount": amount,
        "currency": currency,
        "occurred_at": occurred_at,
        **extra,
    }


def audit(client: TestClient, account: str = "acct-1", as_of: str = "2026-09-30T00:00:00Z"):
    resp = client.get(f"/accounts/{account}/refund-audit", params={"as_of": as_of})
    assert resp.status_code == 200, resp.text
    return resp.json()


def income_by_id(data, income_id):
    return next(i for i in data["incomes"] if i["income_event_id"] == income_id)


def anomaly_kinds(data):
    return {(a["kind"], a["event_id"]) for a in data["anomalies"]}


# ------------------------------------------------------------- basic attribution


def test_refunds_attributed_to_income_in_order(client):
    post_event(client, make_event("inc-1", amount="100.00", occurred_at="2026-09-10T10:00:00Z"))
    post_event(client, make_event(
        "r1", type_="refund", amount="30.00",
        occurred_at="2026-09-11T10:00:00Z", reference_event_id="inc-1",
    ))
    post_event(client, make_event(
        "r2", type_="refund", amount="20.50",
        occurred_at="2026-09-12T10:00:00Z", reference_event_id="inc-1",
    ))

    data = audit(client)
    inc = income_by_id(data, "inc-1")
    assert inc["refunded_amount"] == "50.50"
    assert inc["remaining_amount"] == "49.50"
    assert inc["refund_event_ids"] == ["r1", "r2"]
    assert data["anomalies"] == []


def test_results_sorted_by_income_id(client):
    post_event(client, make_event("inc-z", amount="10.00", occurred_at="2026-09-10T12:00:00Z"))
    post_event(client, make_event("inc-a", amount="10.00", occurred_at="2026-09-10T11:00:00Z"))

    data = audit(client)
    assert [i["income_event_id"] for i in data["incomes"]] == ["inc-a", "inc-z"]


# ------------------------------------------------------------- as_of slicing


def test_as_of_is_inclusive(client):
    post_event(client, make_event("inc-1", occurred_at="2026-09-10T10:00:00Z"))
    post_event(client, make_event(
        "r1", type_="refund", amount="30.00",
        occurred_at="2026-09-10T12:00:00Z", reference_event_id="inc-1",
    ))

    # Refund exactly at the cutoff is included.
    at_cutoff = audit(client, as_of="2026-09-10T12:00:00Z")
    assert income_by_id(at_cutoff, "inc-1")["refunded_amount"] == "30.00"

    # One second before: neither the refund ... but the income is visible.
    before = audit(client, as_of="2026-09-10T11:59:59Z")
    assert income_by_id(before, "inc-1")["refunded_amount"] == "0.00"
    assert income_by_id(before, "inc-1")["remaining_amount"] == "100.00"


def test_future_income_excluded_from_slice(client):
    post_event(client, make_event("inc-1", occurred_at="2026-09-20T10:00:00Z"))

    data = audit(client, as_of="2026-09-10T00:00:00Z")
    assert data["incomes"] == []


# ----------------------------------- refund before income becomes attributed later


def test_refund_before_income_pending_then_attributed(client):
    # Business-time order: income at 10:00, refund at 09:00 (earlier).
    # This models the refund arriving first and the income being backfilled.
    post_event(client, make_event(
        "r-early", type_="refund", amount="30.00",
        occurred_at="2026-09-10T09:00:00Z", reference_event_id="inc-1",
    ))

    before = audit(client, as_of="2026-09-10T09:30:00Z")
    assert before["incomes"] == []
    assert anomaly_kinds(before) == {("reference_not_found", "r-early")}

    # Backfill the income; the early refund auto-converts to attributed.
    post_event(client, make_event("inc-1", occurred_at="2026-09-10T10:00:00Z"))

    after = audit(client, as_of="2026-09-10T10:30:00Z")
    inc = income_by_id(after, "inc-1")
    assert inc["refund_event_ids"] == ["r-early"]
    assert inc["refunded_amount"] == "30.00"
    assert inc["remaining_amount"] == "70.00"
    assert after["anomalies"] == []


# ----------------------------------------------------------------- anomalies


def test_missing_reference_anomaly(client):
    post_event(client, make_event("inc-1"))
    post_event(client, make_event("r-noref", type_="refund", amount="10.00"))

    data = audit(client)
    kinds = anomaly_kinds(data)
    assert ("missing_reference", "r-noref") in kinds
    inc = income_by_id(data, "inc-1")
    assert inc["refunded_amount"] == "0.00"
    assert inc["remaining_amount"] == "100.00"


def test_reference_not_found_anomaly(client):
    post_event(client, make_event(
        "r-ghost", type_="refund", amount="10.00", reference_event_id="nope",
    ))
    data = audit(client)
    assert anomaly_kinds(data) == {("reference_not_found", "r-ghost")}


def test_cross_account_reference_anomaly(client):
    post_event(client, make_event("inc-a", account_id="acct-a"))
    post_event(client, make_event(
        "r-cross", account_id="acct-b", type_="refund", amount="10.00",
        reference_event_id="inc-a",
    ))

    data = audit(client, account="acct-b")
    assert anomaly_kinds(data) == {("cross_account_or_currency", "r-cross")}
    # acct-a view sees only its income, untouched.
    view_a = audit(client, account="acct-a")
    assert income_by_id(view_a, "inc-a")["refunded_amount"] == "0.00"


def test_cross_currency_reference_anomaly(client):
    post_event(client, make_event("inc-1", currency="USD"))
    post_event(client, make_event(
        "r-eur", type_="refund", amount="10.00", currency="EUR",
        reference_event_id="inc-1",
    ))

    data = audit(client)
    kinds = anomaly_kinds(data)
    assert ("cross_account_or_currency", "r-eur") in kinds
    assert income_by_id(data, "inc-1")["refunded_amount"] == "0.00"


def test_reference_to_refund_is_not_income(client):
    post_event(client, make_event("r-base", type_="refund", amount="5.00"))
    post_event(client, make_event(
        "r-chain", type_="refund", amount="5.00", reference_event_id="r-base",
    ))

    data = audit(client)
    assert anomaly_kinds(data) == {
        ("missing_reference", "r-base"),
        ("reference_not_income", "r-chain"),
    }


def test_over_refund_whole_refund_rejected(client):
    post_event(client, make_event("inc-1", amount="100.00"))
    post_event(client, make_event(
        "r1", type_="refund", amount="60.00",
        occurred_at="2026-09-11T10:00:00Z", reference_event_id="inc-1",
    ))
    post_event(client, make_event(
        "r2", type_="refund", amount="50.00",
        occurred_at="2026-09-12T10:00:00Z", reference_event_id="inc-1",
    ))

    data = audit(client)
    assert anomaly_kinds(data) == {("over_refund", "r2")}
    over = next(a for a in data["anomalies"] if a["event_id"] == "r2")
    assert over["cumulative_amount"] == "110.00"
    assert over["income_amount"] == "100.00"
    inc = income_by_id(data, "inc-1")
    # Whole over-refund is rejected, not clipped to the 40.00 remainder.
    assert inc["refunded_amount"] == "60.00"
    assert inc["remaining_amount"] == "40.00"
    assert inc["refund_event_ids"] == ["r1"]


def test_exact_refund_is_not_over(client):
    post_event(client, make_event("inc-1", amount="100.00"))
    post_event(client, make_event(
        "r1", type_="refund", amount="100.00", reference_event_id="inc-1",
    ))
    data = audit(client)
    inc = income_by_id(data, "inc-1")
    assert inc["refunded_amount"] == "100.00"
    assert inc["remaining_amount"] == "0.00"
    assert data["anomalies"] == []


def test_anomalies_sorted_by_event_id(client):
    post_event(client, make_event("r-z", type_="refund", amount="1.00"))
    post_event(client, make_event("r-a", type_="refund", amount="1.00"))

    data = audit(client)
    assert [a["event_id"] for a in data["anomalies"]] == ["r-a", "r-z"]


# ---------------------------------------------------------------- validation


def test_as_of_is_required(client):
    resp = client.get("/accounts/acct-1/refund-audit")
    assert resp.status_code == 422


def test_tie_break_by_event_id(client):
    # Two refunds at the same occurred_at: event_id order decides accumulation.
    post_event(client, make_event("inc-1", amount="100.00"))
    post_event(client, make_event(
        "r-b", type_="refund", amount="60.00",
        occurred_at="2026-09-11T10:00:00Z", reference_event_id="inc-1",
    ))
    post_event(client, make_event(
        "r-a", type_="refund", amount="60.00",
        occurred_at="2026-09-11T10:00:00Z", reference_event_id="inc-1",
    ))
    data = audit(client)
    inc = income_by_id(data, "inc-1")
    assert inc["refund_event_ids"] == ["r-a"]
    assert anomaly_kinds(data) == {("over_refund", "r-b")}
