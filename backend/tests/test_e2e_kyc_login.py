"""E2E del alta y login (Q-T10; HU01/HU02/HU03).

Flujo completo con adaptadores falsos (cero red real):

1. `POST /auth/kyc/submit` (proveedor KYC falso por `dependency_overrides`)
   crea `User` (PENDING_ACTIVATION) + `Credential` + `Account` +
   `KycVerification` + OTP `ACTIVATION`, y emite el OTP por **email** con un
   sender de notificaciones falso que captura el codigo en claro.
2. `POST /auth/pin/setup` con ese codigo activa la cuenta y fija el PIN con
   un solo OTP.
3. `POST /auth/login/pin` con `device_public_key` ("hmac:<hex>") abre sesion y
   registra `device_binding` (sin duplicar en un segundo login).
4. `POST /auth/login/challenge` + firma HMAC del `nonce` -> `POST
   /auth/login/facial` completa el segundo factor.

Errores / atomicidad:

- `overall_result=false` no crea usuario ni cuenta (solo la verificacion).
- El codigo OTP reutilizado falla (`INVALID_SETUP_CODE`).

Sin Postgres: SQLite en memoria con schemas ATTACH (patron de
`test_kyc_onboarding_submit.py` + `test_pin_login.py` + `test_device_login.py`).
Sin correo/SMS reales: `sender_for_channel` se reemplaza por un capturador.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.adapters.kyc_provider import MockKycProvider
from app.adapters.notification_sender import SendResult
from app.core.db import Base, get_db
from app.main import app
from app.modules.identity.api.kyc import get_kyc_provider
from app.modules.notifications import service as notifications_service

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode()
JPEG_B64 = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 100).decode()

PIN = "4829"

ACTIVATION_PURPOSE = "ACTIVATION"


class CapturingNotificationSender:
    """Sender falso que captura el body renderizado (el OTP en claro) sin red."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, *, channel: str, recipient: str, subject: str | None, body: str) -> SendResult:
        self.calls.append(
            {
                "channel": channel,
                "recipient": recipient,
                "subject": subject,
                "body": body,
            }
        )
        return SendResult(ok=True, provider_ref="capturado-1")

    def email_codes(self, recipient: str) -> list[str]:
        """Codigos de 6 digitos capturados para `recipient` por email."""
        codes: list[str] = []
        for call in self.calls:
            if call["channel"] != "email" or call["recipient"] != recipient:
                continue
            match = re.search(r"\b(\d{6})\b", call["body"])
            if match is not None:
                codes.append(match.group(1))
        return codes


@pytest.fixture()
def e2e_session():
    """Sesion SQLite aislada con todos los schemas que toca el alta+login."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401 (registro)
    import app.modules.audit.models as _au  # noqa: F401 (registro)
    import app.modules.identity.models as _i  # noqa: F401 (registro)
    import app.modules.ledger.models as _l  # noqa: F401 (registro)
    import app.modules.notifications.models as _n  # noqa: F401 (registro)
    import app.modules.shared.models as _s  # noqa: F401 (registro)

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
            Base.metadata.tables["identity.device_bindings"],
            Base.metadata.tables["identity.sessions"],
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
def e2e_client(monkeypatch, e2e_session: Session):
    """TestClient con KYC falso, sender capturador y `get_db` a SQLite."""
    from app.modules.identity.domain import nonce as nonce_domain
    from app.modules.identity.service import activation, kyc_proxy

    kyc_proxy.reset_rate_limits()
    activation.reset_activation_rate_limits()
    nonce_domain.reset_nonces()

    sender = CapturingNotificationSender()
    monkeypatch.setattr(notifications_service, "sender_for_channel", lambda channel: sender)
    app.dependency_overrides[get_kyc_provider] = lambda: MockKycProvider(mode="success")

    def _override_db():
        yield e2e_session

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as client:
            yield client, sender
    finally:
        app.dependency_overrides.pop(get_kyc_provider, None)
        app.dependency_overrides.pop(get_db, None)
        kyc_proxy.reset_rate_limits()
        activation.reset_activation_rate_limits()
        nonce_domain.reset_nonces()


def _payload(*, doc_number: str, email: str, phone: str = "999888777") -> dict:
    return {
        "challenge_token": "tok-e2e",
        "document": {"type": "DNI", "number": doc_number, "image_b64": PNG_B64},
        "segments": [{"task": "blink", "image_b64": JPEG_B64}],
        "applicant": {
            "first_name": "Ana",
            "last_name": "Quispe",
            "email": email,
            "phone": phone,
        },
    }


def _onboard(client: TestClient, sender: CapturingNotificationSender) -> tuple[dict, str, str, str]:
    """Corre el submit exitoso y devuelve `(data, codigo_otp, doc, email)`."""
    doc = f"DNI{uuid.uuid4().hex[:8]}"
    email = f"e2e.{uuid.uuid4().hex[:8]}@example.com"
    resp = client.post("/api/v1/auth/kyc/submit", json=_payload(doc_number=doc, email=email))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["overall_result"] is True
    codes = sender.email_codes(email)
    assert codes, "el OTP de activacion debe salir por email (capturado por el fake)"
    return data, codes[-1], doc, email


def _count(session: Session, table_name: str) -> int:
    return int(
        session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[table_name]))
    )


def _bindings(session: Session, user_id: uuid.UUID) -> list:
    from app.modules.identity.models import DeviceBinding

    session.expire_all()
    stmt = sa.select(DeviceBinding).where(DeviceBinding.user_id == user_id)
    return list(session.scalars(stmt).all())


def _outbox(session: Session, event_type: str) -> list:
    stmt = sa.select(Base.metadata.tables["shared.outbox"]).where(
        Base.metadata.tables["shared.outbox"].c.event_type == event_type
    )
    return list(session.execute(stmt).all())


# ------------------------------------------------------------------ Camino E2E
def test_e2e_full_alta_y_login(e2e_client, e2e_session: Session):
    """Pasos 1-4 en secuencia, con fakes (KYC + sender) y cero red real."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.identity import repository as identity_repo
    from app.modules.notifications.models import Notification

    client, sender = e2e_client

    # --- 1. submit KYC crea User/Credential/Account/KycVerification/OTP -----
    data, code, _doc, _email = _onboard(client, sender)
    user_id = data["user_id"]
    account_id = data["account_id"]
    assert data["status"] == "ONBOARDED"
    assert user_id and account_id

    user = identity_repo.get_user(e2e_session, user_id)
    assert user is not None and user.status == "PENDING_ACTIVATION"
    credential = identity_repo.get_credential(e2e_session, user_id)
    assert credential is not None and credential.pin_hash is None
    account = accounts_repo.get_account(e2e_session, account_id)
    assert account is not None and account.user_id == user.id
    verifications = identity_repo.list_by_user(e2e_session, user.id)
    assert len(verifications) == 1 and verifications[0].overall_result is True
    otp = identity_repo.get_active_otp(e2e_session, user.id, ACTIVATION_PURPOSE)
    assert otp is not None and otp.status == "PENDING"

    # El OTP salio por email y el fake lo capturo en claro.
    notifications = list(
        e2e_session.scalars(sa.select(Notification).where(Notification.user_id == user.id)).all()
    )
    assert notifications, "el alta debe registrar la notificacion del OTP"
    email_rows = [n for n in notifications if n.channel == "email"]
    assert email_rows, "el OTP de activacion viaja por email (E1-T26)"
    assert email_rows[0].template_code == "otp_code_email"
    assert email_rows[0].payload_json["data"]["code"] == code

    # --- 2. pin/setup activa + fija PIN con un solo OTP ---------------------
    setup = client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": user_id, "code": code, "pin": PIN},
    )
    assert setup.status_code == 200, setup.text
    assert setup.json()["data"] == {"user_id": user_id, "status": "ACTIVE"}
    e2e_session.expire_all()
    user = identity_repo.get_user(e2e_session, user_id)
    credential = identity_repo.get_credential(e2e_session, user_id)
    assert user is not None and user.status == "ACTIVE"
    assert credential is not None and credential.pin_hash
    assert credential.pin_hash != PIN and PIN not in credential.pin_hash
    otp = identity_repo.get_active_otp(e2e_session, user.id, ACTIVATION_PURPOSE)
    assert otp is None, "el OTP queda consumido (USED)"
    assert len(_outbox(e2e_session, "user.activated")) == 1
    assert len(_outbox(e2e_session, "kyc.completed")) == 1

    # --- 3. primer login PIN registra device_binding ------------------------
    device_id = "pixel-e2e"
    secret_hex = secrets.token_hex(24)
    device_public_key = f"hmac:{secret_hex}"
    pin_login = client.post(
        "/api/v1/auth/login/pin",
        json={
            "user_ref": user_id,
            "pin": PIN,
            "device_id": device_id,
            "device_public_key": device_public_key,
            "platform": "android",
            "biometric_type": "FACE",
        },
    )
    assert pin_login.status_code == 200, pin_login.text
    tokens = pin_login.json()["data"]
    assert tokens["access_token"] and tokens["refresh_token"]
    bindings = _bindings(e2e_session, user.id)
    assert len(bindings) == 1
    assert bindings[0].device_id == device_id
    assert bindings[0].public_key == device_public_key
    assert bindings[0].status == "ACTIVE"

    # Segundo login PIN: no duplica el binding (touch sobre la misma fila).
    second_pin = client.post(
        "/api/v1/auth/login/pin",
        json={
            "user_ref": user_id,
            "pin": PIN,
            "device_id": device_id,
            "device_public_key": device_public_key,
        },
    )
    assert second_pin.status_code == 200, second_pin.text
    assert len(_bindings(e2e_session, user.id)) == 1

    # --- 4. challenge + firma HMAC del nonce -> facial ----------------------
    challenge = client.post(
        "/api/v1/auth/login/challenge",
        json={"user_ref": user_id, "device_id": device_id},
    )
    assert challenge.status_code == 200, challenge.text
    nonce = challenge.json()["data"]["nonce"]
    signature = hmac.new(
        bytes.fromhex(secret_hex), nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    facial = client.post(
        "/api/v1/auth/login/facial",
        json={"nonce": nonce, "device_id": device_id, "signature": signature},
    )
    assert facial.status_code == 200, facial.text
    facial_tokens = facial.json()["data"]
    assert facial_tokens["access_token"] and facial_tokens["refresh_token"]

    assert len(_outbox(e2e_session, "auth.login_succeeded")) >= 3


def test_e2e_rejected_submit_does_not_create_user(e2e_client, e2e_session: Session):
    """`overall_result=false`: solo se guarda la verificacion fallida (atomicidad)."""
    client, sender = e2e_client
    from app.modules.identity.models import KycVerification

    app.dependency_overrides[get_kyc_provider] = lambda: MockKycProvider(mode="failure")
    doc = f"DNI{uuid.uuid4().hex[:8]}"
    email = f"rechazado.{uuid.uuid4().hex[:8]}@example.com"

    resp = client.post("/api/v1/auth/kyc/submit", json=_payload(doc_number=doc, email=email))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["overall_result"] is False
    assert data["user_id"] is None and data["account_id"] is None
    assert data["status"] == "REJECTED"

    assert _count(e2e_session, "identity.users") == 0
    assert _count(e2e_session, "accounts.accounts") == 0
    assert _count(e2e_session, "shared.outbox") == 0
    verifications = list(e2e_session.scalars(sa.select(KycVerification)).all())
    assert len(verifications) == 1
    assert verifications[0].overall_result is False
    assert verifications[0].user_id is None
    assert sender.email_codes(email) == [], "un KYC rechazado no emite OTP"


def test_e2e_reused_activation_code_fails(e2e_client, e2e_session: Session):
    """El OTP de un solo uso no se puede reutilizar en `pin/setup`."""
    client, sender = e2e_client
    data, code, _doc, _email = _onboard(client, sender)
    user_id = data["user_id"]

    first = client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": user_id, "code": code, "pin": PIN},
    )
    assert first.status_code == 200, first.text

    reused = client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": user_id, "code": code, "pin": "1234"},
    )
    assert reused.status_code == 401, reused.text
    assert reused.json()["error"]["code"] == "INVALID_SETUP_CODE"


def test_e2e_second_pin_login_keeps_single_binding(e2e_client, e2e_session: Session):
    """Dos logins PIN consecutivos con la misma clave dejan un solo binding."""
    client, sender = e2e_client
    from app.modules.identity import repository as identity_repo

    data, code, _doc, _email = _onboard(client, sender)
    user_id = data["user_id"]
    assert (
        client.post(
            "/api/v1/auth/pin/setup",
            json={"user_ref": user_id, "code": code, "pin": PIN},
        ).status_code
        == 200
    )

    user = identity_repo.get_user(e2e_session, user_id)
    payload = {
        "user_ref": user_id,
        "pin": PIN,
        "device_id": "device-unico",
        "device_public_key": "hmac:" + secrets.token_hex(24),
    }
    assert client.post("/api/v1/auth/login/pin", json=payload).status_code == 200
    assert client.post("/api/v1/auth/login/pin", json=payload).status_code == 200
    assert len(_bindings(e2e_session, user.id)) == 1
