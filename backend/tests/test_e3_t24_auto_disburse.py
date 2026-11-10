"""Desembolso automatico al firmar + notificacion dedicada (E3-T24, HU12).

Decision del dueno (inline, no consumidor): al completarse la firma del
contrato (`POST .../sign`), el desembolso a la cuenta principal ocurre
automaticamente en `savepoint` (clave estable `auto-disburse:<application_id>`,
un prestamo por expediente) y la respuesta trae el bloque aditivo
`disbursement` (`loan_id`, `transaction_id`, `amount_minor`,
`account_masked`, `disbursed_at`).

- Camino feliz: expediente `APPROVED` + juego `SIGNED` via firma con PIN ->
  `SIGNED` + desembolso `SETTLED` a la principal (el disponible sube por el
  monto aprobado), una `Transaction LOAN_DISBURSEMENT` con su asiento
  `docs/04#7.5` cuadrado, `loans`+`loan_schedules`, `contracts.loan_id`
  poblado, evento `credit.disbursed` (sin PII) y notificacion dedicada
  `credit_disbursed` (push) + `credit_disbursed_email` (email) emitida UNA
  vez (sin duplicar la alerta generica de seguridad para el mismo evento).
- Idempotencia: replay de la firma (misma clave) -> mismo snapshot, un solo
  `loans`, un solo asiento, un solo evento, una sola notificacion; reintento
  manual (`POST .../disburse`) tras el automatico -> replay de negocio sin
  doble desembolso.
- Fallo: automatico que no liquida (motor caido inyectado) -> la firma queda
  `SIGNED` con `disbursement: {FAILED}`, sin acreditacion parcial ni fila
  huerfana, y el reintento manual completa el desembolso una sola vez.
- Aislamiento: `credits` no escribe `postings`/`journal_entries` directamente
  (solo via la fachada del motor); sin `float`; sin PII en logs.

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del repo);
`get_db` y `get_current_user_id` (credits) se inyectan via
`app.dependency_overrides`. Sin red, sin `float`.
"""

from __future__ import annotations

import logging
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.modules.accounts.models as _accounts
import app.modules.audit.models as _audit
import app.modules.credits.models as _credits
import app.modules.identity.models as _identity
import app.modules.ledger.models as _ledger
import app.modules.notifications.models as _notifications
import app.modules.shared.models as _shared
import app.modules.transactions.models as _transactions
from app.adapters.credit_bureau import BureauScore, MockCreditBureau
from app.adapters.notification_sender import MockNotificationSender
from app.core.db import Base, get_db
from app.main import app
from app.modules.credits import repository as repo
from app.modules.credits.api import get_current_user_id
from app.modules.credits.service import applications as apps_service
from app.modules.credits.service import contract_signing as sign_flow
from app.modules.credits.service import documents as docs_service

assert _accounts is not None and _audit is not None and _credits is not None
assert _identity is not None and _ledger is not None and _notifications is not None
assert _shared is not None and _transactions is not None

USER = uuid.uuid4()
ACCOUNT_NUMBER = "00110002424"
EMAIL = "titular.e3t24@example.com"
AMOUNT = 1_000_000
TERM = 12
FUNDED = 50_000
PIN = "7319"

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "credits.contracts",
    "credits.loans",
    "credits.loan_schedules",
    "credits.loan_payments",
    "accounts.accounts",
    "accounts.account_balances",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "notifications.notifications",
    "notifications.notification_templates",
    "notifications.user_channel_preferences",
    "audit.audit_log",
    "identity.users",
    "identity.credentials",
]


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
        for schema in (
            "credits",
            "accounts",
            "ledger",
            "transactions",
            "shared",
            "notifications",
            "audit",
            "identity",
        ):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


_CURRENT = {"user": USER}


@pytest.fixture()
def env():
    """Sesion SQLite + TestClient con catalogo, cuenta fondeada y titular."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.identity.models import User

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    mock_sender = MockNotificationSender()
    try:
        repo.seed_demo_products(session)
        own = accounts_repo.create_account(
            session, user_id=USER, account_number=ACCOUNT_NUMBER, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, own.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        session.add(
            User(
                id=USER,
                doc_type="DNI",
                doc_number_hash="e3t24-user-hash",
                doc_number_masked="****1234",
                first_name="Test",
                last_name="User",
                email=EMAIL,
                status="ACTIVE",
                kyc_status="VERIFIED",
            )
        )
        session.commit()

        def _override_db():
            yield session

        def _override_user():
            return _CURRENT["user"]

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        _CURRENT["user"] = USER
        with TestClient(app) as client:
            yield {
                "client": client,
                "session": session,
                "own": own.id,
                "sender": mock_sender,
            }
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        _CURRENT["user"] = USER
        session.close()
        engine.dispose()


def _product_id(session: Session) -> str:
    from app.modules.credits import service as catalog

    return str(catalog.get_product(session, code="PERSONAL_LIBRE")["id"])


def _approved_app_id(session: Session, user: uuid.UUID = USER) -> str:
    view = apps_service.submit_application(
        session,
        user,
        product_id=_product_id(session),
        requested_amount_minor=AMOUNT,
        term_months=TERM,
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        bureau_authorized=True,
        bureau_provider=_bureau_for(user),
        notify=False,
    )
    session.commit()
    return view["id"]


def _sign_with_auto_disburse(
    session: Session, app_id: str, key: str, *, sender=None, email_resolver=None
):
    """Firma con PIN por el servicio (firma + copia + auto-desembolso)."""
    data, replayed = sign_flow.sign_contract_idempotent(
        session,
        USER,
        app_id,
        pin=PIN,
        idempotency_key=key,
        sender=sender,
        pin_verifier=lambda s, u, p: p == PIN,
        email_resolver=email_resolver,
    )
    session.commit()
    return data, replayed


def _count(session: Session, key: str) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[key])))


def _avail(session: Session, account_id: uuid.UUID) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _outbox_by_type(session: Session, event_type: str) -> list:
    from app.modules.shared.models import OutboxEntry

    return list(
        session.scalars(sa.select(OutboxEntry).where(OutboxEntry.event_type == event_type)).all()
    )


def _notifications_by_template(session: Session, code: str) -> list:
    return list(
        session.scalars(
            sa.select(_notifications.Notification).where(
                _notifications.Notification.template_code == code
            )
        ).all()
    )


# ------------------------------------------------------------ camino feliz


def test_sign_auto_disburses_to_primary_with_single_dedicated_notification(env, caplog):
    session, own = env["session"], env["own"]
    app_id = _approved_app_id(session)
    payload, _ = docs_service.generate_contracts(session, USER, app_id)
    assert len(payload["documents"]) == 3
    session.commit()

    with caplog.at_level(logging.INFO):
        data, replayed = _sign_with_auto_disburse(
            session, app_id, "sign-auto-happy", sender=env["sender"]
        )
    assert replayed is False
    assert data["status"] == "SIGNED"

    # Bloque aditivo del desembolso automatico (respuesta de la firma).
    auto = data["disbursement"]
    assert auto["status"] == "SETTLED"
    assert auto["loan_id"] and auto["transaction_id"] and auto["disbursed_at"]
    assert auto["amount_minor"] == AMOUNT
    assert auto["account_masked"] == f"****{ACCOUNT_NUMBER[-4:]}"

    # El disponible de la principal sube por el monto aprobado.
    assert _avail(session, own) == FUNDED + AMOUNT

    # Un prestamo, un asiento cuadrado Debe 5000 / Haber 2000, un evento.
    assert _count(session, "credits.loans") == 1
    assert _count(session, "credits.loan_schedules") == TERM
    from app.modules.ledger import service as ledger_service

    entries = list(session.scalars(sa.select(_ledger.JournalEntry)).all())
    assert len(entries) == 1 and entries[0].entry_type == "LOAN_DISBURSEMENT"
    postings = ledger_service.list_postings(session, entries[0].id)
    debit = sum(p.amount_minor for p in postings if p.direction == "DEBIT")
    credit = sum(p.amount_minor for p in postings if p.direction == "CREDIT")
    assert debit == credit == AMOUNT
    assert len(_outbox_by_type(session, "credit.disbursed")) == 1

    # Notificacion dedicada UNA vez: un push + un email (plantillas aditivas).
    push = _notifications_by_template(session, "credit_disbursed")
    mail = _notifications_by_template(session, "credit_disbursed_email")
    assert len(push) == 1 and push[0].status == "SENT" and push[0].channel == "push"
    assert len(mail) == 1 and mail[0].status == "SENT" and mail[0].channel == "email"
    # La generica de seguridad NO se emite para este evento (sin duplicar).
    generics = _notifications_by_template(session, "security_operation_alert")
    generic_mails = _notifications_by_template(session, "security_operation_email")
    assert generics == [] and generic_mails == []

    # `contracts.loan_id` poblado (G54) y sin PII en logs.
    from app.modules.credits.models import Contract

    rows = session.scalars(
        sa.select(Contract).where(Contract.application_id == uuid.UUID(app_id))
    ).all()
    assert len(rows) == 3 and {str(r.loan_id) for r in rows} == {auto["loan_id"]}
    assert ACCOUNT_NUMBER not in caplog.text
    assert EMAIL not in caplog.text


def test_sign_response_carries_additive_disbursement_over_http(env, monkeypatch):
    from app.modules.credits.service import signing as signing_service

    monkeypatch.setattr(signing_service, "default_pin_verifier", lambda s, u, p: p == PIN)
    client, session = env["client"], env["session"]
    app_id = _approved_app_id(session)
    docs_service.generate_contracts(session, USER, app_id)
    session.commit()
    resp = client.post(
        f"/api/v1/credits/applications/{app_id}/sign",
        json={"pin": PIN},
        headers={"Idempotency-Key": "sign-auto-http"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    auto = body["data"]["disbursement"]
    assert auto["status"] == "SETTLED"
    assert auto["amount_minor"] == AMOUNT
    assert auto["account_masked"] == f"****{ACCOUNT_NUMBER[-4:]}"
    assert body["meta"]["replayed"] is False


# ------------------------------------------------------- idempotencia


def test_sign_replay_does_not_double_disburse_or_notify(env):
    session = env["session"]
    app_id = _approved_app_id(session)
    docs_service.generate_contracts(session, USER, app_id)
    session.commit()

    first, replayed_first = _sign_with_auto_disburse(
        session, app_id, "sign-auto-replay", sender=env["sender"]
    )
    assert replayed_first is False
    second, replayed_second = _sign_with_auto_disburse(
        session, app_id, "sign-auto-replay", sender=env["sender"]
    )
    assert replayed_second is True
    assert second == first

    # Un solo prestamo, un solo asiento, un solo evento, una sola notificacion.
    assert _count(session, "credits.loans") == 1
    assert _count(session, "ledger.journal_entries") == 1
    assert len(_outbox_by_type(session, "credit.disbursed")) == 1
    assert len(_notifications_by_template(session, "credit_disbursed")) == 1
    assert len(_notifications_by_template(session, "credit_disbursed_email")) == 1


def test_manual_disburse_after_auto_is_business_replay_without_double_money(env):
    client, session, own = env["client"], env["session"], env["own"]
    app_id = _approved_app_id(session)
    docs_service.generate_contracts(session, USER, app_id)
    session.commit()
    data, _ = _sign_with_auto_disburse(session, app_id, "sign-auto-then-manual")
    assert data["disbursement"]["status"] == "SETTLED"

    # El endpoint manual se conserva para reintento: replay de negocio.
    resp = client.post(
        f"/api/v1/credits/applications/{app_id}/disburse",
        json={},
        headers={"Idempotency-Key": "manual-after-auto"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["loan_id"] == data["disbursement"]["loan_id"]
    assert _count(session, "credits.loans") == 1
    assert _count(session, "ledger.journal_entries") == 1
    assert _avail(session, own) == FUNDED + AMOUNT
    # Sin segunda notificacion dedicada en el replay.
    assert len(_notifications_by_template(session, "credit_disbursed")) == 1


# ------------------------------------------------------------------ fallo


def test_failed_auto_disburse_keeps_signed_and_manual_retry_completes_once(env, monkeypatch):
    from app.modules.transactions.service import disbursement as motor

    client, session, own = env["client"], env["session"], env["own"]
    app_id = _approved_app_id(session)
    docs_service.generate_contracts(session, USER, app_id)
    session.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("motor caido")

    monkeypatch.setattr(motor, "disburse_loan", _boom)
    data, replayed = _sign_with_auto_disburse(session, app_id, "sign-auto-boom")
    assert replayed is False
    assert data["status"] == "SIGNED"

    # La firma queda; el automatico expone el fallo para reintento.
    auto = data["disbursement"]
    assert auto["status"] == "FAILED" and auto["code"]
    from app.modules.credits.models import Contract

    rows = session.scalars(
        sa.select(Contract).where(Contract.application_id == uuid.UUID(app_id))
    ).all()
    assert {r.status for r in rows} == {"SIGNED"}
    # Sin acreditacion parcial ni fila huerfana.
    assert _avail(session, own) == FUNDED
    assert _count(session, "credits.loans") == 0
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "ledger.journal_entries") == 0
    assert _outbox_by_type(session, "credit.disbursed") == []

    # Reintento manual: completa el desembolso una sola vez.
    monkeypatch.undo()
    resp = client.post(
        f"/api/v1/credits/applications/{app_id}/disburse",
        json={},
        headers={"Idempotency-Key": "retry-after-boom"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["status"] == "SETTLED"
    assert _avail(session, own) == FUNDED + AMOUNT
    assert _count(session, "credits.loans") == 1
    assert _count(session, "ledger.journal_entries") == 1


def test_provider_outage_keeps_disbursement_with_failed_notification(env, monkeypatch):
    from app.modules.notifications import service as notifications_service

    session = env["session"]
    app_id = _approved_app_id(session)
    docs_service.generate_contracts(session, USER, app_id)
    session.commit()

    def _down(*args, **kwargs):
        raise RuntimeError("proveedor caido")

    monkeypatch.setattr(notifications_service, "send", _down)
    data, _ = _sign_with_auto_disburse(session, app_id, "sign-auto-no-notify")
    # Best-effort: el desembolso queda intacto aunque no haya notificacion.
    assert data["disbursement"]["status"] == "SETTLED"
    assert _count(session, "credits.loans") == 1
    assert _count(session, "ledger.journal_entries") == 1
