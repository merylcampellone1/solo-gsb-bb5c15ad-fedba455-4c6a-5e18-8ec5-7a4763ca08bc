"""End-to-end tests: signature auth, idempotency, out-of-order folding, reconciliation."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

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


def sign(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict[str, str]:
    ts = ts if ts is not None else int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Webhook-Timestamp": str(ts), "X-Webhook-Signature": f"sha256={sig}"}


def post_event(client: TestClient, event: dict, **sign_kwargs):
    body = json.dumps(event).encode()
    return client.post("/webhooks/events", content=body, headers=sign(body, **sign_kwargs))


def make_event(event_id: str, account_id: str = "acct-1", type_: str = "income",
               amount: str = "100.00", currency: str = "USD",
               occurred_at: str = "2026-09-01T10:00:00Z", **extra) -> dict:
    return {
        "event_id": event_id,
        "account_id": account_id,
        "type": type_,
        "amount": amount,
        "currency": currency,
        "occurred_at": occurred_at,
        **extra,
    }


# ---------------------------------------------------------------- signature


def test_valid_signed_event_is_processed(client):
    resp = post_event(client, make_event("evt-1"))
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "processed"
    assert data["balance"]["balance"] == "100.00"
    assert data["balance"]["currency"] == "USD"


def test_missing_signature_headers_rejected(client):
    resp = client.post("/webhooks/events", content=json.dumps(make_event("evt-1")).encode())
    assert resp.status_code == 401


def test_wrong_signature_rejected(client):
    body = json.dumps(make_event("evt-1")).encode()
    headers = sign(body, secret="wrong-secret")
    resp = client.post("/webhooks/events", content=body, headers=headers)
    assert resp.status_code == 401


def test_tampered_body_rejected(client):
    body = json.dumps(make_event("evt-1")).encode()
    headers = sign(body)
    tampered = json.dumps(make_event("evt-1", amount="999.99")).encode()
    resp = client.post("/webhooks/events", content=tampered, headers=headers)
    assert resp.status_code == 401


def test_stale_timestamp_rejected(client):
    old_ts = int(time.time()) - 3600
    resp = post_event(client, make_event("evt-1"), ts=old_ts)
    assert resp.status_code == 401


# -------------------------------------------------------------- idempotency


def test_duplicate_event_is_idempotent(client):
    event = make_event("evt-1")
    assert post_event(client, event).json()["status"] == "processed"

    resp = post_event(client, event)
    assert resp.status_code == 201
    assert resp.json()["status"] == "duplicate"
    # Balance must not be applied twice.
    assert resp.json()["balance"]["balance"] == "100.00"
    assert resp.json()["balance"]["event_count"] == 1


def test_same_event_id_different_payload_conflicts(client):
    assert post_event(client, make_event("evt-1")).status_code == 201
    resp = post_event(client, make_event("evt-1", amount="50.00"))
    assert resp.status_code == 409


# ------------------------------------------------- out-of-order balance fold


def test_out_of_order_events_fold_to_same_balance(client):
    # Refund arrives *before* the income it refers to (occurred_at is later).
    refund = make_event(
        "evt-refund", type_="refund", amount="30.00",
        occurred_at="2026-09-01T12:00:00Z", reference_event_id="evt-income",
    )
    income = make_event("evt-income", amount="100.00", occurred_at="2026-09-01T10:00:00Z")

    assert post_event(client, refund).status_code == 201
    assert post_event(client, income).status_code == 201

    resp = client.get("/accounts/acct-1/balance")
    assert resp.status_code == 200
    balances = {b["currency"]: b["balance"] for b in resp.json()["balances"]}
    assert balances["USD"] == "70.00"


def test_projection_matches_full_fold(client):
    events = [
        make_event("e1", amount="100.00", occurred_at="2026-09-01T10:00:00Z"),
        make_event("e2", type_="refund", amount="25.50", occurred_at="2026-09-03T10:00:00Z"),
        make_event("e3", amount="10.25", occurred_at="2026-09-02T10:00:00Z"),  # out of order
        make_event("e4", amount="0.01", occurred_at="2026-09-01T09:00:00Z"),
    ]
    for e in events:
        assert post_event(client, e).status_code == 201

    projected = client.get("/accounts/acct-1/balance").json()
    folded = client.get("/accounts/acct-1/balance", params={"recompute": "true"}).json()
    assert projected["balances"] == folded["balances"]
    assert folded["balances"][0]["balance"] == "84.76"
    assert folded["balances"][0]["event_count"] == 4


def test_multi_currency_balances(client):
    post_event(client, make_event("e1", amount="100.00", currency="USD"))
    post_event(client, make_event("e2", amount="200.00", currency="EUR"))
    post_event(client, make_event("e3", type_="refund", amount="50.00", currency="EUR"))

    resp = client.get("/accounts/acct-1/balance")
    balances = {b["currency"]: b["balance"] for b in resp.json()["balances"]}
    assert balances == {"USD": "100.00", "EUR": "150.00"}


def test_accounts_are_isolated(client):
    post_event(client, make_event("e1", account_id="acct-a", amount="10.00"))
    post_event(client, make_event("e2", account_id="acct-b", amount="20.00"))

    a = client.get("/accounts/acct-a/balance").json()["balances"]
    b = client.get("/accounts/acct-b/balance").json()["balances"]
    assert a[0]["balance"] == "10.00"
    assert b[0]["balance"] == "20.00"


# ------------------------------------------------------------ reconciliation


def seed_window(client):
    events = [
        make_event("w1", amount="100.00", occurred_at="2026-09-10T08:00:00Z"),
        make_event("w2", type_="refund", amount="40.00", occurred_at="2026-09-10T09:00:00Z"),
        make_event("w3", amount="10.00", occurred_at="2026-09-10T10:00:00Z"),
        make_event("outside", amount="999.00", occurred_at="2026-09-20T10:00:00Z"),
    ]
    for e in events:
        assert post_event(client, e).status_code == 201


WINDOW = {"start": "2026-09-10T00:00:00Z", "end": "2026-09-10T23:59:59Z"}


def test_reconcile_in_sync(client):
    seed_window(client)
    resp = client.post("/reconcile", json={
        "account_id": "acct-1", **WINDOW,
        "expected_balance": {"USD": "70.00"},
        "expected_events": [
            {"event_id": "w1", "type": "income", "amount": "100.00"},
            {"event_id": "w2", "type": "refund", "amount": "40.00"},
            {"event_id": "w3", "type": "income", "amount": "10.00"},
        ],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["in_sync"] is True
    assert data["discrepancies"] == []
    assert data["ledger_event_count"] == 3  # window excludes "outside"
    assert data["ledger_net"] == {"USD": "70.00"}


def test_reconcile_detects_missing_and_extra_events(client):
    seed_window(client)
    resp = client.post("/reconcile", json={
        "account_id": "acct-1", **WINDOW,
        "expected_events": [
            {"event_id": "w1"},
            {"event_id": "w2"},
            {"event_id": "w4"},  # source thinks it sent w4; ledger never got it
        ],
    })
    kinds = {(d["kind"], d["event_id"]) for d in resp.json()["discrepancies"]}
    assert ("missing_in_ledger", "w4") in kinds
    assert ("missing_in_source", "w3") in kinds
    assert resp.json()["in_sync"] is False


def test_reconcile_detects_amount_mismatch(client):
    seed_window(client)
    resp = client.post("/reconcile", json={
        "account_id": "acct-1", **WINDOW,
        "expected_events": [{"event_id": "w1", "amount": "90.00"}],
    })
    discrepancies = resp.json()["discrepancies"]
    assert any(d["kind"] == "amount_mismatch" and d["event_id"] == "w1" for d in discrepancies)


def test_reconcile_detects_balance_mismatch(client):
    seed_window(client)
    resp = client.post("/reconcile", json={
        "account_id": "acct-1", **WINDOW,
        "expected_balance": {"USD": "65.00"},
    })
    data = resp.json()
    assert data["in_sync"] is False
    mismatch = [d for d in data["discrepancies"] if d["kind"] == "balance_mismatch"]
    assert mismatch[0]["ledger_value"] == "70.00"
    assert mismatch[0]["expected_value"] == "65.00"


# ---------------------------------------------------------------- validation


def test_invalid_amount_precision_rejected(client):
    resp = post_event(client, make_event("evt-1", amount="10.001"))
    assert resp.status_code == 422


def test_negative_amount_rejected(client):
    resp = post_event(client, make_event("evt-1", amount="-5.00"))
    assert resp.status_code == 422


def test_invalid_event_type_rejected(client):
    resp = post_event(client, make_event("evt-1", type_="chargeback"))
    assert resp.status_code == 422


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
