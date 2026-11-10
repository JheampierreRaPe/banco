"""Soporte real de RUC en el alta (E1-T36, HU01).

- `POST /auth/kyc/submit` acepta `document.type=RUC` con
  `applicant.business_name` (juridica: nombres vacios + razon social;
  natural: nombres completos sin razon social) y persiste la razon social
  en `identity.users.business_name`.
- Validacion por tipo ANTES del proveedor (`validate_applicant`): RUC sin
  razon ni nombres -> 422 sin invocar al proveedor; DNI sin nombres -> 422;
  tipo desconocido -> 422.
- `create_user`/`onboard_customer`/`persist_kyc_submission` propagan
  `business_name`; duplicado de RUC -> 409 `DUPLICATE_DOCUMENT`.
- Sin PII en logs (numero, nombres ni razon social en claro).

Patron de fixtures: `tests/test_kyc_proxy.py` (TestClient + SQLite ATTACH).
"""

from __future__ import annotations

import base64
import logging
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.adapters.kyc_provider import MockKycProvider
from app.core.db import Base, get_db
from app.main import app
from app.modules.identity.api.kyc import get_kyc_provider
from app.modules.identity.service import kyc_proxy

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode()
JPEG_B64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 100).decode()


class RecordingKycProvider(MockKycProvider):
    """Mock que registra si el proveedor llego a invocarse (sin red)."""

    def __init__(self, mode: str = "success") -> None:
        super().__init__(mode=mode)
        self.calls: list = []

    def verify_full(self, *, session_id, payload=None):
        self.calls.append(("verify_full", session_id, payload))
        return super().verify_full(session_id=session_id, payload=payload)


def _ruc_number() -> str:
    return f"20{uuid.uuid4().int % 10**9:09d}"


def _submit_payload(
    *,
    doc_type: str = "RUC",
    doc_number: str | None = None,
    first_name: str = "",
    last_name: str = "",
    business_name: str | None = "ACME SAC",
    email: str | None = None,
) -> dict:
    applicant: dict = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email or f"ruc.{uuid.uuid4().hex[:8]}@example.com",
        "phone": "999888777",
    }
    if business_name is not None:
        applicant["business_name"] = business_name
    return {
        "challenge_token": "tok-ruc",
        "document": {
            "type": doc_type,
            "number": doc_number or _ruc_number(),
            "image_b64": PNG_B64,
        },
        "segments": [{"task": "blink", "image_b64": JPEG_B64}],
        "applicant": applicant,
    }


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada para el alta KYC con RUC (schemas ATTACH)."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401
    import app.modules.audit.models as _au  # noqa: F401
    import app.modules.identity.models as _i  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.notifications.models as _n  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "accounts", "ledger", "shared", "notifications", "audit"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["identity.kyc_verifications"],
            Base.metadata.tables["identity.otp_codes"],
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["notifications.notifications"],
            Base.metadata.tables["notifications.notification_templates"],
            Base.metadata.tables["audit.audit_log"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def kyc_client(monkeypatch, sqlite_session):
    """TestClient con proveedor mock en `success` y rate limit limpio."""
    monkeypatch.delenv("KYC_RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("KYC_RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("KYC_MAX_IMAGE_BYTES", raising=False)
    kyc_proxy.reset_rate_limits()
    app.dependency_overrides[get_kyc_provider] = lambda: MockKycProvider(mode="success")

    def _override_db():
        yield sqlite_session

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_kyc_provider, None)
        app.dependency_overrides.pop(get_db, None)
        kyc_proxy.reset_rate_limits()


# ------------------------------------------------------- validate_applicant (puro)
def test_validate_applicant_ruc_juridica_ok():
    out = kyc_proxy.validate_applicant("RUC", "", "", "ACME SAC")
    assert out == {
        "doc_type": "RUC",
        "first_name": "",
        "last_name": "",
        "business_name": "ACME SAC",
    }


def test_validate_applicant_ruc_natural_con_nombres_ok():
    out = kyc_proxy.validate_applicant("RUC", "Luis", "Tello", None)
    assert out["first_name"] == "Luis" and out["last_name"] == "Tello"
    assert out["business_name"] is None


def test_validate_applicant_ruc_sin_datos_422():
    with pytest.raises(kyc_proxy.KycProxyValidationError):
        kyc_proxy.validate_applicant("RUC", "", "", None)
    with pytest.raises(kyc_proxy.KycProxyValidationError):
        kyc_proxy.validate_applicant("RUC", "Solo", "", None)


def test_validate_applicant_dni_exige_nombres_e_ignora_razon():
    out = kyc_proxy.validate_applicant("DNI", "Ana", "Quispe", "ACME SAC")
    assert out["business_name"] is None
    with pytest.raises(kyc_proxy.KycProxyValidationError):
        kyc_proxy.validate_applicant("DNI", "", "Quispe", None)
    with pytest.raises(kyc_proxy.KycProxyValidationError):
        kyc_proxy.validate_applicant("DNI", "Ana", "", None)


def test_validate_applicant_rechaza_tipo_desconocido():
    with pytest.raises(kyc_proxy.KycProxyValidationError):
        kyc_proxy.validate_applicant("LICENCIA", "Ana", "Quispe", None)


# ------------------------------------------------------- submit RUC (API)
def test_submit_ruc_juridica_persiste_razon_social(kyc_client, sqlite_session):
    from app.modules.identity import repository as repo

    payload = _submit_payload()
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["overall_result"] is True
    assert data["status"] == "ONBOARDED"

    user = repo.get_user(sqlite_session, data["user_id"])
    assert user is not None
    assert user.doc_type == "RUC"
    assert user.business_name == "ACME SAC"
    assert user.first_name == "" and user.last_name == ""


def test_submit_ruc_natural_con_nombres_persiste_nombres(kyc_client, sqlite_session):
    from app.modules.identity import repository as repo

    payload = _submit_payload(first_name="Luis", last_name="Tello", business_name=None)
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 200, resp.text
    user = repo.get_user(sqlite_session, resp.json()["data"]["user_id"])
    assert user is not None
    assert user.doc_type == "RUC"
    assert (user.first_name, user.last_name) == ("Luis", "Tello")
    assert user.business_name is None


def test_submit_ruc_sin_datos_422_y_proveedor_no_invocado(kyc_client):
    recording = RecordingKycProvider()
    app.dependency_overrides[get_kyc_provider] = lambda: recording
    try:
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload(business_name=None))
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert recording.calls == [], "el proveedor no debe invocarse ante 422 previo"
    finally:
        app.dependency_overrides[get_kyc_provider] = lambda: MockKycProvider(mode="success")


def test_submit_dni_sin_nombres_422(kyc_client):
    payload = _submit_payload(doc_type="DNI", doc_number="12345678", business_name=None)
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_submit_duplicate_ruc_returns_409(kyc_client, sqlite_session):
    doc = _ruc_number()
    first = _submit_payload(doc_number=doc)
    assert kyc_client.post("/api/v1/auth/kyc/submit", json=first).status_code == 200
    resp = kyc_client.post("/api/v1/auth/kyc/submit", json=_submit_payload(doc_number=doc))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_DOCUMENT"
    count = sqlite_session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["identity.users"])
    )
    assert count == 1


# ------------------------------------------------------- repositorio / servicio
def test_create_user_ruc_juridica_y_dni(sqlite_session):
    from app.modules.identity import repository as repo

    user = repo.create_user(
        sqlite_session,
        doc_type="RUC",
        doc_number_hash=f"hash-{uuid.uuid4().hex}",
        first_name="",
        last_name="",
        business_name="ACME SAC",
        email=f"ruc.{uuid.uuid4().hex[:8]}@example.com",
    )
    assert user.business_name == "ACME SAC"
    assert user.first_name == "" and user.last_name == ""

    with pytest.raises(ValueError, match="business_name"):
        repo.create_user(
            sqlite_session,
            doc_type="RUC",
            doc_number_hash=f"hash-{uuid.uuid4().hex}",
            first_name="",
            last_name="",
            email=f"ruc.{uuid.uuid4().hex[:8]}@example.com",
        )
    sqlite_session.rollback()

    with pytest.raises(ValueError, match="first_name"):
        repo.create_user(
            sqlite_session,
            doc_type="DNI",
            doc_number_hash=f"hash-{uuid.uuid4().hex}",
            first_name="",
            last_name="Quispe",
            business_name="IGNORADA",
            email=f"dni.{uuid.uuid4().hex[:8]}@example.com",
        )
    sqlite_session.rollback()


def test_create_user_dni_ignora_business_name(sqlite_session):
    from app.modules.identity import repository as repo

    user = repo.create_user(
        sqlite_session,
        doc_type="DNI",
        doc_number_hash=f"hash-{uuid.uuid4().hex}",
        first_name="Ana",
        last_name="Quispe",
        business_name="IGNORADA",
        email=f"dni.{uuid.uuid4().hex[:8]}@example.com",
    )
    assert user.business_name is None


def test_onboard_customer_ruc_passthrough(sqlite_session):
    from app.modules.identity import repository as repo
    from app.modules.identity.service import onboard_customer

    doc_hash = f"hash-{uuid.uuid4().hex}"
    result = onboard_customer(
        sqlite_session,
        kyc_result={"overall_result": True, "doc_number_hash": doc_hash},
        first_name="",
        last_name="",
        doc_type="RUC",
        business_name="ACME SAC",
        email=f"ruc.{uuid.uuid4().hex[:8]}@example.com",
    )
    assert result["status"] == "ONBOARDED"
    user = repo.get_by_doc_hash(sqlite_session, doc_hash)
    assert user is not None and user.business_name == "ACME SAC"


def test_ruc_sin_pii_en_logs(kyc_client, caplog):
    # Sin PII en logs: ni numero de documento, ni nombres, ni razon social.
    # (El `challenge_token` opaco lo registra el mock del adaptador como
    # `session_id` —comportamiento preexistente fuera de frontera—; no es PII.)
    payload = _submit_payload(
        first_name="NombreUnico", last_name="ApellidoUnico", business_name="Razon Social Unica XYZ"
    )
    with caplog.at_level(logging.INFO):
        resp = kyc_client.post("/api/v1/auth/kyc/submit", json=payload)
    assert resp.status_code == 200, resp.text
    assert "Razon Social Unica XYZ" not in caplog.text
    assert "NombreUnico" not in caplog.text
    assert "ApellidoUnico" not in caplog.text
    assert payload["document"]["number"] not in caplog.text
    assert payload["applicant"]["email"] not in caplog.text
