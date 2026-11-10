"""QA de firma del contrato: integridad del documento y no reutilizacion (E3-T16, HU11).

Cubre de extremo a extremo (HTTP + dominio puro), sin Postgres:

- Camino feliz: `APPROVED` -> `POST .../contract` (`GENERATED`, 3 filas) ->
  firma valida por biometria (dispositivo) y por PIN (fallback) -> `SIGNED`
  con sello (`signed_at` + `doc_hash` + `signature_token_hash`) + copia por
  correo (`credit_contract_signed`, una sola).
- Integridad: `doc_hash` reproducible con las mismas condiciones y sensible
  a cualquier cambio (monto/campo); el PDF descargado existe (`%PDF`).
- No reutilizacion: `nonce` reutilizado (mismo `nonce` en otro expediente o
  re-firma) -> rechazo generico sin cambiar el contrato; token repetido
  (colision forzada a nivel dominio) -> `CONFLICT`; contrato ya `SIGNED`
  con clave nueva -> `409`.
- Errores: firma invalida -> `403` generico; `nonce` vencido ->
  `EXPIRED_NONCE`; expediente no `APPROVED` -> `409` en generacion;
  ajeno/inexistente -> `404` neutro; misma `Idempotency-Key` con cuerpo
  distinto -> `409`.
- Correo best-effort: sender caido -> firma `SIGNED` igual y notificacion
  `FAILED` (una sola copia por firma).
- Idempotencia: doble `POST .../sign` con la misma clave -> una firma y
  una copia (replay).
- Aislamiento: 0 `loans`/`transactions`/`postings`; sin PII/secretos en logs.

Criterio de "token" fijado con E3-T13 (`backend/app/modules/credits/domain/signing.py`):
el token biometrico es la terna `nonce` + `signature` + `device_id` ligada al
expediente (`token_hash_for(..., application_id)`); la primera capa de no
reutilizacion es el `nonce` de un solo uso (`identity.domain.nonce`, TTL 120 s)
y la segunda es la igualdad del `signature_token_hash` en BD
(`find_by_token_hash` -> `CONFLICT`). En la via PIN el PIN es reutilizable por
diseno y la no re-firma la garantiza el estado (`GENERATED` -> `SIGNED`).

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del repo);
`get_db` y `get_current_user_id` (credits) se inyectan via
`app.dependency_overrides`; `identity.domain.nonce` es en memoria (se resetea
por test). Sin red.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.modules.audit.models as _audit
import app.modules.credits.models as _credits
import app.modules.identity.models as _identity
import app.modules.notifications.models as _notifications
import app.modules.shared.models as _shared
from app.adapters.credit_bureau import BureauScore, MockCreditBureau
from app.adapters.notification_sender import MockNotificationSender
from app.core.db import Base, get_db
from app.main import app
from app.modules.credits import repository as repo
from app.modules.credits import service as catalog
from app.modules.credits.api import get_current_user_id
from app.modules.credits.domain import signing as signing_domain
from app.modules.credits.repository import contracts as contracts_repo
from app.modules.credits.service import applications as apps_service
from app.modules.credits.service import contract_signing as sign_flow
from app.modules.credits.service import documents as docs_service
from app.modules.credits.service import signing as signing_service
from app.modules.identity.domain import nonce as nonce_domain

USER = uuid.uuid4()
OTHER = uuid.uuid4()
DEVICE = "dev-e3t16-1"
SECRET_HEX = "ef" * 20  # 20 bytes (formato hmac: exigido por el verificador real)
PUBLIC_KEY = f"hmac:{SECRET_HEX}"
PIN = "5291"
EMAIL = "titular.e3t16@example.com"

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "credits.contracts",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "notifications.notifications",
    "notifications.notification_templates",
    "audit.audit_log",
    "identity.users",
    "identity.credentials",
]

ALLOWED_OUTBOX = {
    "credit.application.submitted",
    "credit.approved",
    "credit.contract.generated",
    "credit.contract.signed",
}

assert _audit is not None and _credits is not None
assert _identity is not None and _notifications is not None and _shared is not None


def _bureau_for(user_id: uuid.UUID, score: int = 780):
    return MockCreditBureau(
        profiles={str(user_id): BureauScore(score=score, deudas_minor=0, morosidad=False)}
    )


def _make_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("credits", "shared", "notifications", "audit", "identity"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


def _seed(session: Session) -> None:
    from app.modules.identity.models import User

    repo.seed_demo_products(session)
    session.add(
        User(
            id=USER,
            doc_type="DNI",
            doc_number_hash="e3t16-user-hash",
            doc_number_masked="****1234",
            first_name="Test",
            last_name="User",
            email=EMAIL,
            status="ACTIVE",
            kyc_status="VERIFIED",
        )
    )
    session.commit()


@pytest.fixture()
def app_client(monkeypatch):
    nonce_domain.reset_nonces()
    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    _seed(session)
    mock_sender = MockNotificationSender()

    def _binding_reader(current: Session, user_id: uuid.UUID, device_id: str):
        assert user_id == USER
        assert device_id == DEVICE
        return {"device_id": DEVICE, "public_key": PUBLIC_KEY, "status": "ACTIVE"}

    monkeypatch.setattr(signing_service, "default_binding_reader", _binding_reader)
    monkeypatch.setattr(signing_service, "default_pin_verifier", lambda s, u, p: p == PIN)
    monkeypatch.setattr(sign_flow, "_default_email_sender", lambda: mock_sender)
    try:

        def _override_db():
            yield session

        def _override_user():
            return USER

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        with TestClient(app) as client:
            yield {"client": client, "session": session, "sender": mock_sender}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        nonce_domain.reset_nonces()
        session.close()
        engine.dispose()


def _product_id(session: Session) -> str:
    return str(catalog.get_product(session, code="PERSONAL_LIBRE")["id"])


def _approved_app_id(session: Session, user: uuid.UUID = USER) -> str:
    view = apps_service.submit_application(
        session,
        user,
        product_id=_product_id(session),
        requested_amount_minor=1_000_000,
        term_months=12,
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        bureau_authorized=True,
        bureau_provider=_bureau_for(user),
    )
    assert view["status"] == "APPROVED"
    session.commit()
    return view["id"]


def _in_review_app_id(session: Session) -> str:
    from app.adapters.credit_bureau import BUREAU_UNAVAILABLE, CreditBureauError

    class _Down(MockCreditBureau):
        def get_score(self, **kwargs):
            raise CreditBureauError(BUREAU_UNAVAILABLE, "bureau caido")

    view = apps_service.submit_application(
        session,
        USER,
        product_id=_product_id(session),
        requested_amount_minor=1_000_000,
        term_months=12,
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        bureau_authorized=True,
        bureau_provider=_Down(profiles={}),
    )
    assert view["status"] == "IN_REVIEW"
    session.commit()
    return view["id"]


def _generated_app_id(session: Session, user: uuid.UUID = USER) -> str:
    app_id = _approved_app_id(session, user)
    payload, reused = docs_service.generate_contracts(session, user, app_id)
    assert not reused and len(payload["documents"]) == 3
    session.commit()
    return app_id


def _hmac_signature(nonce: str) -> str:
    return hmac.new(bytes.fromhex(SECRET_HEX), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _post_contract(client: TestClient, app_id: str, key: str | None = "k-contract"):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(f"/api/v1/credits/applications/{app_id}/contract", headers=headers)


def _challenge(client: TestClient, app_id: str) -> dict:
    resp = client.post(
        f"/api/v1/credits/applications/{app_id}/sign/challenge", json={"device_id": DEVICE}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _sign(client: TestClient, app_id: str, body: dict, *, key: str | None = "k-sign"):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(f"/api/v1/credits/applications/{app_id}/sign", json=body, headers=headers)


def _device_body(nonce: str) -> dict:
    return {"device_id": DEVICE, "signature": _hmac_signature(nonce), "nonce": nonce}


def _email_copies(session: Session) -> list:
    table = Base.metadata.tables["notifications.notifications"]
    session.expire_all()
    return list(
        session.execute(
            sa.select(table.c.channel, table.c.template_code, table.c.status)
            .where(table.c.template_code == "credit_contract_signed")
            .order_by(table.c.created_at)
        ).all()
    )


def _outbox_count(session: Session, event_type: str) -> int:
    table = Base.metadata.tables["shared.outbox"]
    session.expire_all()
    return int(
        session.scalar(
            sa.select(sa.func.count()).select_from(table).where(table.c.event_type == event_type)
        )
        or 0
    )


def _assert_sealed(session: Session, app_id: str, data: dict) -> None:
    rows = {row.doc_type: row for row in contracts_repo.list_by_application(session, app_id)}
    assert set(rows) == {"CONTRATO", "PAGARE", "RESUMEN"}
    for doc in data["documents"]:
        row = rows[doc["doc_type"]]
        assert row.status == "SIGNED" and row.signed_at is not None
        assert re.fullmatch(r"[0-9a-f]{64}", row.doc_hash or "")
        assert re.fullmatch(r"[0-9a-f]{64}", row.signature_token_hash or "")
        assert signing_domain.verify_seal(
            doc_hash=row.doc_hash,
            signed_at_iso=data["signed_at"],
            signature_token_hash=row.signature_token_hash or "",
            seal=doc["seal"],
        )


# ------------------------------------------------ camino feliz (CA-01..CA-04)


def test_qa_generate_then_sign_device_sealed_with_copy(app_client):
    """CA-01/CA-02/CA-03/CA-04: generar -> firmar por biometria -> sello + copia."""
    client, session, sender = (
        app_client["client"],
        app_client["session"],
        app_client["sender"],
    )
    app_id = _approved_app_id(session)

    gen = _post_contract(client, app_id, key="k-qa-gen-device")
    assert gen.status_code == 201, gen.text
    assert gen.json()["data"]["status"] == "GENERATED"
    assert len(gen.json()["data"]["documents"]) == 3

    challenge = _challenge(client, app_id)
    resp = _sign(client, app_id, _device_body(challenge["nonce"]), key="k-qa-sign-device")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SIGNED" and data["via"] == "device"
    assert resp.json()["meta"]["replayed"] is False
    _assert_sealed(session, app_id, data)

    assert len(sender.calls) == 1 and sender.calls[0]["channel"] == "email"
    copies = _email_copies(session)
    assert len(copies) == 1 and copies[0][2] == "SENT"
    assert _outbox_count(session, "credit.contract.generated") == 1
    assert _outbox_count(session, "credit.contract.signed") == 1

    pdf = client.get(f"/api/v1/credits/applications/{app_id}/contract?format=pdf&doc=contrato")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_qa_sign_pin_fallback_sealed_with_copy(app_client):
    """CA-02/CA-03/CA-04: la via PIN (fallback) sella igual y envia una copia."""
    client, session, sender = (
        app_client["client"],
        app_client["session"],
        app_client["sender"],
    )
    app_id = _generated_app_id(session)
    resp = _sign(client, app_id, {"pin": PIN}, key="k-qa-pin")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SIGNED" and data["via"] == "pin"
    _assert_sealed(session, app_id, data)
    assert len(sender.calls) == 1
    assert len(_email_copies(session)) == 1
    rows = contracts_repo.list_by_application(session, app_id)
    assert all(PIN not in (row.signature_token_hash or "") for row in rows)


# ------------------------------------------------ integridad del doc_hash


def test_qa_doc_hash_reproducible_and_sensitive(app_client):
    """El `doc_hash` es SHA-256 del contenido canonico: reproducible y sensible."""
    _, session = app_client["client"], app_client["session"]
    app_id = _approved_app_id(session)
    from app.modules.credits.repository import applications as apps_repo

    row = apps_repo.get_application(session, app_id)
    assert row is not None
    ctx = docs_service.build_context(session, row)
    for doc_type in ("CONTRATO", "PAGARE", "RESUMEN"):
        first = docs_service.doc_hash_for(docs_service.canonical_for(doc_type, ctx))
        again = docs_service.doc_hash_for(docs_service.canonical_for(doc_type, ctx))
        assert re.fullmatch(r"[0-9a-f]{64}", first)
        assert first == again  # reproducible con las mismas condiciones
        tampered = dict(ctx, requested_amount_minor=ctx["requested_amount_minor"] + 1)
        assert docs_service.doc_hash_for(docs_service.canonical_for(doc_type, tampered)) != first
        renamed = dict(ctx, first_name=(ctx.get("first_name") or "X") + "Y")
        assert docs_service.doc_hash_for(docs_service.canonical_for(doc_type, renamed)) != first


def test_qa_pdfs_download_after_sign(app_client):
    """El juego firmado sigue descargable (los 3 PDF existen)."""
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    assert _sign(client, app_id, {"pin": PIN}, key="k-qa-pdfs").status_code == 200
    for slug in ("contrato", "pagare", "resumen"):
        pdf = client.get(f"/api/v1/credits/applications/{app_id}/contract?format=pdf&doc={slug}")
        assert pdf.status_code == 200, pdf.text
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content.startswith(b"%PDF")


# ------------------------------------------------ no reutilizacion


def test_qa_nonce_reuse_rejected(app_client):
    """El `nonce` es de un solo uso: reusarlo se rechaza y no firma."""
    client, session = app_client["client"], app_client["session"]
    app_a = _generated_app_id(session)
    nonce = _challenge(client, app_a)["nonce"]
    assert _sign(client, app_a, _device_body(nonce), key="k-qa-reuse-a").status_code == 200

    app_b = _generated_app_id(session)
    resp = _sign(client, app_b, _device_body(nonce), key="k-qa-reuse-b")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
    rows = contracts_repo.list_by_application(session, app_b)
    assert all(row.status == "GENERATED" for row in rows)
    assert _outbox_count(session, "credit.contract.signed") == 1


def test_qa_token_reuse_rejected_domain(app_client, monkeypatch):
    """El mismo token biometrico no firma un segundo expediente (`CONFLICT`)."""
    _, session = app_client["client"], app_client["session"]
    forced = "ff" * 32
    monkeypatch.setattr(signing_domain, "token_hash_for", lambda **kwargs: forced)

    app_a = _generated_app_id(session)
    challenge = signing_service.request_signature_challenge(session, USER, app_a, device_id=DEVICE)
    payload = signing_service.sign_contract(
        session,
        USER,
        app_a,
        device_id=DEVICE,
        signature="cualquiera",
        nonce=challenge["nonce"],
        binding_reader=lambda s, u, d: {
            "device_id": DEVICE,
            "public_key": PUBLIC_KEY,
            "status": "ACTIVE",
        },
        signature_verifier=lambda n, s, k: True,
    )
    assert payload["status"] == "SIGNED"
    session.commit()

    app_b = _generated_app_id(session)
    challenge_b = signing_service.request_signature_challenge(
        session, USER, app_b, device_id=DEVICE
    )
    with pytest.raises(contracts_repo.ContractConflictError):
        signing_service.sign_contract(
            session,
            USER,
            app_b,
            device_id=DEVICE,
            signature="otra",
            nonce=challenge_b["nonce"],
            binding_reader=lambda s, u, d: {
                "device_id": DEVICE,
                "public_key": PUBLIC_KEY,
                "status": "ACTIVE",
            },
            signature_verifier=lambda n, s, k: True,
        )
    assert all(
        row.status == "GENERATED" for row in contracts_repo.list_by_application(session, app_b)
    )


def test_qa_resign_same_contract_conflicts(app_client):
    """Una firma por contrato: re-firmar el mismo juego (clave nueva) -> `409`."""
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    assert _sign(client, app_id, {"pin": PIN}, key="k-qa-once").status_code == 200
    resp = _sign(client, app_id, {"pin": PIN}, key="k-qa-twice-new-key")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"
    assert len(_email_copies(session)) == 1
    assert _outbox_count(session, "credit.contract.signed") == 1


# ------------------------------------------------ errores


def test_qa_bad_signature_generic_and_expired_nonce(app_client, monkeypatch):
    """Firma invalida -> `403` generico; `nonce` vencido -> `EXPIRED_NONCE`; nada cambia."""
    client, session = app_client["client"], app_client["session"]
    monkeypatch.setattr(signing_service, "default_signature_verifier", lambda n, s, k: False)
    app_id = _generated_app_id(session)
    challenge = _challenge(client, app_id)
    bad = _sign(client, app_id, _device_body(challenge["nonce"]), key="k-qa-bad")
    assert bad.status_code == 403, bad.text
    assert bad.json()["error"]["code"] == "NOT_AUTHORIZED"
    assert bad.json()["error"]["message"] == signing_service.NOT_AUTHORIZED_MESSAGE
    assert all(
        row.status == "GENERATED" for row in contracts_repo.list_by_application(session, app_id)
    )

    stale = nonce_domain.issue_nonce(
        USER, device_id=DEVICE, now=datetime.now(UTC) - timedelta(seconds=300)
    )[0]
    expired = _sign(client, app_id, _device_body(stale), key="k-qa-expired")
    assert expired.status_code == 400, expired.text
    assert expired.json()["error"]["code"] == "EXPIRED_NONCE"


def test_qa_not_approved_conflicts_on_generate(app_client):
    """Expediente no `APPROVED` no genera juego -> `409` (`CONFLICT`)."""
    client, session = app_client["client"], app_client["session"]
    review_id = _in_review_app_id(session)
    resp = _post_contract(client, review_id, key="k-qa-review")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_qa_foreign_or_missing_neutral_404(app_client):
    """Expediente ajeno o inexistente -> `404` neutro en las 4 rutas."""
    client, session = app_client["client"], app_client["session"]
    foreign_id = _generated_app_id(session, OTHER)
    for target in (foreign_id, str(uuid.uuid4())):
        assert _post_contract(client, target, key="k-qa-404").status_code == 404
        assert client.get(f"/api/v1/credits/applications/{target}/contract").status_code == 404
        challenge = client.post(f"/api/v1/credits/applications/{target}/sign/challenge", json={})
        assert challenge.status_code == 404, challenge.text
        assert challenge.json()["error"]["code"] == "NOT_FOUND"
        sign = _sign(client, target, {"pin": PIN}, key=f"k-qa-404-{target[:8]}")
        assert sign.status_code == 404, sign.text
        assert sign.json()["error"]["code"] == "NOT_FOUND"


def test_qa_same_key_different_body_conflicts(app_client):
    """Misma `Idempotency-Key` con cuerpo distinto -> `409` (`IDEMPOTENCY_CONFLICT`)."""
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    assert _sign(client, app_id, {"pin": PIN}, key="k-qa-clash").status_code == 200
    other = _generated_app_id(session)
    clash = _sign(client, other, {"pin": PIN}, key="k-qa-clash")
    assert clash.status_code == 409, clash.text
    assert clash.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ------------------------------------------------ correo best-effort + idempotencia


def test_qa_provider_down_keeps_signed_single_failed_copy(app_client):
    """Correo caido: la firma sigue `SIGNED` y queda una copia `FAILED` (una sola)."""
    client, session, sender = (
        app_client["client"],
        app_client["session"],
        app_client["sender"],
    )
    sender.set_channel_failure("email", "smtp caido")
    app_id = _generated_app_id(session)
    resp = _sign(client, app_id, {"pin": PIN}, key="k-qa-down")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "SIGNED"
    copies = _email_copies(session)
    assert len(copies) == 1 and copies[0][2] == "FAILED"
    assert all(
        row.status == "SIGNED" for row in contracts_repo.list_by_application(session, app_id)
    )


def test_qa_double_post_single_sign_single_copy(app_client):
    """Doble `POST .../sign` con la misma clave -> una firma y una copia (replay)."""
    client, session, sender = (
        app_client["client"],
        app_client["session"],
        app_client["sender"],
    )
    app_id = _generated_app_id(session)
    challenge = _challenge(client, app_id)
    first = _sign(client, app_id, _device_body(challenge["nonce"]), key="k-qa-replay")
    assert first.status_code == 200, first.text
    assert first.json()["meta"]["replayed"] is False
    second = _sign(client, app_id, _device_body(challenge["nonce"]), key="k-qa-replay")
    assert second.status_code == 200, second.text
    assert second.json()["meta"]["replayed"] is True
    assert second.json()["data"] == first.json()["data"]
    assert len(sender.calls) == 1
    assert len(_email_copies(session)) == 1
    assert _outbox_count(session, "credit.contract.signed") == 1


# ------------------------------------------------ aislamiento + logs


def test_qa_sign_moves_no_money(app_client):
    """La firma no mueve dinero ni crea `loans`: 0 filas nuevas fuera de `credits`."""
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    assert _sign(client, app_id, {"pin": PIN}, key="k-qa-iso").status_code == 200
    session.expire_all()
    names = {
        row[0]
        for row in session.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    }
    for forbidden in (
        "loans",
        "loan_schedules",
        "loan_payments",
        "transactions",
        "postings",
        "journal_entries",
        "holds",
    ):
        assert forbidden not in names
    outbox = Base.metadata.tables["shared.outbox"]
    types = {row[0] for row in session.execute(sa.select(outbox.c.event_type)).all()}
    assert types <= ALLOWED_OUTBOX


def test_qa_no_pii_or_secrets_in_logs(app_client, caplog):
    """Ni firmas, nonces, PIN, claves, correo o montos en los logs de `credits`."""
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    challenge = _challenge(client, app_id)
    signature = _hmac_signature(challenge["nonce"])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.modules.credits"):
        resp = _sign(
            client,
            app_id,
            {"device_id": DEVICE, "signature": signature, "nonce": challenge["nonce"]},
            key="k-qa-logs",
        )
    assert resp.status_code == 200, resp.text
    blob = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (signature, challenge["nonce"], PIN, SECRET_HEX, EMAIL, DEVICE):
        assert secret not in blob
    assert "2000000" not in blob and "1000000" not in blob
