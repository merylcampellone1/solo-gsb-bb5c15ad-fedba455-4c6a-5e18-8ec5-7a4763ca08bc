"""Runtime configuration, sourced from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    webhook_secret: str
    # Max allowed clock skew between the sender and this service when
    # validating the signature timestamp (replay protection).
    signature_tolerance_seconds: int = 300


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/ledger.db"),
        webhook_secret=os.getenv("WEBHOOK_SECRET", "dev-webhook-secret"),
        signature_tolerance_seconds=int(os.getenv("SIGNATURE_TOLERANCE_SECONDS", "300")),
    )
