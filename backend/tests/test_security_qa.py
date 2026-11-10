"""QA de seguridad transversal (Q-T03).

Suite reproducible (solo `backend/tests/`, SQLite en memoria con schemas
ATTACH, sin Postgres, sin `kyc-service/`) que cubre:

- CA-01 RBAC exhaustivo: sin token -> `401 NOT_AUTHENTICATED`; token de
  otro titular o rol insuficiente -> `403 NOT_AUTHORIZED` o `404 NOT_FOUND`
  neutro, segun el contrato de cada endpoint (creditos, transferencias,
  beneficiarios, movimientos, admin y firma).
- CA-02 anti-enumeracion: inexistente vs no autorizado/malformado con
  cuerpos indistinguibles (salvo `request_id`) en login, recovery/OTP,
  `resolve-target`, `pin-reset` y `document/lookup`.
- CA-03 idempotencia maliciosa: misma `Idempotency-Key` + cuerpo distinto
  -> `409` sin doble efecto; replay identico -> un solo efecto; reuso de
  `nonce`/firma -> `EXPIRED_NONCE`/rechazo neutro.
- CA-04 rate-limit/anti-abuso: `429 RATE_LIMITED` en OTP/recovery/KYC tras
  el umbral; login por contador (`423 ACCOUNT_LOCKED`); `authorize` con
  bloqueo preventivo (`BIOMETRIC_PREVENTIVE_BLOCK`, no 429).
- CA-05 inyeccion: SQL/JSON/cabeceras hostiles -> nunca `500` ni traza.
- CA-06 higiene: PII/secretos fuera de logs (`caplog`) y respuestas; `422`
  saneado (solo `loc`/`msg`/`type`).

Patron: `TestClient(app)` + override de `get_db` a SQLite (como
`test_auth_security_qa.py` / `test_profile_me.py`). Sin PII/secretos reales.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app

PIN = "482917"
WRONG_PIN = "000000"
PHONE_A = "+51999888777"
PHONE_B = "+51222222222"
PHONE_C = "+51333333333"
CCI_1 = "00110001111111111111"
CCI_2 = "00220002222222222222"
ACCNUM_A1 = "00110001111111"
ACCNUM_A2 = "00110003333333"
ACCNUM_B1 = "00110002222222"

ALLOWED_ERROR_CODES = {
    "NOT_AUTHENTICATED",
    "NOT_AUTHORIZED",
    "NOT_FOUND",
    "IDEMPOTENCY_CONFLICT",
    "CONFLICT",
    "RATE_LIMITED",
    "RESEND_LIMIT",
    "ACCOUNT_LOCKED",
    "INVALID_CREDENTIALS",
    "INVALID_LOGIN",
    "INVALID_OTP",
    "INVALID_PIN_RESET",
    "EXPIRED_NONCE",
    "EXPIRED_OTP",
    "BIOMETRIC_PREVENTIVE_BLOCK",
    "DAILY_LIMIT_EXCEEDED",
    "DUPLICATE_BENEFICIARY",
    "DUPLICATE_DOCUMENT",
    "DUPLICATE_EMAIL",
    "DOCUMENT_NOT_FOUND",
    "TARGET_NOT_FOUND",
    "TARGET_AMBIGUOUS",
    "VALIDATION_ERROR",
    "INSUFFICIENT_FUNDS",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sign_hmac(secret_hex: str, nonce: str) -> str:
    secret = bytes.fromhex(secret_hex)
    return hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _bearer(user_id: uuid.UUID, roles: list[str] | None = None) -> dict:
    from app.core.security import create_access_token

    token = create_access_token(subject=str(user_id), roles=roles)
    return {"Authorization": f"Bearer {token}"}


def _sans_request_id(body: dict) -> dict:
    clone = json.loads(json.dumps(body))
    if isinstance(clone, dict):
        if isinstance(clone.get("error"), dict):
            clone["error"].pop("request_id", None)
        if isinstance(clone.get("meta"), dict):
            clone["meta"].pop("request_id", None)
    return clone


@pytest.fixture()
def sec_session():
    """SQLite aislada con todos los schemas de la matriz RBAC via ATTACH."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _ac  # noqa: F401 (registro)
    import app.modules.audit.models as _au  # noqa: F401 (registro)
    import app.modules.credits.models as _cr  # noqa: F401 (registro)
    import app.modules.identity.models as _i  # noqa: F401 (registro)
    import app.modules.ledger.models as _le  # noqa: F401 (registro)
    import app.modules.notifications.models as _n  # noqa: F401 (registro)
    import app.modules.shared.models as _s  # noqa: F401 (registro)
    import app.modules.transactions.models as _t  # noqa: F401 (registro)

    try:
        import app.modules.risk.models as _rk  # noqa: F401 (registro)
    except ImportError:
        pass

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # Todos los schemas registrados (inmune al orden de ejecucion: si otra
    # suite importo mas modelos, sus schemas tambien se adjuntan), salvo
    # `wallet` (E4-T01, 2026-10-13): SQLite admite maximo 10 BD adjuntas
    # (`SQLITE_MAX_ATTACHED`, techo de compilacion sin PRAGMA que lo eleve)
    # y con 10 schemas con tablas + `config` se llegaria a 11. Esta suite
    # no ejercita endpoints de `wallet` (sus tablas las cubre la fixture
    # propia de `tests/test_e4_t01_wallet_links.py`); si un test futuro la
    # necesitara, el fallo seria un `OperationalError` ruidoso (jamas
    # silencioso) y habria que re-partir la fixture.
    schemas = {name.split(".")[0] for name in Base.metadata.tables if "." in name}
    schemas.add("config")
    schemas.discard("wallet")

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in sorted(schemas):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[table for table in Base.metadata.tables.values() if table.schema != "wallet"],
    )
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        from app.modules.identity.domain import nonce as nonce_domain
        from app.modules.identity.service import activation as activation_service
        from app.modules.identity.service import kyc_proxy
        from app.modules.identity.service import recovery as recovery_service

        nonce_domain.reset_nonces()
        activation_service.reset_activation_rate_limits()
        kyc_proxy.reset_rate_limits()
        recovery_service.reset_recovery_rate_limits()
        yield session
    finally:
        try:
            from app.modules.identity.domain import nonce as nonce_domain
            from app.modules.identity.service import activation as activation_service
            from app.modules.identity.service import kyc_proxy
            from app.modules.identity.service import recovery as recovery_service

            nonce_domain.reset_nonces()
            activation_service.reset_activation_rate_limits()
            kyc_proxy.reset_rate_limits()
            recovery_service.reset_recovery_rate_limits()
        finally:
            session.close()
            engine.dispose()


@pytest.fixture()
def sec_client(sec_session: Session, monkeypatch):
    """TestClient con `get_db` a SQLite y ventanas de abuso reseteadas."""
    import os

    for var in (
        "KYC_RATE_LIMIT_WINDOW_SECONDS",
        "KYC_RATE_LIMIT_MAX_REQUESTS",
        "RECOVERY_REQUEST_RATE_LIMIT_WINDOW_SECONDS",
        "RECOVERY_REQUEST_RATE_LIMIT_MAX_REQUESTS",
        "RECOVERY_VERIFY_RATE_LIMIT_WINDOW_SECONDS",
        "RECOVERY_VERIFY_RATE_LIMIT_MAX_REQUESTS",
        "ACTIVATION_RESEND_RATE_LIMIT_WINDOW_SECONDS",
        "ACTIVATION_RESEND_RATE_LIMIT_MAX_REQUESTS",
    ):
        monkeypatch.delenv(var, raising=False)
        os.environ.pop(var, None)

    def _override():
        yield sec_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _mk_user(sec_session: Session, email: str, phone: str, status: str = "ACTIVE"):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import pin_login as pin_login_service

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        sec_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ana",
        last_name="Lopez",
        email=email.replace("@", f".{suffix}@"),
        phone=phone,
        status=status,
    )
    identity_repo.create_credential(sec_session, user.id, pin_hash=pin_login_service.hash_pin(PIN))
    sec_session.commit()
    return user


def _mk_account(sec_session: Session, user_id: uuid.UUID, number: str):
    from app.modules.accounts.models import Account, AccountBalance

    acc = Account(
        user_id=user_id, account_number=number, type="AHORRO", currency="PEN", status="ACTIVE"
    )
    sec_session.add(acc)
    sec_session.flush()
    sec_session.add(
        AccountBalance(account_id=acc.id, currency="PEN", available_minor=5_000_000, held_minor=0)
    )
    sec_session.commit()
    return acc


def _mk_product(sec_session: Session, code: str = "QA-SEC"):
    from app.modules.credits.models import LoanProduct

    prod = LoanProduct(
        code=code,
        name="Producto QA",
        tcea=Decimal("0.25"),
        min_amount_minor=1_000,
        max_amount_minor=5_000_000,
        min_term_months=1,
        max_term_months=36,
        enabled=True,
    )
    sec_session.add(prod)
    sec_session.flush()
    sec_session.commit()
    return prod


def _mk_application(sec_session: Session, user_id, product_id, status: str = "SUBMITTED"):
    from app.modules.credits.models import LoanApplication

    row = LoanApplication(
        user_id=user_id,
        product_id=product_id,
        requested_amount_minor=50_000,
        term_months=12,
        status=status,
        bureau_authorized=True,
    )
    sec_session.add(row)
    sec_session.flush()
    sec_session.commit()
    return row


def _seed_admin_params(sec_session: Session):
    rows = [
        ("transfer.biometric_threshold_minor", "100000", "transactions"),
        ("transfer.daily_limit_minor", "500000", "transactions"),
        ("auth.max_failed_attempts", "5", "identity"),
    ]
    for key, value, module in rows:
        sec_session.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module) "
                "VALUES (:id, :key, :value, :module)"
            ),
            {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
        )
    sec_session.commit()


def _make_enrolled_user(sec_session: Session, device_id: str = "pixel-8-pro"):
    from app.modules.identity import repository as identity_repo

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        sec_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"bio.{suffix}@example.com",
        phone="+51999888777",
    )
    credential = identity_repo.create_credential(sec_session, user.id)
    credential.biometric_enabled = True
    sec_session.flush()
    secret_hex = secrets.token_hex(24)
    identity_repo.register_binding(
        sec_session,
        user.id,
        device_id,
        f"hmac:{secret_hex}",
        platform="android",
        biometric_type="FACE",
    )
    sec_session.commit()
    return user, secret_hex


def _key(headers: dict, key: str) -> dict:
    merged = dict(headers)
    merged["Idempotency-Key"] = key
    return merged


# ------------------------------------------------------------------ CA-01 RBAC
def test_rbac_private_endpoints_require_token(sec_client: TestClient):
    """Sin token -> 401 NOT_AUTHENTICATED en todo endpoint privado."""
    ghost = uuid.uuid4()
    probes = [
        ("GET", "/api/v1/me", None),
        ("GET", "/api/v1/accounts", None),
        ("GET", "/api/v1/accounts/totals", None),
        ("GET", f"/api/v1/accounts/{ghost}", None),
        ("GET", f"/api/v1/accounts/{ghost}/movements", None),
        ("GET", f"/api/v1/accounts/{ghost}/movements/export", None),
        ("GET", "/api/v1/beneficiaries", None),
        ("PUT", f"/api/v1/beneficiaries/{ghost}", {"alias": "X"}),
        ("DELETE", f"/api/v1/beneficiaries/{ghost}", None),
        ("POST", "/api/v1/transfers/own", {"amount_minor": 1}),
        ("POST", "/api/v1/transfers/third-party", {"amount_minor": 1}),
        ("POST", "/api/v1/transfers/third-party/resolve-target", {"target_phone": "+51000000000"}),
        ("POST", "/api/v1/transfers/interbank", {"amount_minor": 1}),
        ("GET", "/api/v1/transactions", None),
        ("GET", f"/api/v1/transactions/{ghost}", None),
        ("GET", f"/api/v1/transactions/{ghost}/receipt", None),
        ("POST", f"/api/v1/transactions/{ghost}/reverse", {"reason": "x"}),
        ("POST", f"/api/v1/transactions/{ghost}/authorize", {"pin": PIN}),
        ("POST", f"/api/v1/transactions/{ghost}/authorize/challenge", None),
        ("GET", "/api/v1/credits/products", None),
        ("POST", "/api/v1/credits/simulate", {"amount_minor": 1}),
        ("POST", "/api/v1/credits/applications", {"requested_amount_minor": 1}),
        ("GET", f"/api/v1/credits/applications/{ghost}", None),
        ("GET", "/api/v1/credits/applications", None),
        ("POST", f"/api/v1/credits/applications/{ghost}/review", {"decision": "APPROVED"}),
        ("POST", f"/api/v1/credits/applications/{ghost}/contract", None),
        ("GET", f"/api/v1/credits/applications/{ghost}/contract", None),
        ("POST", f"/api/v1/credits/applications/{ghost}/sign/challenge", None),
        ("POST", f"/api/v1/credits/applications/{ghost}/sign", {"pin": PIN}),
        ("POST", "/api/v1/auth/biometric/consent", {"enabled": True}),
        ("GET", "/api/v1/admin/parameters", None),
        ("PUT", "/api/v1/admin/parameters/transfer.daily_limit_minor", {"value_json": 1}),
    ]
    for method, path, body in probes:
        headers = {"Idempotency-Key": f"qa-noauth-{uuid.uuid4().hex}"}
        if method in ("GET", "DELETE"):
            resp = sec_client.request(method, path, headers=headers)
        else:
            resp = sec_client.request(method, path, json=body, headers=headers)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code} {resp.text[:120]}"
        assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED", f"{method} {path}"


def test_rbac_garbage_token_rejected(sec_client: TestClient, sec_session: Session):
    """Bearer invalido o con `sub` no UUID -> 401 sin tocar la BD."""
    _mk_user(sec_session, "qa.t1@example.com", PHONE_A)
    for headers in (
        {"Authorization": "Bearer basura"},
        {"Authorization": "Token abc"},
        {"Authorization": "Bearer "},
    ):
        resp = sec_client.get("/api/v1/me", headers=headers)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"

    from app.core.security import create_access_token

    bad = create_access_token(subject="not-a-uuid")
    resp = sec_client.get("/api/v1/me", headers={"Authorization": f"Bearer {bad}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_rbac_accounts_isolation(sec_client: TestClient, sec_session: Session):
    """Cuentas: titular 200; otro titular o fantasma -> 403 neutro."""
    owner = _mk_user(sec_session, "qa.acc.a@example.com", PHONE_A)
    other = _mk_user(sec_session, "qa.acc.b@example.com", PHONE_B)
    acc = _mk_account(sec_session, owner.id, ACCNUM_A1)

    ok_resp = sec_client.get(f"/api/v1/accounts/{acc.id}", headers=_bearer(owner.id))
    assert ok_resp.status_code == 200, ok_resp.text
    assert set(ok_resp.json()) == {"data", "meta"}
    assert ACCNUM_A1 not in ok_resp.text, "solo UUIDs/mascaras en la respuesta"

    for uid in (other.id, uuid.uuid4()):
        denied = sec_client.get(f"/api/v1/accounts/{acc.id}", headers=_bearer(uid))
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "NOT_AUTHORIZED"

    mine = sec_client.get("/api/v1/accounts", headers=_bearer(other.id))
    assert mine.status_code == 200, mine.text
    assert all(item["id"] != str(acc.id) for item in mine.json()["data"])

    movements = sec_client.get(f"/api/v1/accounts/{acc.id}/movements", headers=_bearer(other.id))
    assert movements.status_code == 403
    assert movements.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_rbac_transactions_isolation(sec_client: TestClient, sec_session: Session):
    """Transacciones ajenas -> 403 generico; listado solo del titular."""
    owner = _mk_user(sec_session, "qa.tx.a@example.com", PHONE_A)
    other = _mk_user(sec_session, "qa.tx.b@example.com", PHONE_B)
    src = _mk_account(sec_session, owner.id, ACCNUM_A1)
    dst = _mk_account(sec_session, owner.id, ACCNUM_A2)
    body = {
        "source_account_id": str(src.id),
        "target_account_id": str(dst.id),
        "amount_minor": 1_000,
        "currency": "PEN",
    }
    created = sec_client.post(
        "/api/v1/transfers/own", headers=_key(_bearer(owner.id), "qa-tx-own-1"), json=body
    )
    assert created.status_code == 201, created.text
    tx_id = created.json()["data"]["transaction_id"]

    detail = sec_client.get(f"/api/v1/transactions/{tx_id}", headers=_bearer(owner.id))
    assert detail.status_code == 200, detail.text

    denied = sec_client.get(f"/api/v1/transactions/{tx_id}", headers=_bearer(other.id))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "NOT_AUTHORIZED"

    listing = sec_client.get("/api/v1/transactions", headers=_bearer(other.id))
    assert listing.status_code == 200, listing.text
    assert all(item["transaction_id"] != tx_id for item in listing.json()["data"])

    receipt = sec_client.get(f"/api/v1/transactions/{tx_id}/receipt", headers=_bearer(other.id))
    assert receipt.status_code == 403
    assert receipt.json()["error"]["code"] == "NOT_AUTHORIZED"

    reverse = sec_client.post(
        f"/api/v1/transactions/{tx_id}/reverse",
        headers=_key(_bearer(other.id), "qa-tx-rev-ajeno"),
        json={"reason": "intruso"},
    )
    assert reverse.status_code == 403
    assert reverse.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_rbac_authorize_only_holder(sec_client: TestClient, sec_session: Session):
    """Autorizar operacion ajena (incluso con PIN valido del intruso) -> 403."""
    owner = _mk_user(sec_session, "qa.au.a@example.com", PHONE_A)
    intruder = _mk_user(sec_session, "qa.au.b@example.com", PHONE_B)
    src = _mk_account(sec_session, owner.id, ACCNUM_A1)
    dst = _mk_account(sec_session, intruder.id, ACCNUM_B1)
    created = sec_client.post(
        "/api/v1/transfers/third-party",
        headers=_key(_bearer(owner.id), "qa-au-big-1"),
        json={
            "source_account_id": str(src.id),
            "target_account_id": str(dst.id),
            "amount_minor": 200_000,
            "currency": "PEN",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["status"] == "PENDING_AUTHORIZATION"
    tx_id = created.json()["data"]["transaction_id"]

    denied = sec_client.post(
        f"/api/v1/transactions/{tx_id}/authorize",
        headers=_key(_bearer(intruder.id), "qa-au-intruso"),
        json={"pin": PIN},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_rbac_beneficiaries_isolation(sec_client: TestClient, sec_session: Session):
    """Agenda: listado aislado; PUT/DELETE ajeno -> 403; sin cuenta en claro."""
    owner = _mk_user(sec_session, "qa.ben.a@example.com", PHONE_A)
    other = _mk_user(sec_session, "qa.ben.b@example.com", PHONE_B)
    created = sec_client.post(
        "/api/v1/beneficiaries",
        headers=_key(_bearer(owner.id), "qa-ben-1"),
        json={"alias": "Mama", "account_or_cci": CCI_1},
    )
    assert created.status_code == 201, created.text
    bid = created.json()["data"]["id"]
    assert CCI_1 not in created.text, "la cuenta/CCI jamas viaja en claro"

    listing = sec_client.get("/api/v1/beneficiaries", headers=_bearer(other.id))
    assert listing.status_code == 200, listing.text
    assert listing.json()["meta"]["total"] == 0
    assert all(item["id"] != bid for item in listing.json()["data"])

    put = sec_client.put(
        f"/api/v1/beneficiaries/{bid}", headers=_bearer(other.id), json={"alias": "X"}
    )
    assert put.status_code == 403
    assert put.json()["error"]["code"] == "NOT_AUTHORIZED"

    delete = sec_client.delete(
        f"/api/v1/beneficiaries/{bid}", headers=_bearer(other.id), params={"confirm": True}
    )
    assert delete.status_code == 403
    assert delete.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_rbac_credits_isolation(sec_client: TestClient, sec_session: Session):
    """Expediente/contrato/firma ajenos -> 404 neutro; revision exige analista."""
    owner = _mk_user(sec_session, "qa.cre.a@example.com", PHONE_A)
    other = _mk_user(sec_session, "qa.cre.b@example.com", PHONE_B)
    prod = _mk_product(sec_session)
    row = _mk_application(sec_session, owner.id, prod.id, status="APPROVED")

    mine = sec_client.get(f"/api/v1/credits/applications/{row.id}", headers=_bearer(owner.id))
    assert mine.status_code == 200, mine.text

    ghost = sec_client.get(
        f"/api/v1/credits/applications/{uuid.uuid4()}", headers=_bearer(owner.id)
    )
    assert ghost.status_code == 404
    foreign = sec_client.get(f"/api/v1/credits/applications/{row.id}", headers=_bearer(other.id))
    assert foreign.status_code == 404
    assert _sans_request_id(ghost.json()) == _sans_request_id(foreign.json())
    assert foreign.json()["error"]["code"] == "NOT_FOUND"

    contract = sec_client.post(
        f"/api/v1/credits/applications/{row.id}/contract",
        headers=_key(_bearer(other.id), "qa-cre-ctr-ajeno"),
        json={},
    )
    assert contract.status_code == 404
    assert contract.json()["error"]["code"] == "NOT_FOUND"

    challenge = sec_client.post(
        f"/api/v1/credits/applications/{row.id}/sign/challenge", headers=_bearer(other.id)
    )
    assert challenge.status_code == 404
    assert challenge.json()["error"]["code"] == "NOT_FOUND"

    review = sec_client.post(
        f"/api/v1/credits/applications/{row.id}/review",
        headers=_bearer(other.id),
        json={"decision": "APPROVED", "reason": "intruso sin rol"},
    )
    assert review.status_code == 403
    assert review.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_rbac_review_separation_of_duties(sec_client: TestClient, sec_session: Session):
    """Analista feliz dictamina expediente ajeno; el solicitante no el propio."""
    applicant = _mk_user(sec_session, "qa.rev.a@example.com", PHONE_A)
    analyst = _mk_user(sec_session, "qa.rev.c@example.com", PHONE_C)
    prod = _mk_product(sec_session)
    row = _mk_application(sec_session, applicant.id, prod.id, status="IN_REVIEW")

    ok_resp = sec_client.post(
        f"/api/v1/credits/applications/{row.id}/review",
        headers=_bearer(analyst.id, roles=["analyst"]),
        json={"decision": "APPROVED", "reason": "dictamen QA"},
    )
    assert ok_resp.status_code == 200, ok_resp.text
    assert ok_resp.json()["data"]["status"] == "APPROVED"

    row2 = _mk_application(sec_session, applicant.id, prod.id, status="IN_REVIEW")
    selfish = sec_client.post(
        f"/api/v1/credits/applications/{row2.id}/review",
        headers=_bearer(applicant.id, roles=["analyst"]),
        json={"decision": "APPROVED", "reason": "auto-dictamen"},
    )
    assert selfish.status_code == 409, selfish.text

    tray = sec_client.get(
        "/api/v1/credits/applications",
        headers=_bearer(applicant.id),
        params={"status": "IN_REVIEW"},
    )
    assert tray.status_code == 403
    assert tray.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_rbac_admin_parameters(sec_client: TestClient, sec_session: Session):
    """Admin: sin rol -> 403; admin -> 200; PUT valida sin escribir en 422."""
    from app.modules.shared.config import parameters as params

    _seed_admin_params(sec_session)
    admin_id = uuid.uuid4()

    listing = sec_client.get("/api/v1/admin/parameters", headers=_bearer(admin_id, roles=["admin"]))
    assert listing.status_code == 200, listing.text
    assert {"transfer.daily_limit_minor", "auth.max_failed_attempts"} <= {
        item["key"] for item in listing.json()["data"]
    }

    for roles in ([], ["operator"], ["viewer"]):
        denied = sec_client.get("/api/v1/admin/parameters", headers=_bearer(admin_id, roles=roles))
        assert denied.status_code == 403, roles
        assert denied.json()["error"]["code"] == "NOT_AUTHORIZED"

    unknown = sec_client.put(
        "/api/v1/admin/parameters/transfer.no_existe_minor",
        headers=_bearer(admin_id, roles=["admin"]),
        json={"value_json": 1},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "NOT_FOUND"

    bad = sec_client.put(
        "/api/v1/admin/parameters/transfer.daily_limit_minor",
        headers=_bearer(admin_id, roles=["admin"]),
        json={"value_json": -1},
    )
    assert bad.status_code == 422, bad.text
    assert params.get_daily_limit_minor(sec_session) == 500_000, "el 422 no escribe"


def test_rbac_me_and_consent(sec_client: TestClient, sec_session: Session):
    """`/me` auto-alcance; consentimiento 404 neutro sin credencial."""
    user = _mk_user(sec_session, "qa.me@example.com", PHONE_A)
    resp = sec_client.get("/api/v1/me", headers=_bearer(user.id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "first_name": "Ana",
        "last_name": "Lopez",
        "business_name": None,
    }
    for forbidden in ("email", "phone", "doc_number_hash", "status"):
        assert forbidden not in resp.text

    ghost = sec_client.get("/api/v1/me", headers=_bearer(uuid.uuid4()))
    assert ghost.status_code == 404
    assert ghost.json()["error"]["code"] == "NOT_FOUND"

    consent = sec_client.post(
        "/api/v1/auth/biometric/consent", headers=_bearer(user.id), json={"enabled": True}
    )
    assert consent.status_code == 200, consent.text
    assert consent.json()["data"] == {"biometric_enabled": True}

    orphan = sec_client.post(
        "/api/v1/auth/biometric/consent",
        headers=_bearer(uuid.uuid4()),
        json={"enabled": True},
    )
    assert orphan.status_code == 404
    assert orphan.json()["error"]["code"] == "NOT_FOUND"


def test_happy_paths_per_group(sec_client: TestClient, sec_session: Session):
    """Camino feliz representativo por grupo con token valido."""
    user = _mk_user(sec_session, "qa.happy@example.com", PHONE_A)
    headers = _bearer(user.id)
    src = _mk_account(sec_session, user.id, ACCNUM_A1)
    dst = _mk_account(sec_session, user.id, ACCNUM_A2)
    _mk_product(sec_session)

    totals = sec_client.get("/api/v1/accounts/totals", headers=headers)
    assert totals.status_code == 200, totals.text
    assert totals.json()["data"]["primary_currency"] == "PEN"

    products = sec_client.get("/api/v1/credits/products", headers=headers)
    assert products.status_code == 200, products.text
    assert len(products.json()["data"]) >= 1

    simulate = sec_client.post(
        "/api/v1/credits/simulate",
        headers=headers,
        json={"tcea": "0.25", "amount_minor": 100_000, "term_months": 12},
    )
    assert simulate.status_code == 200, simulate.text

    own = sec_client.post(
        "/api/v1/transfers/own",
        headers=_key(headers, "qa-happy-own"),
        json={
            "source_account_id": str(src.id),
            "target_account_id": str(dst.id),
            "amount_minor": 1_000,
            "currency": "PEN",
        },
    )
    assert own.status_code == 201, own.text
    assert own.json()["data"]["status"] == "SETTLED"
    tx_id = own.json()["data"]["transaction_id"]

    receipt = sec_client.get(f"/api/v1/transactions/{tx_id}/receipt", headers=headers)
    assert receipt.status_code == 200, receipt.text
    assert receipt.headers["content-type"] == "application/pdf"


# ---------------------------------------------------------- CA-02 anti-enumeracion
def test_anti_enum_login_identical(sec_client: TestClient, sec_session: Session):
    """PIN erroneo == usuario inexistente == malformado (mismo cuerpo)."""
    user = _mk_user(sec_session, "qa.enum@example.com", PHONE_A)
    headers = {"X-Request-Id": "qa-enum"}
    wrong = sec_client.post(
        "/api/v1/auth/login/pin",
        json={"user_ref": str(user.id), "pin": WRONG_PIN},
        headers=headers,
    )
    unknown = sec_client.post(
        "/api/v1/auth/login/pin",
        json={"user_ref": str(uuid.uuid4()), "pin": WRONG_PIN},
        headers=headers,
    )
    malformed = sec_client.post(
        "/api/v1/auth/login/pin",
        json={"user_ref": "no-es-uuid", "pin": WRONG_PIN},
        headers=headers,
    )
    assert wrong.status_code == 401
    assert wrong.json() == unknown.json() == malformed.json()
    assert wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert PIN not in wrong.text


def test_anti_enum_recovery_identical(sec_client: TestClient, sec_session: Session):
    """Recovery siempre 200 con el mismo `data` exista o no el email."""
    from app.modules.identity.service import recovery as recovery_service

    _mk_user(sec_session, "qa.rec@example.com", PHONE_A)
    recovery_service.reset_recovery_rate_limits()
    registered = sec_client.post(
        "/api/v1/auth/recovery/request", json={"email": "qa.rec@example.com"}
    )
    missing = sec_client.post("/api/v1/auth/recovery/request", json={"email": "nadie@example.com"})
    assert registered.status_code == 200 and missing.status_code == 200
    assert registered.json()["data"] == missing.json()["data"]
    assert "qa.rec@example.com" not in missing.text
    recovery_service.reset_recovery_rate_limits()


def test_anti_enum_device_request_identical(sec_client: TestClient, sec_session: Session):
    """Login en dispositivo nuevo: 200 identico exista o no la cuenta."""
    from app.modules.identity.service import recovery as recovery_service

    _mk_user(sec_session, "qa.dev@example.com", PHONE_A)
    recovery_service.reset_recovery_rate_limits()
    payload = {"email": "qa.dev@example.com", "document_number": "00000001"}
    ghost = {"email": "fantasma@example.com", "document_number": "00000001"}
    first = sec_client.post("/api/v1/auth/login/device/request", json=payload)
    second = sec_client.post("/api/v1/auth/login/device/request", json=ghost)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    recovery_service.reset_recovery_rate_limits()


def test_anti_enum_pin_reset_identical(sec_client: TestClient, sec_session: Session):
    """Pin-reset: documento/codigo/email invalidos -> mismo 401 generico."""
    from app.modules.identity.service import recovery as recovery_service

    user = _mk_user(sec_session, "qa.pr@example.com", PHONE_A)
    recovery_service.reset_recovery_rate_limits()
    variants = [
        {"email": user.email, "doc_number": "00000001", "code": "999999", "pin": "1234"},
        {"email": "nadie@example.com", "doc_number": "00000001", "code": "999999", "pin": "1234"},
        {"email": user.email, "doc_number": "87654321", "code": "999999", "pin": "1234"},
    ]
    bodies = [sec_client.post("/api/v1/auth/pin-reset", json=payload) for payload in variants]
    for resp in bodies:
        assert resp.status_code == 401, resp.text
    assert bodies[0].json() == bodies[1].json() == bodies[2].json()
    assert bodies[0].json()["error"]["code"] == "INVALID_PIN_RESET"
    recovery_service.reset_recovery_rate_limits()


def test_anti_enum_resolve_target_identical(sec_client: TestClient, sec_session: Session):
    """Resolve-target: sin cuentas ACTIVE == telefono inexistente (404 neutro)."""
    owner = _mk_user(sec_session, "qa.rt.a@example.com", PHONE_A)
    _mk_user(sec_session, "qa.rt.c@example.com", PHONE_C)
    _mk_account(sec_session, owner.id, ACCNUM_A1)
    headers = _bearer(owner.id)

    no_accounts = sec_client.post(
        "/api/v1/transfers/third-party/resolve-target",
        headers=headers,
        json={"target_phone": PHONE_C},
    )
    unknown = sec_client.post(
        "/api/v1/transfers/third-party/resolve-target",
        headers=headers,
        json={"target_phone": "+51000000000"},
    )
    assert no_accounts.status_code == 404 and unknown.status_code == 404
    assert _sans_request_id(no_accounts.json()) == _sans_request_id(unknown.json())
    assert no_accounts.json()["error"]["code"] == "TARGET_NOT_FOUND"
    assert PHONE_C not in no_accounts.text, "sin PII en el 404 neutro"


def test_anti_enum_document_lookup(sec_client: TestClient, sec_session: Session):
    """Lookup: dos numeros distintos -> indistinguibles; malformado -> 422."""
    from app.modules.identity.service import kyc_proxy

    kyc_proxy.reset_rate_limits()
    first = sec_client.post(
        "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": "12345678"}
    )
    second = sec_client.post(
        "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": "87654321"}
    )
    assert first.status_code == 200 and second.status_code == 200
    assert _sans_request_id(first.json()) == _sans_request_id(second.json())

    malformed = sec_client.post(
        "/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": "abc"}
    )
    assert malformed.status_code == 422
    assert "abc" not in malformed.text, "el numero nunca se refleja"
    kyc_proxy.reset_rate_limits()


def test_anti_enum_transaction_id_shape(sec_client: TestClient, sec_session: Session):
    """Detalle: id malformado == inexistente (404 neutro)."""
    user = _mk_user(sec_session, "qa.tid@example.com", PHONE_A)
    headers = _bearer(user.id)
    ghost = sec_client.get(f"/api/v1/transactions/{uuid.uuid4()}", headers=headers)
    malformed = sec_client.get("/api/v1/transactions/no-es-uuid", headers=headers)
    assert ghost.status_code == 404 and malformed.status_code == 404
    assert _sans_request_id(ghost.json()) == _sans_request_id(malformed.json())
    assert ghost.json()["error"]["code"] == "NOT_FOUND"


# ----------------------------------------------------- CA-03 idempotencia maliciosa
def test_idempotency_own_single_effect(sec_client: TestClient, sec_session: Session):
    """Propia: replay -> mismo id; cuerpo distinto -> 409; un solo debito."""
    from app.modules.accounts.models import AccountBalance

    user = _mk_user(sec_session, "qa.idem.a@example.com", PHONE_A)
    src = _mk_account(sec_session, user.id, ACCNUM_A1)
    dst = _mk_account(sec_session, user.id, ACCNUM_A2)
    headers = _bearer(user.id)
    body = {
        "source_account_id": str(src.id),
        "target_account_id": str(dst.id),
        "amount_minor": 1_000,
        "currency": "PEN",
    }
    first = sec_client.post(
        "/api/v1/transfers/own", headers=_key(headers, "qa-idem-own"), json=body
    )
    assert first.status_code == 201, first.text
    tx_id = first.json()["data"]["transaction_id"]

    replay = sec_client.post(
        "/api/v1/transfers/own", headers=_key(headers, "qa-idem-own"), json=body
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["data"]["transaction_id"] == tx_id
    assert replay.json()["meta"]["replayed"] is True

    conflict = sec_client.post(
        "/api/v1/transfers/own",
        headers=_key(headers, "qa-idem-own"),
        json={**body, "amount_minor": 2_000},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] in {"CONFLICT", "IDEMPOTENCY_CONFLICT"}

    sec_session.expire_all()
    balance = sec_session.scalar(
        sa.select(AccountBalance).where(AccountBalance.account_id == src.id)
    )
    assert balance is not None
    assert balance.available_minor == 5_000_000 - 1_000, "un solo debito pese al replay"
    count = sec_session.scalar(
        sa.select(sa.func.count())
        .select_from(Base.metadata.tables["transactions.transactions"])
        .where(Base.metadata.tables["transactions.transactions"].c.initiator_user_id == user.id)
    )
    assert count == 1


def test_idempotency_third_party_conflict(sec_client: TestClient, sec_session: Session):
    """Terceros: misma clave + monto distinto -> 409 sin segunda operacion."""
    from app.modules.transactions.models import Transaction

    owner = _mk_user(sec_session, "qa.idem.b@example.com", PHONE_A)
    other = _mk_user(sec_session, "qa.idem.c@example.com", PHONE_B)
    src = _mk_account(sec_session, owner.id, ACCNUM_A1)
    dst = _mk_account(sec_session, other.id, ACCNUM_B1)
    headers = _bearer(owner.id)
    body = {
        "source_account_id": str(src.id),
        "target_account_id": str(dst.id),
        "amount_minor": 500,
        "currency": "PEN",
    }
    first = sec_client.post(
        "/api/v1/transfers/third-party", headers=_key(headers, "qa-idem-tp"), json=body
    )
    assert first.status_code == 201, first.text
    assert first.json()["data"]["status"] == "SETTLED"

    conflict = sec_client.post(
        "/api/v1/transfers/third-party",
        headers=_key(headers, "qa-idem-tp"),
        json={**body, "amount_minor": 600},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] in {"CONFLICT", "IDEMPOTENCY_CONFLICT"}

    sec_session.expire_all()
    rows = sec_session.scalars(
        sa.select(Transaction).where(Transaction.initiator_user_id == owner.id)
    ).all()
    assert len(rows) == 1, "sin doble operacion"


def test_idempotency_beneficiary_single_row(sec_client: TestClient, sec_session: Session):
    """Agenda: replay -> misma fila; cuerpo distinto -> 409 sin duplicar."""
    from app.modules.accounts.models import Beneficiary

    owner = _mk_user(sec_session, "qa.idem.d@example.com", PHONE_A)
    headers = _bearer(owner.id)
    body = {"alias": "Mama", "account_or_cci": CCI_1}
    first = sec_client.post(
        "/api/v1/beneficiaries", headers=_key(headers, "qa-idem-ben"), json=body
    )
    assert first.status_code == 201, first.text
    bid = first.json()["data"]["id"]

    replay = sec_client.post(
        "/api/v1/beneficiaries", headers=_key(headers, "qa-idem-ben"), json=body
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["data"]["id"] == bid
    assert replay.json()["meta"]["replayed"] is True

    conflict = sec_client.post(
        "/api/v1/beneficiaries",
        headers=_key(headers, "qa-idem-ben"),
        json={"alias": "Otro", "account_or_cci": CCI_2},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    sec_session.expire_all()
    count = sec_session.scalar(
        sa.select(sa.func.count())
        .select_from(Beneficiary)
        .where(Beneficiary.owner_user_id == owner.id)
    )
    assert count == 1


def test_idempotency_credit_application(sec_client: TestClient, sec_session: Session):
    """Expediente: replay -> mismo id; cuerpo distinto -> 409 sin duplicar."""
    from app.modules.credits.models import LoanApplication

    user = _mk_user(sec_session, "qa.idem.e@example.com", PHONE_A)
    prod = _mk_product(sec_session)
    headers = _bearer(user.id)
    body = {
        "product_id": str(prod.id),
        "requested_amount_minor": 100_000,
        "term_months": 12,
        "total_income_minor": 500_000,
        "total_debt_minor": 0,
        "bureau_authorized": True,
    }
    first = sec_client.post(
        "/api/v1/credits/applications", headers=_key(headers, "qa-idem-app"), json=body
    )
    assert first.status_code in (201, 202), first.text
    app_id = first.json()["data"]["id"]

    replay = sec_client.post(
        "/api/v1/credits/applications", headers=_key(headers, "qa-idem-app"), json=body
    )
    assert replay.status_code == first.status_code
    assert replay.json()["data"]["id"] == app_id
    assert replay.json()["meta"]["replayed"] is True

    conflict = sec_client.post(
        "/api/v1/credits/applications",
        headers=_key(headers, "qa-idem-app"),
        json={**body, "requested_amount_minor": 200_000},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    sec_session.expire_all()
    count = sec_session.scalar(
        sa.select(sa.func.count())
        .select_from(LoanApplication)
        .where(LoanApplication.user_id == user.id)
    )
    assert count == 1


def test_idempotency_reverse_single_entry(sec_client: TestClient, sec_session: Session):
    """Reverso: replay -> mismo asiento inverso; motivo distinto -> 409."""
    user = _mk_user(sec_session, "qa.idem.f@example.com", PHONE_A)
    src = _mk_account(sec_session, user.id, ACCNUM_A1)
    dst = _mk_account(sec_session, user.id, ACCNUM_A2)
    headers = _bearer(user.id)
    created = sec_client.post(
        "/api/v1/transfers/own",
        headers=_key(headers, "qa-idem-rev-src"),
        json={
            "source_account_id": str(src.id),
            "target_account_id": str(dst.id),
            "amount_minor": 1_000,
            "currency": "PEN",
        },
    )
    assert created.status_code == 201, created.text
    tx_id = created.json()["data"]["transaction_id"]

    first = sec_client.post(
        f"/api/v1/transactions/{tx_id}/reverse",
        headers=_key(headers, "qa-idem-rev"),
        json={"reason": "qa"},
    )
    assert first.status_code == 200, first.text
    entry_id = first.json()["data"]["reversal_entry_id"]

    replay = sec_client.post(
        f"/api/v1/transactions/{tx_id}/reverse",
        headers=_key(headers, "qa-idem-rev"),
        json={"reason": "qa"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["reversal_entry_id"] == entry_id

    conflict = sec_client.post(
        f"/api/v1/transactions/{tx_id}/reverse",
        headers=_key(headers, "qa-idem-rev"),
        json={"reason": "otro motivo"},
    )
    assert conflict.status_code == 409


def test_nonce_reuse_facial_and_expiry(sec_client: TestClient, sec_session: Session):
    """Nonce facial de un solo uso; vencido -> EXPIRED_NONCE."""
    from app.modules.identity.domain import nonce as nonce_domain

    user, secret_hex = _make_enrolled_user(sec_session)
    challenge = sec_client.post(
        "/api/v1/auth/login/challenge",
        json={"user_ref": str(user.id), "device_id": "pixel-8-pro"},
    )
    assert challenge.status_code == 200, challenge.text
    payload = {
        "nonce": challenge.json()["data"]["nonce"],
        "device_id": "pixel-8-pro",
        "signature": _sign_hmac(secret_hex, challenge.json()["data"]["nonce"]),
    }
    first = sec_client.post("/api/v1/auth/login/facial", json=payload)
    assert first.status_code == 200, first.text

    reuse = sec_client.post("/api/v1/auth/login/facial", json=payload)
    assert reuse.status_code == 400
    assert reuse.json()["error"]["code"] == "INVALID_LOGIN"

    user2, secret2 = _make_enrolled_user(sec_session)
    challenge2 = sec_client.post(
        "/api/v1/auth/login/challenge",
        json={"user_ref": str(user2.id), "device_id": "pixel-8-pro"},
    )
    nonce2 = challenge2.json()["data"]["nonce"]
    nonce_domain._NONCES[nonce2].expires_at = _utcnow() - timedelta(seconds=1)
    expired = sec_client.post(
        "/api/v1/auth/login/facial",
        json={
            "nonce": nonce2,
            "device_id": "pixel-8-pro",
            "signature": _sign_hmac(secret2, nonce2),
        },
    )
    assert expired.status_code == 400
    assert expired.json()["error"]["code"] == "EXPIRED_NONCE"


def test_sign_nonce_reuse_rejected(sec_client: TestClient, sec_session: Session):
    """Firma de contrato: reuso del reto tras firmar -> 409 sin doble firma."""
    from app.modules.credits.models import Contract

    user = _mk_user(sec_session, "qa.idem.g@example.com", PHONE_A)
    prod = _mk_product(sec_session)
    row = _mk_application(sec_session, user.id, prod.id, status="APPROVED")
    headers = _bearer(user.id)
    generated = sec_client.post(
        f"/api/v1/credits/applications/{row.id}/contract",
        headers=_key(headers, "qa-idem-sign-ctr"),
        json={},
    )
    assert generated.status_code == 201, generated.text

    challenge = sec_client.post(
        f"/api/v1/credits/applications/{row.id}/sign/challenge", headers=headers
    )
    assert challenge.status_code == 200, challenge.text
    nonce = challenge.json()["data"]["nonce"]

    signed = sec_client.post(
        f"/api/v1/credits/applications/{row.id}/sign",
        headers=_key(headers, "qa-idem-sign"),
        json={"nonce": nonce, "pin": PIN},
    )
    assert signed.status_code == 200, signed.text
    assert PIN not in signed.text

    reuse = sec_client.post(
        f"/api/v1/credits/applications/{row.id}/sign",
        headers=_key(headers, "qa-idem-sign-2"),
        json={"nonce": nonce, "pin": PIN},
    )
    assert reuse.status_code == 409, reuse.text

    sec_session.expire_all()
    count = sec_session.scalar(
        sa.select(sa.func.count()).select_from(Contract).where(Contract.status == "SIGNED")
    )
    assert count == 3, "las tres piezas se firman una sola vez"


# ------------------------------------------------------- CA-04 rate-limit/abuso
def test_bruteforce_lockout(sec_client: TestClient, sec_session: Session):
    """Login: 5 fallos -> 423 ACCOUNT_LOCKED; el bloqueo gana al PIN correcto."""
    user = _mk_user(sec_session, "qa.lock@example.com", PHONE_A)
    for _ in range(4):
        resp = sec_client.post(
            "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": WRONG_PIN}
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
    fifth = sec_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": WRONG_PIN}
    )
    assert fifth.status_code == 423, fifth.text
    assert fifth.json()["error"]["code"] == "ACCOUNT_LOCKED"
    assert "failed_attempts" not in fifth.text
    assert "locked_until" not in fifth.text

    locked = sec_client.post("/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": PIN})
    assert locked.status_code == 423


def test_rate_limit_recovery_request(sec_client: TestClient, sec_session: Session):
    """Recovery: 10 ok + 429 RATE_LIMITED tras el umbral (ventana email+IP)."""
    from app.modules.identity.service import recovery as recovery_service

    _mk_user(sec_session, "qa.rl.rec@example.com", PHONE_A)
    recovery_service.reset_recovery_rate_limits()
    codes = [
        sec_client.post(
            "/api/v1/auth/recovery/request", json={"email": "qa.rl.rec@example.com"}
        ).status_code
        for _ in range(12)
    ]
    assert codes[:10] == [200] * 10
    assert codes[10] == 429 and codes[11] == 429
    last = sec_client.post("/api/v1/auth/recovery/request", json={"email": "qa.rl.rec@example.com"})
    assert last.json()["error"]["code"] == "RATE_LIMITED"
    recovery_service.reset_recovery_rate_limits()


def test_rate_limit_pin_reset(sec_client: TestClient, sec_session: Session):
    """Pin-reset: 10 intentos 401 + 429 RATE_LIMITED (ventana antes de existir)."""
    from app.modules.identity.service import recovery as recovery_service

    recovery_service.reset_recovery_rate_limits()
    payload = {
        "email": "nadie@example.com",
        "doc_number": "00000001",
        "code": "111111",
        "pin": "1234",
    }
    codes = [sec_client.post("/api/v1/auth/pin-reset", json=payload).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429 and codes[11] == 429
    recovery_service.reset_recovery_rate_limits()


def test_rate_limit_kyc_window(sec_client: TestClient, sec_session: Session):
    """KYC: 30 ok + 429 RATE_LIMITED tras el umbral (ventana en memoria)."""
    from app.modules.identity.service import kyc_proxy

    kyc_proxy.reset_rate_limits()
    codes = [
        sec_client.post(
            "/api/v1/auth/kyc/email/check", json={"email": f"nuevo{i}@example.com"}
        ).status_code
        for i in range(32)
    ]
    assert codes[:30] == [200] * 30, codes
    assert codes[30] == 429 and codes[31] == 429
    kyc_proxy.reset_rate_limits()


def test_rate_limit_otp_resend(sec_client: TestClient, sec_session: Session, monkeypatch):
    """OTP: 3 reenvios ok + 429 RESEND_LIMIT (maximo de reenvios)."""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import activation as activation_service
    from app.modules.identity.service import otp_service
    from app.modules.identity.service import pin_login as pin_login_service

    monkeypatch.setattr(otp_service, "OTP_RESEND_WAIT_SECONDS", 0)
    activation_service.reset_activation_rate_limits()
    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        sec_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Pen",
        last_name="Diente",
        email=f"qa.rl.otp.{suffix}@example.com",
        phone="+51111111111",
        status="PENDING_ACTIVATION",
    )
    identity_repo.create_credential(
        sec_session, user.id, pin_hash=pin_login_service.hash_pin("1234")
    )
    otp_service.generate_otp(sec_session, user_id=user.id, purpose="ACTIVATION")
    sec_session.commit()

    for _ in range(otp_service.OTP_MAX_RESENDS):
        ok_resp = sec_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)})
        assert ok_resp.status_code == 200, ok_resp.text
    limited = sec_client.post("/api/v1/auth/otp/resend", json={"user_ref": str(user.id)})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RESEND_LIMIT"
    activation_service.reset_activation_rate_limits()


def test_authorize_preventive_block(sec_client: TestClient, sec_session: Session):
    """Authorize: 4 fallos 403 identicos + 5to bloquea (sin oraculo, no 429)."""
    owner = _mk_user(sec_session, "qa.blk.a@example.com", PHONE_A)
    other = _mk_user(sec_session, "qa.blk.b@example.com", PHONE_B)
    src = _mk_account(sec_session, owner.id, ACCNUM_A1)
    dst = _mk_account(sec_session, other.id, ACCNUM_B1)
    headers = _bearer(owner.id)
    created = sec_client.post(
        "/api/v1/transfers/third-party",
        headers=_key(headers, "qa-blk-big"),
        json={
            "source_account_id": str(src.id),
            "target_account_id": str(dst.id),
            "amount_minor": 200_000,
            "currency": "PEN",
        },
    )
    assert created.status_code == 201, created.text
    tx_id = created.json()["data"]["transaction_id"]

    failures = [
        sec_client.post(
            f"/api/v1/transactions/{tx_id}/authorize",
            headers=_key(headers, f"qa-blk-w{i}"),
            json={"pin": WRONG_PIN},
        )
        for i in range(4)
    ]
    for resp in failures:
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
    assert _sans_request_id(failures[0].json()) == _sans_request_id(failures[3].json())
    assert PIN not in failures[0].text and WRONG_PIN not in failures[0].text

    blocked = sec_client.post(
        f"/api/v1/transactions/{tx_id}/authorize",
        headers=_key(headers, "qa-blk-w4"),
        json={"pin": WRONG_PIN},
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["data"]["status"] == "REJECTED"
    assert blocked.json()["data"]["failure_code"] == "BIOMETRIC_PREVENTIVE_BLOCK"

    terminal = sec_client.post(
        f"/api/v1/transactions/{tx_id}/authorize",
        headers=_key(headers, "qa-blk-w5"),
        json={"pin": PIN},
    )
    assert terminal.status_code == 409, terminal.text


def test_authorize_pin_happy_reuses_hold(sec_client: TestClient, sec_session: Session):
    """Authorize con PIN valido liquida sin duplicar (replay identico)."""
    owner = _mk_user(sec_session, "qa.auth.a@example.com", PHONE_A)
    other = _mk_user(sec_session, "qa.auth.b@example.com", PHONE_B)
    src = _mk_account(sec_session, owner.id, ACCNUM_A1)
    dst = _mk_account(sec_session, other.id, ACCNUM_B1)
    headers = _bearer(owner.id)
    created = sec_client.post(
        "/api/v1/transfers/third-party",
        headers=_key(headers, "qa-auth-big"),
        json={
            "source_account_id": str(src.id),
            "target_account_id": str(dst.id),
            "amount_minor": 200_000,
            "currency": "PEN",
        },
    )
    assert created.json()["data"]["status"] == "PENDING_AUTHORIZATION"
    tx_id = created.json()["data"]["transaction_id"]

    ok_resp = sec_client.post(
        f"/api/v1/transactions/{tx_id}/authorize",
        headers=_key(headers, "qa-auth-ok"),
        json={"pin": PIN},
    )
    assert ok_resp.status_code == 200, ok_resp.text
    assert ok_resp.json()["data"]["status"] == "SETTLED"
    assert PIN not in ok_resp.text

    replay = sec_client.post(
        f"/api/v1/transactions/{tx_id}/authorize",
        headers=_key(headers, "qa-auth-ok"),
        json={"pin": PIN},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["status"] == "SETTLED"


# ------------------------------------------------------------------ CA-05 inyeccion
def test_injection_never_500_nor_echo(sec_client: TestClient, sec_session: Session):
    """Payloads hostiles: nunca 500/trazas y sin eco del valor inyectado."""
    user = _mk_user(sec_session, "qa.inj@example.com", PHONE_A)
    headers = _key(_bearer(user.id), "qa-inj")
    hostile = [
        "' OR 1=1 --",
        "'; DROP TABLE users; --",
        "${7*7}",
        "{{7*7}}",
        "../../../etc/passwd",
        "A" * 5000,
        "1 OR 1=1",
        "' UNION SELECT * FROM identity.users --",
    ]
    targets = []
    for value in hostile:
        targets.append(("POST", "/api/v1/auth/login/pin", {"user_ref": value, "pin": value}, None))
        targets.append(("POST", "/api/v1/auth/recovery/request", {"email": value}, None))
        targets.append(
            (
                "POST",
                "/api/v1/transfers/third-party",
                {
                    "source_account_id": value,
                    "target_account_id": value,
                    "amount_minor": 1,
                    "currency": "PEN",
                },
                headers,
            )
        )
    for method, path, body, hdrs in targets:
        resp = sec_client.request(method, path, json=body, headers=hdrs or {})
        assert resp.status_code != 500, f"{method} {path} -> 500 con {body}"
        lowered = resp.text.lower()
        assert "traceback" not in lowered, f"traza en {method} {path}"
        assert "sqlalchemy" not in lowered and "psycopg" not in lowered
    probe = sec_client.post(
        "/api/v1/auth/login/pin", json={"user_ref": "' OR 1=1 --", "pin": "' OR 1=1 --"}
    )
    assert probe.status_code == 401
    assert "' OR 1=1 --" not in probe.text


def test_injection_headers_and_types(sec_client: TestClient, sec_session: Session):
    """Cabeceras hostiles y tipos invalidos: rechazo 4xx, nunca 500."""
    user = _mk_user(sec_session, "qa.inj2@example.com", PHONE_A)
    headers = _bearer(user.id)

    evil_headers = dict(headers)
    evil_headers["X-Forwarded-For"] = "1.2.3.4', evil"
    evil_headers["X-Forwarded-Host"] = "' OR 1=1 --"
    resp = sec_client.post(
        "/api/v1/auth/login/pin",
        json={"user_ref": str(user.id), "pin": PIN},
        headers=evil_headers,
    )
    assert resp.status_code in (200, 401, 422), resp.text

    bad_types = sec_client.post(
        "/api/v1/transfers/third-party",
        headers=_key(headers, "qa-inj-types"),
        json={
            "source_account_id": str(uuid.uuid4()),
            "target_account_id": str(uuid.uuid4()),
            "amount_minor": "1 OR 1=1",
            "currency": "' OR 1=1 --",
        },
    )
    assert bad_types.status_code in (400, 422), bad_types.text
    assert "1 OR 1=1" not in bad_types.text

    negative = sec_client.post(
        "/api/v1/transfers/third-party",
        headers=_key(headers, "qa-inj-neg"),
        json={
            "source_account_id": str(uuid.uuid4()),
            "target_account_id": str(uuid.uuid4()),
            "amount_minor": -5,
            "currency": "PEN",
        },
    )
    assert negative.status_code in (400, 422), negative.text

    huge = sec_client.get("/api/v1/transactions", headers=headers, params={"page_size": 9999999})
    assert huge.status_code in (200, 422), huge.text

    search = sec_client.get("/api/v1/beneficiaries", headers=headers, params={"q": "' OR 1=1 --"})
    assert search.status_code == 200, search.text

    raw = sec_client.post(
        "/api/v1/transfers/third-party",
        content="{no es json",
        headers={**_key(headers, "qa-inj-raw"), "Content-Type": "application/json"},
    )
    assert raw.status_code != 500, raw.text

    proto = sec_client.post(
        "/api/v1/transfers/third-party",
        headers=_key(headers, "qa-inj-proto"),
        json={
            "__proto__": {"polluted": True},
            "source_account_id": str(uuid.uuid4()),
            "target_account_id": str(uuid.uuid4()),
            "amount_minor": 1,
            "currency": "PEN",
        },
    )
    assert proto.status_code == 422, proto.text


# ------------------------------------------------------------------ CA-06 higiene
def test_hygiene_secrets_out_of_logs_and_bodies(
    sec_client: TestClient, sec_session: Session, caplog
):
    """PIN/firma/nonce/documento/cuenta jamas en logs ni respuestas."""
    import app.modules.identity.repository as identity_repo

    owner = _mk_user(sec_session, "qa.hyg.a@example.com", PHONE_A)
    other = _mk_user(sec_session, "qa.hyg.b@example.com", PHONE_B)
    src = _mk_account(sec_session, owner.id, ACCNUM_A1)
    dst = _mk_account(sec_session, other.id, ACCNUM_B1)
    headers = _bearer(owner.id)
    fake_signature = "ab" * 32

    responses = []
    with caplog.at_level(logging.DEBUG):
        responses.append(
            sec_client.post(
                "/api/v1/auth/login/pin",
                json={"user_ref": str(owner.id), "pin": WRONG_PIN},
            )
        )
        created = sec_client.post(
            "/api/v1/transfers/third-party",
            headers=_key(headers, "qa-hyg-tp"),
            json={
                "source_account_id": str(src.id),
                "target_account_id": str(dst.id),
                "amount_minor": 500,
                "currency": "PEN",
            },
        )
        responses.append(created)
        responses.append(
            sec_client.post(
                "/api/v1/transfers/third-party/resolve-target",
                headers=headers,
                json={"target_phone": PHONE_B},
            )
        )
        responses.append(
            sec_client.post("/api/v1/auth/recovery/request", json={"email": owner.email})
        )
        tx_id = created.json()["data"]["transaction_id"]
        responses.append(sec_client.get(f"/api/v1/transactions/{tx_id}", headers=headers))
        responses.append(
            sec_client.post(
                f"/api/v1/transactions/{tx_id}/authorize",
                headers=_key(headers, "qa-hyg-auth"),
                json={"pin": WRONG_PIN},
            )
        )
        responses.append(
            sec_client.post(
                "/api/v1/auth/login/facial",
                json={
                    "nonce": "00" * 16,
                    "device_id": "pixel-8-pro",
                    "signature": fake_signature,
                },
            )
        )

    blob = caplog.text
    for sentinel in (
        PIN,
        WRONG_PIN,
        fake_signature,
        ACCNUM_A1,
        ACCNUM_A2,
        ACCNUM_B1,
        CCI_1,
        PHONE_A,
        PHONE_B,
        owner.email,
    ):
        assert sentinel not in blob, f"secreto/PII en logs: {sentinel[:12]}..."
    for resp in responses:
        assert resp.status_code != 500
    bodies = "\n".join(resp.text for resp in responses)
    for sentinel in (PIN, WRONG_PIN, fake_signature, ACCNUM_A1, ACCNUM_B1, CCI_1):
        assert sentinel not in bodies, f"secreto en respuesta: {sentinel[:12]}..."

    sec_session.expire_all()
    import json as _json

    from app.modules.audit.models import AuditLog

    dump = "\n".join(
        _json.dumps(
            {"action": r.action, "after": r.after_json, "before": r.before_json},
            sort_keys=True,
            default=str,
        )
        for r in sec_session.scalars(sa.select(AuditLog)).all()
    )
    assert PIN not in dump and WRONG_PIN not in dump
    assert "pbkdf2" not in dump
    assert identity_repo is not None


def test_hygiene_refresh_never_in_logs(sec_client: TestClient, sec_session: Session, caplog):
    """El refresh en claro jamas va a logs; en BD solo su hash."""
    import hashlib as _hashlib

    from app.modules.identity import repository as identity_repo

    user = _mk_user(sec_session, "qa.hyg.r@example.com", PHONE_A)
    refresh = secrets.token_urlsafe(32)
    identity_repo.create_session(
        sec_session,
        user.id,
        identity_repo.hash_refresh_token(refresh),
        _utcnow() + timedelta(seconds=3600),
        device_id="pixel-8-pro",
    )
    sec_session.commit()

    with caplog.at_level(logging.DEBUG):
        resp = sec_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200, resp.text
    assert refresh not in caplog.text

    sec_session.expire_all()
    row = identity_repo.get_session_by_refresh_hash(
        sec_session, _hashlib.sha256(refresh.encode()).hexdigest()
    )
    assert row is not None
    assert row.refresh_token_hash != refresh


def test_validation_422_sanitized(sec_client: TestClient, sec_session: Session, caplog):
    """El 422 expone solo loc/msg/type (sin input/ctx) y el log sin valores."""
    user = _mk_user(sec_session, "qa.v22@example.com", PHONE_A)
    sentinel_pin = "719283"
    tx_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING):
        resp = sec_client.post(
            f"/api/v1/transactions/{tx_id}/authorize",
            headers=_key(_bearer(user.id), "qa-v22"),
            json={"device_id": "dev-1", "signature": "ab" * 32, "pin": sentinel_pin},
        )
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert "detail" in payload and "error" not in payload
    assert sentinel_pin not in resp.text
    for item in payload["detail"]:
        assert "input" not in item and "ctx" not in item
        assert set(item.keys()) <= {"loc", "msg", "type"}
    assert sentinel_pin not in caplog.text


def test_error_catalog_uses_known_codes(sec_client: TestClient, sec_session: Session):
    """Los `error.code` observados pertenecen al catalogo (sin codigos nuevos)."""
    user = _mk_user(sec_session, "qa.cat@example.com", PHONE_A)
    ghost = uuid.uuid4()
    probes = [
        sec_client.get("/api/v1/me"),
        sec_client.get(f"/api/v1/accounts/{ghost}", headers=_bearer(user.id)),
        sec_client.post("/api/v1/auth/login/pin", json={"user_ref": str(ghost), "pin": WRONG_PIN}),
        sec_client.post(
            "/api/v1/transfers/third-party/resolve-target",
            headers=_bearer(user.id),
            json={"target_phone": "+51000000000"},
        ),
        sec_client.post("/api/v1/auth/kyc/document/lookup", json={"type": "DNI", "number": "x"}),
    ]
    seen = set()
    for resp in probes:
        assert resp.status_code != 500, resp.text
        body = resp.json()
        if "error" in body:
            seen.add(body["error"]["code"])
    assert seen, "la sonda debe observar codigos de error"
    assert seen <= ALLOWED_ERROR_CODES, f"codigos fuera de catalogo: {seen - ALLOWED_ERROR_CODES}"
