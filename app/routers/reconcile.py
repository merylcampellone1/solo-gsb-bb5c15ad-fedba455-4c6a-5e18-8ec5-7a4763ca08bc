"""POST /reconcile -- replay a time window and diff against the source system."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..schemas import ReconcileRequest, ReconcileResponse
from ..services import reconcile
from .webhooks import get_db

router = APIRouter(tags=["reconciliation"])


@router.post("/reconcile", response_model=ReconcileResponse)
def reconcile_window(req: ReconcileRequest, db: Session = Depends(get_db)):
    return reconcile(db, req)
