"""Webhook Ledger API -- application factory and entrypoint."""
from __future__ import annotations

import os

from fastapi import FastAPI

from .config import Settings, get_settings
from .db import make_engine, make_session_factory
from .routers import balances, reconcile, refund_audit, webhooks


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    if settings.database_url.startswith("sqlite"):
        # Ensure the SQLite directory exists (e.g. ./data/ledger.db).
        path = settings.database_url.split("///", 1)[-1]
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    engine = make_engine(settings.database_url)

    app = FastAPI(
        title="Webhook Ledger API",
        version="1.0.0",
        description=(
            "Receives HMAC-signed income/refund webhook events, applies them "
            "idempotently, folds out-of-order events into account balances, "
            "and replays time windows for reconciliation."
        ),
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    app.include_router(webhooks.router)
    app.include_router(balances.router)
    app.include_router(reconcile.router)
    app.include_router(refund_audit.router)

    @app.get("/healthz", tags=["meta"])
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
