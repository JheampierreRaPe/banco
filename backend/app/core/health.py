"""Healthchecks de la aplicacion."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    database_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - healthcheck no debe fallar
        database_ok = False
    return {"status": "ok" if database_ok else "degraded", "database": database_ok}
