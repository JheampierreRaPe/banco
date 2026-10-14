"""Debito automatico y recordatorios de cuota (E3-T19, HU12 CA-03/CA-04).

- Toggle: `PUT .../auto-debit {true|false}` persiste el flag (solo el
  titular; ajeno -> `404` neutro; `enabled` ausente/no booleano ->
  `422`); no mueve dinero (sin `Idempotency-Key`).
- Debito: el job cobra la cuota vencida del prestamo con
  `auto_debit_enabled=true` reutilizando el caso de uso de `E3-T20`
  (motor + asiento `7.6`); con el flag en `false` no cobra.
- Sin fondos: cuota y saldos intactos + aviso `loan_auto_debit_failed`.
- Recordatorio: solo las cuotas que vencen en
  `loan.reminder_days_before` dias (default `3`) generan un aviso
  push, una sola vez por dia.
- Idempotencia: re-ejecutar los jobs no duplica cobros ni avisos.
- Sin PII en plantillas; sender mock (sin envios reales).

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron
E3-T20); `get_db` y `get_current_user_id` (credits) se inyectan via
`app.dependency_overrides`. Sin red, sin `float`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.modules.accounts.models as _accounts
import app.modules.audit.models as _audit
import app.modules.credits.models as _credits
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
from app.modules.credits.jobs.auto_debit import AUTO_DEBIT_CONSUMER, run_auto_debit_once
from app.modules.credits.jobs.installment_reminders import (
    REMINDERS_CONSUMER,
    run_installment_reminders_once,
)
from app.modules.credits.models import Loan, LoanSchedule
from app.modules.credits.service import applications as apps_service
from app.modules.credits.service import documents as docs_service
from app.modules.credits.service import signing as signing_service

assert _accounts is not None and _audit is not None and _credits is not None
assert _ledger is not None and _shared is not None and _transactions is not None
assert _notifications is not None

USER = uuid.uuid4()
OTHER = uuid.uuid4()
ACCOUNT_NUMBER = "00110009999"
OTHER_ACCOUNT_NUMBER = "00220001111"
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
    "audit.audit_log",
    "notifications.notification_templates",
    "notifications.notifications",
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
            "audit",
            "notifications",
        ):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


_CURRENT = {"user": USER}


@pytest.fixture()
def env(monkeypatch):
    """Sesion SQLite + TestClient con catalogo, cuentas fondeadas y auth falsa."""
    from app.modules.accounts import repository as accounts_repo

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        repo.seed_demo_products(session)
        own = accounts_repo.create_account(
            session, user_id=USER, account_number=ACCOUNT_NUMBER, type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, own.id, available_delta=FUNDED, held_delta=0, currency="PEN"
        )
        stranger = accounts_repo.create_account(
            session,
            user_id=OTHER,
            account_number=OTHER_ACCOUNT_NUMBER,
            type="AHORRO",
            currency="PEN",
        )
        accounts_repo.apply_delta(
            session, stranger.id, available_delta=FUNDED, held_delta=0, currency="PEN"
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
                "engine": engine,
                "own": own.id,
                "stranger": stranger.id,
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


def _application_id(session: Session, user: uuid.UUID = USER, score: int = 780) -> str:
    view = apps_service.submit_application(
        session,
        user,
        product_id=_product_id(session),
        requested_amount_minor=AMOUNT,
        term_months=TERM,
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        bureau_authorized=True,
        bureau_provider=_bureau_for(user, score),
        notify=False,
    )
    session.commit()
    return view["id"]


def _signed_app_id(session: Session, user: uuid.UUID = USER) -> str:
    app_id = _application_id(session, user)
    payload, reused = docs_service.generate_contracts(session, user, app_id)
    assert not reused and len(payload["documents"]) == 3
    signing_service.sign_contract_with_pin(
        session, user, app_id, pin=PIN, pin_verifier=lambda s, u, p: p == PIN
    )
    session.commit()
    return app_id


def _disbursed_loan_id(session: Session, client: TestClient, own, key: str) -> str:
    app_id = _signed_app_id(session)
    resp = client.post(
        f"/api/v1/credits/applications/{app_id}/disburse",
        json={"account_id": str(own)},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["loan_id"]


def _put_auto_debit(client: TestClient, loan_id: str, body: dict):
    return client.put(f"/api/v1/credits/loans/{loan_id}/auto-debit", json=body)


def _today() -> date:
    """Hoy en UTC (los jobs operan en fecha UTC; determinista por test)."""
    return datetime.now(UTC).date()


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


def _notifications_by_template(session: Session, template_code: str) -> list:
    from app.modules.notifications.models import Notification

    return list(
        session.scalars(
            sa.select(Notification).where(Notification.template_code == template_code)
        ).all()
    )


def _schedules(session: Session, loan_id: uuid.UUID) -> list[LoanSchedule]:
    return list(
        session.scalars(
            sa.select(LoanSchedule)
            .where(LoanSchedule.loan_id == loan_id)
            .order_by(LoanSchedule.installment_no)
        ).all()
    )


def _loan(session: Session, loan_id: str) -> Loan:
    return session.scalars(sa.select(Loan).where(Loan.id == uuid.UUID(loan_id))).one()


def _enable(session: Session, client: TestClient, loan_id: str, enabled: bool = True) -> None:
    resp = _put_auto_debit(client, loan_id, {"enabled": enabled})
    assert resp.status_code == 200, resp.text


# ------------------------------------------------------------ toggle


def test_toggle_auto_debit_persists_and_reads_back(env):
    client, session = env["client"], env["session"]
    loan_id = _disbursed_loan_id(session, client, env["own"], key="k-disb-toggle")
    assert _loan(session, loan_id).auto_debit_enabled is False

    resp = _put_auto_debit(client, loan_id, {"enabled": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == {"loan_id": loan_id, "auto_debit_enabled": True}
    assert body["meta"]["request_id"]
    assert _loan(session, loan_id).auto_debit_enabled is True

    # Lectura del cronograma refleja el flag (lo consume E3-T21).
    sched = client.get(f"/api/v1/credits/loans/{loan_id}/schedule")
    assert sched.status_code == 200, sched.text
    assert sched.json()["data"]["loan"]["auto_debit_enabled"] is True

    off = _put_auto_debit(client, loan_id, {"enabled": False})
    assert off.status_code == 200, off.text
    assert off.json()["data"]["auto_debit_enabled"] is False
    assert _loan(session, loan_id).auto_debit_enabled is False


def test_toggle_auto_debit_foreign_is_neutral_404(env):
    client, session = env["client"], env["session"]
    loan_id = _disbursed_loan_id(session, client, env["own"], key="k-disb-foreign")
    _CURRENT["user"] = OTHER
    try:
        resp = _put_auto_debit(client, loan_id, {"enabled": True})
    finally:
        _CURRENT["user"] = USER
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert _loan(session, loan_id).auto_debit_enabled is False


def test_toggle_auto_debit_unknown_is_neutral_404(env):
    resp = _put_auto_debit(env["client"], str(uuid.uuid4()), {"enabled": True})
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}, {"enabled": None}])
def test_toggle_auto_debit_invalid_is_422(env, body):
    client, session = env["client"], env["session"]
    loan_id = _disbursed_loan_id(
        session, client, env["own"], key=f"k-disb-422-{uuid.uuid4().hex[:8]}"
    )
    resp = _put_auto_debit(client, loan_id, body)
    assert resp.status_code == 422, resp.text
    assert _loan(session, loan_id).auto_debit_enabled is False


# ------------------------------------------------------------ debito automatico


def test_auto_debit_pays_overdue_when_enabled(env):
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-debit")
    _enable(session, client, loan_id, True)
    first = _schedules(session, uuid.UUID(loan_id))[0]
    today = _today()
    first.due_date = today - timedelta(days=1)
    session.commit()
    prior_avail = _avail(session, own)
    prior_outstanding = _loan(session, loan_id).outstanding_principal_minor
    sender = MockNotificationSender()

    result = run_auto_debit_once(session, today=today, sender=sender)

    assert result["paid"] == [str(first.id)]
    assert result["failed"] == [] and result["skipped"] == []
    assert session.get(LoanSchedule, first.id).status == "PAID"
    assert _avail(session, own) == prior_avail - int(first.total_minor)
    assert _loan(session, loan_id).outstanding_principal_minor == prior_outstanding - int(
        first.principal_minor
    )
    assert _count(session, "credits.loan_payments") == 1
    assert len(_outbox_by_type(session, "loan.installment.paid")) == 1
    # Sin aviso de fallo en el camino feliz.
    assert _notifications_by_template(session, "loan_auto_debit_failed") == []
    assert sender.calls == []


def test_auto_debit_disabled_loan_is_not_charged(env):
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-nobit")
    first = _schedules(session, uuid.UUID(loan_id))[0]
    today = _today()
    first.due_date = today - timedelta(days=1)
    session.commit()
    prior_avail = _avail(session, own)

    result = run_auto_debit_once(session, today=today, sender=MockNotificationSender())

    assert result == {
        "paid": [],
        "failed": [],
        "skipped": [],
        "as_of": today.isoformat(),
    }
    # El job no toco la cuota (sigue PENDING: ni siquiera la marco OVERDUE).
    assert session.get(LoanSchedule, first.id).status == "PENDING"
    assert _avail(session, own) == prior_avail
    assert _count(session, "credits.loan_payments") == 0
    assert _count(session, "ledger.journal_entries") == 1  # solo el desembolso


def test_auto_debit_insufficient_funds_notifies_without_moving_money(env):
    from app.modules.accounts import repository as accounts_repo

    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-poor")
    _enable(session, client, loan_id, True)
    first = _schedules(session, uuid.UUID(loan_id))[0]
    today = _today()
    first.due_date = today - timedelta(days=1)
    session.commit()
    # Drena la cuenta (incluido lo desembolsado) hasta 100 centimos.
    accounts_repo.apply_delta(
        session, own, available_delta=100 - _avail(session, own), held_delta=0, currency="PEN"
    )
    session.commit()
    sender = MockNotificationSender()

    result = run_auto_debit_once(session, today=today, sender=sender)

    assert result["paid"] == [] and result["skipped"] == []
    assert len(result["failed"]) == 1
    item = result["failed"][0]
    assert item["schedule_id"] == str(first.id)
    assert item["error"] == "INSUFFICIENT_FUNDS"
    assert item["notified"] is True

    # Sin cambios de dinero: saldo intacto, sin pago ni asiento de cuota.
    assert _avail(session, own) == 100
    row = session.get(LoanSchedule, first.id)
    assert row.status in ("PENDING", "OVERDUE") and row.paid_at is None
    assert _count(session, "credits.loan_payments") == 0
    assert _count(session, "ledger.journal_entries") == 1  # solo el desembolso
    assert len(_outbox_by_type(session, "loan.installment.paid")) == 0

    # Un aviso push de fallo, sin PII ni cifras.
    notes = _notifications_by_template(session, "loan_auto_debit_failed")
    assert len(notes) == 1 and notes[0].status == "SENT"
    assert notes[0].channel == "push"
    assert len(sender.calls) == 1 and sender.calls[0]["channel"] == "push"


def test_auto_debit_rerun_does_not_duplicate_charges_or_notices(env):
    from app.modules.accounts import repository as accounts_repo

    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-disb-rerun")
    _enable(session, client, loan_id, True)
    first = _schedules(session, uuid.UUID(loan_id))[0]
    today = _today()
    first.due_date = today - timedelta(days=1)
    session.commit()
    sender = MockNotificationSender()

    once = run_auto_debit_once(session, today=today, sender=sender)
    assert once["paid"] == [str(first.id)]
    twice = run_auto_debit_once(session, today=today, sender=sender)

    assert twice["paid"] == [] and twice["failed"] == [] and twice["skipped"] == []
    assert _count(session, "credits.loan_payments") == 1
    assert _count(session, "transactions.transactions") == 2  # desembolso + pago
    assert len(_outbox_by_type(session, "loan.installment.paid")) == 1

    # Camino de fallo: el segundo ciclo no repite el aviso del mismo dia.
    accounts_repo.apply_delta(
        session, own, available_delta=100 - _avail(session, own), held_delta=0, currency="PEN"
    )
    session.commit()
    second = _schedules(session, uuid.UUID(loan_id))[1]
    second.due_date = today
    session.commit()
    poor_sender = MockNotificationSender()
    failed_once = run_auto_debit_once(session, today=today, sender=poor_sender)
    assert len(failed_once["failed"]) == 1 and failed_once["failed"][0]["notified"] is True
    failed_twice = run_auto_debit_once(session, today=today, sender=poor_sender)
    assert len(failed_twice["failed"]) == 1 and failed_twice["failed"][0]["notified"] is False
    assert len(_notifications_by_template(session, "loan_auto_debit_failed")) == 1
    assert _count(session, "credits.loan_payments") == 1  # el cobro feliz no se duplico


# ------------------------------------------------------------ recordatorios


def test_reminder_sent_once_for_installment_due_in_three_days(env):
    client, session = env["client"], env["session"]
    loan_id = _disbursed_loan_id(session, client, env["own"], key="k-disb-remind")
    rows = _schedules(session, uuid.UUID(loan_id))
    today = _today()
    rows[1].due_date = today + timedelta(days=3)
    session.commit()
    sender = MockNotificationSender()

    result = run_installment_reminders_once(session, today=today, sender=sender)

    assert result["notified"] == [str(rows[1].id)]
    assert result["failed"] == []
    assert result["days_before"] == 3
    notes = _notifications_by_template(session, "loan_installment_reminder")
    assert len(notes) == 1 and notes[0].status == "SENT" and notes[0].channel == "push"
    assert len(sender.calls) == 1

    # Re-ejecutar el mismo dia no repite el aviso.
    again = run_installment_reminders_once(session, today=today, sender=sender)
    assert again["notified"] == [] and again["failed"] == []
    assert again["skipped"] == [str(rows[1].id)]
    assert len(_notifications_by_template(session, "loan_installment_reminder")) == 1
    assert len(sender.calls) == 1


def test_reminder_ignores_installments_outside_window(env):
    client, session = env["client"], env["session"]
    loan_id = _disbursed_loan_id(session, client, env["own"], key="k-disb-window")
    rows = _schedules(session, uuid.UUID(loan_id))
    today = _today()
    rows[0].due_date = today  # vence hoy: es del debito, no del recordatorio
    rows[1].due_date = today + timedelta(days=5)
    rows[2].due_date = today + timedelta(days=2)
    session.commit()

    result = run_installment_reminders_once(session, today=today, sender=MockNotificationSender())

    assert result["notified"] == [] and result["failed"] == [] and result["skipped"] == []
    assert _notifications_by_template(session, "loan_installment_reminder") == []


def test_reminder_window_is_configurable(env, monkeypatch):
    import app.modules.shared.config as config_facade

    client, session = env["client"], env["session"]
    loan_id = _disbursed_loan_id(session, client, env["own"], key="k-disb-param")
    rows = _schedules(session, uuid.UUID(loan_id))
    today = _today()
    rows[1].due_date = today + timedelta(days=2)
    session.commit()
    monkeypatch.setattr(config_facade, "get_loan_reminder_days_before", lambda _s: 2)

    result = run_installment_reminders_once(session, today=today, sender=MockNotificationSender())

    assert result["days_before"] == 2
    assert result["notified"] == [str(rows[1].id)]


def test_reminder_default_window_is_three_days_without_config_table(env):
    from app.modules.shared.config import get_loan_reminder_days_before

    # `config.parameters` no existe en este humo: rige el fallback.
    assert get_loan_reminder_days_before(env["session"]) == 3


# ------------------------------------------------------------ plantillas y cableado


def test_loan_templates_have_no_pii_or_amounts():
    from app.modules.notifications.domain.templates import BASE_TEMPLATES, render_template

    reminder = BASE_TEMPLATES["loan_installment_reminder"]
    failed = BASE_TEMPLATES["loan_auto_debit_failed"]
    assert reminder["channel"] == "push" and failed["channel"] == "push"
    reminder_body = render_template(
        reminder["body_template"], {"loan_ref": "abcd1234", "due_on": "2026-10-20"}
    )
    failed_body = render_template(
        failed["body_template"], {"loan_ref": "abcd1234", "at": "2026-10-13"}
    )
    for body in (reminder_body, failed_body):
        assert "S/" not in body and "PEN" not in body
        assert "abcd1234" in body  # solo referencia tecnica, sin PII


def test_composition_runner_honors_env_flags(env, monkeypatch):
    from app import composition

    engine = env["engine"]

    def _factory():
        return Session(bind=engine, autoflush=False, expire_on_commit=False)

    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "0")
    assert composition.credit_loan_jobs_enabled() is False
    result = composition.run_credit_loan_jobs_once(session_factory=_factory)
    assert result == {"auto_debit": {"skipped": "disabled"}, "reminders": {"skipped": "disabled"}}

    monkeypatch.setenv("CREDIT_LOAN_JOBS_ENABLED", "1")
    monkeypatch.setenv("CREDIT_LOANS_AUTO_DEBIT_ENABLED", "0")
    assert composition.credit_loan_auto_debit_enabled() is False
    result = composition.run_credit_loan_jobs_once(
        session_factory=_factory, sender=MockNotificationSender()
    )
    assert result["auto_debit"] == {"skipped": "disabled"}
    assert result["reminders"]["notified"] == []

    assert composition.credit_loan_jobs_interval_seconds() == 3600.0
    monkeypatch.setenv("CREDIT_LOANS_JOBS_INTERVAL_SECONDS", "no-numerico")
    assert composition.credit_loan_jobs_interval_seconds() == 3600.0


def test_processed_events_consumers_are_namespaced(env):
    assert len(AUTO_DEBIT_CONSUMER) <= 60
    assert len(REMINDERS_CONSUMER) <= 60
    assert AUTO_DEBIT_CONSUMER != REMINDERS_CONSUMER
