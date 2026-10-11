"""Endpoint de firma + copia por correo (E3-T14, HU11 CA-02/CA-03/CA-04).

- Camino feliz: juego `GENERATED` -> `POST .../sign/challenge` ->
  `POST .../sign` con firma HMAC valida (verificador REAL de
  `identity`, binding inyectado) -> `200 SIGNED` + 3 sellos
  verificables + evento `credit.contract.signed` + **un** correo
  `credit_contract_signed` (`SENT`, via mock del sender real por
  defecto). Via PIN (fallback, decision del dueno): mismo sellado.
- Errores: sin `Idempotency-Key` -> `422`; ambos/ningun factor ->
  `422` sin eco del PIN; firma/binding invalido -> `403` generico;
  `nonce` vencido -> `400 EXPIRED_NONCE`; `nonce` reutilizado ->
  generico; ajeno/inexistente -> `404` neutro; ya `SIGNED` -> `409`;
  misma clave + cuerpo distinto -> `409`.
- Idempotencia: doble `POST` misma clave -> una firma y **un** correo;
  reintento tras `EXPIRED_NONCE` con reto rotado + clave nueva -> `200`.
- Correo best-effort: sender caido -> firma `SIGNED` igual y
  notificacion `FAILED`; sin correo resoluble -> `200` sin fila.
- Sin PII/secretos en logs; sin mover dinero (0 `loans`/
  `transactions`/`postings`); frontera D-03 intacta.

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del
repo); `get_db` y `get_current_user_id` (credits) se inyectan via
`app.dependency_overrides`; `identity.domain.nonce` es en memoria
(se resetea por test). Sin red.
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
DEVICE = "dev-e3t14-1"
SECRET_HEX = "cd" * 20  # 20 bytes (formato hmac: exigido por el verificador real)
PUBLIC_KEY = f"hmac:{SECRET_HEX}"
PIN = "7319"
EMAIL = "titular.e3t14@example.com"

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
    # `identity.credentials` (vacia): `get_user` precarga la credencial
    # (`lazy="selectin"`); sin la tabla el `SELECT` fallaria en SQLite.
    "identity.credentials",
]

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


def _seed(session: Session, *, email: str | None = EMAIL) -> None:
    """Catalogo demo + titular de `identity` con correo (fachada real)."""
    from app.modules.identity.models import User

    repo.seed_demo_products(session)
    session.add(
        User(
            id=USER,
            doc_type="DNI",
            doc_number_hash="e3t14-user-hash",
            doc_number_masked="****1234",
            first_name="Test",
            last_name="User",
            email=email or "sin-correo@example.com",
            status="ACTIVE",
            kyc_status="VERIFIED",
        )
    )
    if email is None:
        session.flush()
        row = session.get(User, USER)
        assert row is not None
        # Sin correo resoluble: la fachada debe devolver `None`.
        row.email = ""
    session.commit()


@pytest.fixture()
def app_client(monkeypatch):
    """TestClient con catalogo + titular, facades de firma y sender mock."""
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


def _generated_app_id(session: Session, user: uuid.UUID = USER) -> str:
    app_id = _approved_app_id(session, user)
    payload, reused = docs_service.generate_contracts(session, user, app_id)
    assert not reused and len(payload["documents"]) == 3
    session.commit()
    return app_id


def _hmac_signature(nonce: str) -> str:
    return hmac.new(bytes.fromhex(SECRET_HEX), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _challenge(client: TestClient, app_id: str, *, device: str | None = DEVICE) -> dict:
    body = {} if device is None else {"device_id": device}
    resp = client.post(f"/api/v1/credits/applications/{app_id}/sign/challenge", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _sign(client: TestClient, app_id: str, body: dict, *, key: str | None = "k-sign"):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(f"/api/v1/credits/applications/{app_id}/sign", json=body, headers=headers)


def _device_body(nonce: str, *, signature: str | None = None) -> dict:
    return {
        "device_id": DEVICE,
        "signature": signature if signature is not None else _hmac_signature(nonce),
        "nonce": nonce,
    }


def _notifications(session: Session) -> list:
    """Filas de notificacion (`channel`, `template_code`, `status`).

    Nota: el alta del expediente (E3-T08) ya deja un push
    `credit_application_approved`; los asserts de E3-T14 filtran por
    la plantilla `credit_contract_signed`.
    """
    table = Base.metadata.tables["notifications.notifications"]
    session.expire_all()
    return list(
        session.execute(
            sa.select(table.c.channel, table.c.template_code, table.c.status).order_by(
                table.c.created_at
            )
        ).all()
    )


def _email_copies(session: Session) -> list:
    return [row for row in _notifications(session) if row[1] == "credit_contract_signed"]


def _outbox_by_type(session: Session, event_type: str) -> list[dict]:
    table = Base.metadata.tables["shared.outbox"]
    session.expire_all()
    return [
        row[0]
        for row in session.execute(
            sa.select(table.c.payload).where(table.c.event_type == event_type)
        ).all()
    ]


# ------------------------------------------------ reto


def test_challenge_happy_path(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    resp = client.post(
        f"/api/v1/credits/applications/{app_id}/sign/challenge", json={"device_id": DEVICE}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["application_id"] == app_id
    assert isinstance(data["nonce"], str) and data["nonce"].strip()
    assert data["expires_in"] == 120
    assert data["status"] == "GENERATED"


def test_challenge_without_body_ok(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    resp = client.post(f"/api/v1/credits/applications/{app_id}/sign/challenge")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["application_id"] == app_id


def test_challenge_foreign_missing_or_signed(app_client):
    client, session = app_client["client"], app_client["session"]
    foreign_id = _generated_app_id(session, OTHER)
    for target in (foreign_id, str(uuid.uuid4())):
        resp = client.post(f"/api/v1/credits/applications/{target}/sign/challenge", json={})
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "NOT_FOUND"
    # Ya firmado -> 409 (sin `Idempotency-Key`: solo rotacion).
    app_id = _generated_app_id(session)
    signed = _sign(client, app_id, {"pin": PIN}, key="k-ch-signed")
    assert signed.status_code == 200, signed.text
    conflict = client.post(f"/api/v1/credits/applications/{app_id}/sign/challenge", json={})
    assert conflict.status_code == 409, conflict.text


def test_challenge_rotates_nonce(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    first = _challenge(client, app_id)
    second = _challenge(client, app_id)
    assert first["nonce"] != second["nonce"]


# ------------------------------------------------ camino feliz


def test_sign_device_happy_path_sends_single_email(app_client):
    client, session, sender = (
        app_client["client"],
        app_client["session"],
        app_client["sender"],
    )
    app_id = _generated_app_id(session)
    challenge = _challenge(client, app_id)
    resp = _sign(client, app_id, _device_body(challenge["nonce"]), key="k-happy")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["application_id"] == app_id
    assert data["status"] == "SIGNED"
    assert data["via"] == "device"
    assert re.fullmatch(r"[0-9a-f-]{36}", data["contract_id"])
    assert data["contract_id"] == data["documents"][0]["contract_id"]
    assert len(data["documents"]) == 3
    assert body["meta"]["replayed"] is False
    # CA-03: sellos derivados reproducibles (no repudio).
    for doc in data["documents"]:
        row = contracts_repo.get_by_application_and_type(session, app_id, doc["doc_type"])
        assert row is not None and row.status == "SIGNED" and row.signed_at is not None
        assert signing_domain.verify_seal(
            doc_hash=row.doc_hash,
            signed_at_iso=data["signed_at"],
            signature_token_hash=row.signature_token_hash or "",
            seal=doc["seal"],
        )
    # CA-04: una sola copia por correo (mock del sender real por defecto).
    assert len(sender.calls) == 1
    assert sender.calls[0]["channel"] == "email"
    rows = _email_copies(session)
    assert len(rows) == 1
    assert rows[0][0] == "email" and rows[0][1] == "credit_contract_signed"
    assert rows[0][2] == "SENT"
    events = _outbox_by_type(session, "credit.contract.signed")
    assert len(events) == 1
    assert events[0]["application_id"] == app_id
    assert events[0]["status"] == "SIGNED"


def test_sign_pin_happy_path(app_client):
    client, session, sender = (
        app_client["client"],
        app_client["session"],
        app_client["sender"],
    )
    app_id = _generated_app_id(session)
    resp = _sign(client, app_id, {"pin": PIN}, key="k-pin")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SIGNED"
    assert data["via"] == "pin"
    assert len(data["documents"]) == 3
    assert len(sender.calls) == 1
    rows = contracts_repo.list_by_application(session, app_id)
    assert all(row.status == "SIGNED" for row in rows)
    # El PIN en claro jamas queda en la BD.
    assert all(PIN not in (row.signature_token_hash or "") for row in rows)


# ------------------------------------------------ errores


def test_sign_missing_key_422(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    challenge = _challenge(client, app_id)
    resp = _sign(client, app_id, _device_body(challenge["nonce"]), key=None)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_sign_factor_exclusivity_422_without_pin_echo(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    challenge = _challenge(client, app_id)
    both = _sign(
        client,
        app_id,
        {"signature": "x", "device_id": DEVICE, "pin": PIN},
        key="k-422-both",
    )
    assert both.status_code == 422, both.text
    neither = _sign(client, app_id, {}, key="k-422-neither")
    assert neither.status_code == 422, neither.text
    # El 422 sanitario no refleja el cuerpo (sin `input`: el PIN no fuga).
    assert PIN not in both.text
    assert PIN not in neither.text
    assert challenge["nonce"] not in both.text


def test_sign_bad_signature_403_generic(app_client, monkeypatch):
    client, session, sender = (
        app_client["client"],
        app_client["session"],
        app_client["sender"],
    )
    monkeypatch.setattr(signing_service, "default_signature_verifier", lambda n, s, k: False)
    app_id = _generated_app_id(session)
    challenge = _challenge(client, app_id)
    resp = _sign(client, app_id, _device_body(challenge["nonce"]), key="k-bad-sig")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
    assert resp.json()["error"]["message"] == signing_service.NOT_AUTHORIZED_MESSAGE
    rows = contracts_repo.list_by_application(session, app_id)
    assert all(row.status == "GENERATED" for row in rows)
    assert sender.calls == [] and _email_copies(session) == []


def test_sign_wrong_pin_403_same_generic(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    resp = _sign(client, app_id, {"pin": "0000"}, key="k-bad-pin")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["message"] == signing_service.NOT_AUTHORIZED_MESSAGE


def test_sign_expired_nonce_400(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    stale = nonce_domain.issue_nonce(
        USER, device_id=DEVICE, now=datetime.now(UTC) - timedelta(seconds=300)
    )[0]
    resp = _sign(client, app_id, _device_body(stale), key="k-expired")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "EXPIRED_NONCE"
    rows = contracts_repo.list_by_application(session, app_id)
    assert all(row.status == "GENERATED" for row in rows)


def test_sign_reused_nonce_403_generic(app_client):
    client, session = app_client["client"], app_client["session"]
    app_a = _generated_app_id(session)
    nonce = _challenge(client, app_a)["nonce"]
    assert _sign(client, app_a, _device_body(nonce), key="k-reuse-a").status_code == 200
    app_b = _generated_app_id(session)
    resp = _sign(client, app_b, _device_body(nonce), key="k-reuse-b")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_sign_foreign_or_missing_404_neutral(app_client):
    client, session = app_client["client"], app_client["session"]
    foreign_id = _generated_app_id(session, OTHER)
    for target in (foreign_id, str(uuid.uuid4())):
        resp = _sign(client, target, {"pin": PIN}, key=f"k-404-{target[:8]}")
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_sign_already_signed_409(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    assert _sign(client, app_id, {"pin": PIN}, key="k-once").status_code == 200
    resp = _sign(client, app_id, {"pin": PIN}, key="k-twice-new-key")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_sign_same_key_different_body_409(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    assert _sign(client, app_id, {"pin": PIN}, key="k-clash").status_code == 200
    other = _generated_app_id(session)
    clash = _sign(client, other, {"pin": PIN}, key="k-clash")
    assert clash.status_code == 409, clash.text
    assert clash.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ------------------------------------------------ idempotencia


def test_sign_idempotent_replay_single_email(app_client):
    client, session, sender = (
        app_client["client"],
        app_client["session"],
        app_client["sender"],
    )
    app_id = _generated_app_id(session)
    challenge = _challenge(client, app_id)
    first = _sign(client, app_id, _device_body(challenge["nonce"]), key="k-replay")
    assert first.status_code == 200, first.text
    assert first.json()["meta"]["replayed"] is False
    second = _sign(client, app_id, _device_body(challenge["nonce"]), key="k-replay")
    assert second.status_code == 200, second.text
    assert second.json()["meta"]["replayed"] is True
    assert second.json()["data"] == first.json()["data"]
    # Una firma y una sola copia (el replay no reenvia).
    assert len(sender.calls) == 1
    assert len(_email_copies(session)) == 1
    assert len(_outbox_by_type(session, "credit.contract.signed")) == 1


def test_retry_after_expired_with_new_key_ok(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    stale = nonce_domain.issue_nonce(
        USER, device_id=DEVICE, now=datetime.now(UTC) - timedelta(seconds=300)
    )[0]
    failed = _sign(client, app_id, _device_body(stale), key="k-fail-1")
    assert failed.status_code == 400, failed.text
    # El fallo quedo snapshotteado: reintento identico re-juega el error.
    assert _sign(client, app_id, _device_body(stale), key="k-fail-1").status_code == 400
    # Reto rotado + clave nueva autoriza (patron G39).
    fresh = _challenge(client, app_id)
    assert fresh["nonce"] != stale
    ok = _sign(client, app_id, _device_body(fresh["nonce"]), key="k-fail-2")
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["status"] == "SIGNED"


# ------------------------------------------------ correo best-effort


def test_sign_with_provider_down_keeps_signed(app_client):
    client, session, sender = (
        app_client["client"],
        app_client["session"],
        app_client["sender"],
    )
    sender.set_channel_failure("email", "smtp caido")
    app_id = _generated_app_id(session)
    resp = _sign(client, app_id, {"pin": PIN}, key="k-down")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "SIGNED"
    rows = _email_copies(session)
    assert len(rows) == 1 and rows[0][2] == "FAILED"
    assert all(
        row.status == "SIGNED" for row in contracts_repo.list_by_application(session, app_id)
    )


def test_sign_without_email_skips_copy(app_client, monkeypatch):
    client, session, sender = (
        app_client["client"],
        app_client["session"],
        app_client["sender"],
    )
    monkeypatch.setattr(sign_flow, "_default_email_resolver", lambda: (lambda s, u: None))
    app_id = _generated_app_id(session)
    resp = _sign(client, app_id, {"pin": PIN}, key="k-no-mail")
    assert resp.status_code == 200, resp.text
    assert sender.calls == [] and _email_copies(session) == []


def test_holder_email_facade_resolves_and_neutral(app_client):
    from app.modules.identity.service import holder_contact as holder_contact_service

    _, session = app_client["client"], app_client["session"]
    assert holder_contact_service.get_holder_email(session, USER) == EMAIL
    assert holder_contact_service.get_holder_email(session, OTHER) is None
    assert holder_contact_service.get_holder_email(session, "no-es-uuid") is None


# ------------------------------------------------ logs, frontera y aislamiento


def test_no_secrets_or_pii_in_logs(app_client, caplog):
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
            key="k-logs",
        )
    assert resp.status_code == 200, resp.text
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert signature not in blob
    assert challenge["nonce"] not in blob
    assert PIN not in blob
    assert SECRET_HEX not in blob
    assert EMAIL not in blob
    assert DEVICE not in blob


def test_pin_never_persisted_in_idempotency_snapshot(app_client):
    import json

    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    assert _sign(client, app_id, {"pin": PIN}, key="k-digest").status_code == 200
    table = Base.metadata.tables["shared.idempotency_keys"]
    session.expire_all()
    snapshots = [
        json.dumps(row[0], ensure_ascii=False)
        for row in session.execute(sa.select(table.c.response_snapshot)).all()
        if row[0] is not None
    ]
    assert snapshots
    assert all(PIN not in snap for snap in snapshots)


def test_credits_sign_imports_no_foreign_models():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "modules" / "credits"
    checked = 0
    for rel in (
        "api/signing.py",
        "schemas/signing.py",
        "service/contract_signing.py",
        "service/signing.py",
    ):
        imports = [
            line.strip()
            for line in (root / rel).read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        checked += len(imports)
        for line in imports:
            assert "identity.models" not in line, (rel, line)
            assert "identity.repository" not in line, (rel, line)
            assert "notifications.models" not in line, (rel, line)
            assert "transactions" not in line, (rel, line)
            assert "ledger" not in line, (rel, line)
            assert "kyc" not in line.lower(), (rel, line)
    assert checked > 0


def test_sign_creates_no_loans_nor_money(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    assert _sign(client, app_id, {"pin": PIN}, key="k-iso").status_code == 200
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
    assert types <= {
        "credit.application.submitted",
        "credit.approved",
        "credit.contract.generated",
        "credit.contract.signed",
    }


def test_get_contract_intact_after_sign(app_client):
    client, session = app_client["client"], app_client["session"]
    app_id = _generated_app_id(session)
    assert _sign(client, app_id, {"pin": PIN}, key="k-get").status_code == 200
    meta = client.get(f"/api/v1/credits/applications/{app_id}/contract")
    assert meta.status_code == 200, meta.text
    assert len(meta.json()["data"]["documents"]) == 3
