"""QA de punta a punta de HU12 (E3-T22): desembolso atomico, cuota vencida y debito automatico.

Cubre los CA de HU12 a nivel integracion/API sobre el app real, SIN solapar
las suites de modulo ya existentes:

- E3-T17 (`test_e3_t17_disbursement.py`): desembolso feliz, errores 409/404/422,
  idempotencia misma/distinta clave, fallo ANTES del asiento, lectura, aislamiento.
- E3-T18 (`test_e3_t18_loans.py`): cronograma determinista, errores de dominio,
  migracion, aislamiento del dominio.
- E3-T19 (`test_e3_t19_auto_debit_reminders.py`): toggle, debito de vencida-ayer,
  sin fondos, re-ejecucion mismo dia, recordatorio a 3 dias, ventana configurable.
- E3-T20 (`test_e3_t20_loan_payment.py`): pago feliz, OVERDUE pagable, errores,
  insuficiente en PENDING, idempotencia, ultima cuota, atomicidad del pago.

Lo que ANIADE esta suite (huecos reales):

- CA-01: latencia del desembolso < 30 s; fallo DESPUES del asiento -> rollback
  total + recuperacion hacia adelante con una sola acreditacion; guarda G60
  (`5000` es `asset` en el catalogo sembrado); cuadre global de partida doble
  tras desembolso + pago.
- CA-02: el `GET .../schedule` refleja cuotas `PAID`/`OVERDUE`; cronograma
  consistente tras pagos parciales; vencida + sin fondos -> `REJECTED` intacta
  y reintentable (combinacion no cubierta en E3-T20).
- CA-03: el job cobra la cuota que vence HOY (E3-T19 solo cubre vencida-ayer);
  ciclo completo desembolso -> debito automatico -> pagos manuales -> `PAID`
  sin doble cobro al re-ejecutar el job.
- CA-04: fronteras exactas de la ventana (4 y 1 dia no avisan, 3 si); el aviso
  es unico por ventana aunque avance el dia; una cuota `PAID` en ventana no avisa.
- Seguridad (`docs/15#3`): respuestas sin PII (cuenta enmascarada `****dddd`,
  sin UUID del titular) y sin PII en logs del ciclo completo; `404` neutro
  (ajeno === inexistente, sin montos); aislamiento de los jobs y del motor de
  desembolso (archivos no revisados por E3-T17/E3-T20).

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del repo);
`get_db` y `get_current_user_id` (credits) se inyectan via
`app.dependency_overrides`. Sin red, sin `float`.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from app.modules.credits.jobs.auto_debit import run_auto_debit_once
from app.modules.credits.jobs.installment_reminders import run_installment_reminders_once
from app.modules.credits.models import Loan, LoanSchedule
from app.modules.credits.service import applications as apps_service
from app.modules.credits.service import disbursement as disburse_service
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
            yield {"client": client, "session": session, "own": own.id, "stranger": stranger.id}
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


def _post_pay(client: TestClient, loan_id: str, body: dict | None = None, key: str | None = "k"):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        f"/api/v1/credits/loans/{loan_id}/pay",
        json={} if body is None else body,
        headers=headers,
    )


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


def _today():
    return datetime.now(UTC).date()


# ------------------------------------------------- CA-01: desembolso atomico y cuadre


def test_qa_disburse_latency_under_30s(env):
    """CA-01: el desembolso responde < 30 s tras la firma (regla de HU12)."""
    client, session, own = env["client"], env["session"], env["own"]
    app_id = _signed_app_id(session)
    started = time.perf_counter()
    resp = client.post(
        f"/api/v1/credits/applications/{app_id}/disburse",
        json={"account_id": str(own)},
        headers={"Idempotency-Key": "k-qa-latency"},
    )
    elapsed = time.perf_counter() - started
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["status"] == "SETTLED"
    assert elapsed < 30, f"desembolso tardo {elapsed:.2f}s (>= 30s)"


def test_qa_disburse_failure_after_entry_rolls_back_and_recovers_forward(env, monkeypatch):
    """CA-01: fallo DESPUES del asiento (tras mover el dinero, antes del evento).

    E3-T17 cubre el fallo ANTES del asiento; aqui se inyecta despues de que el
    motor acredita: sin dinero parcial (0 tx, 0 asientos, saldo intacto, sin
    evento) y el reintento con otra clave completa hacia adelante con
    exactamente una acreditacion.
    """
    session, own = env["session"], env["own"]
    app_id = _signed_app_id(session)

    def _boom(*args, **kwargs):
        raise RuntimeError("outbox caido tras el asiento")

    monkeypatch.setattr("app.modules.credits.service.disbursement._record_disbursed_event", _boom)
    with pytest.raises(RuntimeError):
        disburse_service.disburse_application_idempotent(
            session, USER, app_id, account_id=str(own), idempotency_key="k-qa-after"
        )
    session.rollback()
    assert _count(session, "transactions.transactions") == 0
    assert _count(session, "ledger.journal_entries") == 0
    assert _count(session, "ledger.postings") == 0
    assert _avail(session, own) == FUNDED
    assert _outbox_by_type(session, "credit.disbursed") == []

    monkeypatch.undo()
    view, replayed = disburse_service.disburse_application_idempotent(
        session, USER, app_id, account_id=str(own), idempotency_key="k-qa-after-retry"
    )
    session.commit()
    assert replayed is True
    assert view["status"] == "SETTLED"
    assert _avail(session, own) == FUNDED + AMOUNT
    assert _count(session, "credits.loans") == 1
    assert _count(session, "transactions.transactions") == 1
    assert _count(session, "ledger.journal_entries") == 1
    assert len(_outbox_by_type(session, "credit.disbursed")) == 1


def test_qa_seeded_catalog_5000_is_asset_guard(env):
    """CA-01/G60: la QA falla si la `5000` no es `asset` (asiento 7.5 valido)."""
    from app.modules.ledger.repository import get_by_code

    session = env["session"]
    portfolio = get_by_code(session, "5000")
    assert portfolio is not None
    assert portfolio.type == "asset", f"5000 es {portfolio.type!r}, el asiento 7.5 seria invalido"
    assert portfolio.name == "Cartera de prestamos"

    loan_id = _disbursed_loan_id(session, env["client"], env["own"], key="k-qa-g60")
    from app.modules.ledger import service as ledger_service
    from app.modules.ledger.models import JournalEntry

    (entry,) = list(session.scalars(sa.select(JournalEntry)).all())
    postings = ledger_service.list_postings(session, entry.id)
    debit = next(p for p in postings if p.direction == "DEBIT")
    assert debit.ledger_account_id == portfolio.id
    assert str(_loan(session, loan_id).id) == loan_id


def test_qa_global_double_entry_squares_after_disburse_and_pay(env):
    """CA-01/CA-02: cuadre global `sum(debitos)=sum(creditos)` tras desembolso + pago."""
    from app.modules.ledger.models import JournalEntry

    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-qa-square-disb")
    pay = _post_pay(client, loan_id, {}, key="k-qa-square-pay")
    assert pay.status_code == 200, pay.text

    from app.modules.ledger import service as ledger_service

    entries = list(session.scalars(sa.select(JournalEntry)).all())
    assert {e.entry_type for e in entries} == {"LOAN_DISBURSEMENT", "LOAN_INSTALLMENT"}
    total_debit = total_credit = 0
    for entry in entries:
        postings = ledger_service.list_postings(session, entry.id)
        debit = sum(int(p.amount_minor) for p in postings if p.direction == "DEBIT")
        credit = sum(int(p.amount_minor) for p in postings if p.direction == "CREDIT")
        assert debit == credit > 0, entry.entry_type
        total_debit += debit
        total_credit += credit
    assert total_debit == total_credit
    first = _schedules(session, uuid.UUID(loan_id))[0]
    assert total_debit == AMOUNT + int(first.total_minor)


# ------------------------------------------------- CA-02: cronograma y cuota vencida


def test_qa_schedule_reflects_paid_and_overdue(env):
    """CA-02: el `GET .../schedule` refleja cuotas `PAID`/`OVERDUE` y sigue cuadrado."""
    from app.modules.credits.service import loans as loans_service

    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-qa-sched")
    rows = _schedules(session, uuid.UUID(loan_id))

    paid = _post_pay(client, loan_id, {"schedule_id": str(rows[0].id)}, key="k-qa-sched-pay")
    assert paid.status_code == 200, paid.text
    rows[1].due_date = _today() - timedelta(days=5)
    session.commit()
    assert loans_service.flag_overdue_installments(session, as_of=_today()) >= 1
    session.commit()

    detail = client.get(f"/api/v1/credits/loans/{loan_id}/schedule")
    assert detail.status_code == 200, detail.text
    schedule = detail.json()["data"]["schedule"]
    assert len(schedule) == TERM
    by_no = {r["installment_no"]: r for r in schedule}
    assert by_no[1]["status"] == "PAID" and by_no[1]["paid_at"] is not None
    assert by_no[2]["status"] == "OVERDUE" and by_no[2]["paid_at"] is None
    for row in schedule:
        assert (
            row["principal_minor"] + row["interest_minor"] + row["insurance_minor"]
            == row["total_minor"]
        )
    assert sum(r["principal_minor"] for r in schedule) == AMOUNT


def test_qa_overdue_without_funds_rejected_intact_and_retryable(env):
    """CA-02: vencida + sin fondos -> `REJECTED` sin cambios y reintentable.

    E3-T20 cubre insuficiente sobre `PENDING`; aqui la cuota es `OVERDUE`
    (sigue intacta como `OVERDUE`, no se resetea) y el rechazo se reintenta
    con la misma clave sin mover dinero.
    """
    from app.modules.accounts import repository as accounts_repo
    from app.modules.credits.service import loans as loans_service

    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-qa-od-poor-disb")
    first = _schedules(session, uuid.UUID(loan_id))[0]
    first.due_date = _today() - timedelta(days=10)
    session.commit()
    assert loans_service.flag_overdue_installments(session, as_of=_today()) >= 1
    session.commit()
    assert session.get(LoanSchedule, first.id).status == "OVERDUE"

    accounts_repo.apply_delta(
        session, own, available_delta=100 - _avail(session, own), held_delta=0, currency="PEN"
    )
    session.commit()

    resp = _post_pay(client, loan_id, {}, key="k-qa-od-poor")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "REJECTED" and data["failure_code"] == "INSUFFICIENT_FUNDS"

    assert _avail(session, own) == 100
    assert session.get(LoanSchedule, first.id).status == "OVERDUE"
    assert _count(session, "credits.loan_payments") == 0
    assert _count(session, "ledger.journal_entries") == 1
    assert _outbox_by_type(session, "loan.installment.paid") == []

    again = _post_pay(client, loan_id, {}, key="k-qa-od-poor")
    assert again.status_code == 200
    assert again.json()["meta"]["replayed"] is True
    assert again.json()["data"] == data
    assert _avail(session, own) == 100


# ------------------------------------------------- CA-03: debito automatico punta a punta


def test_qa_auto_debit_collects_installment_due_today(env):
    """CA-03: el job cobra la cuota que vence HOY (`due_date <= hoy`).

    E3-T19 solo cubre vencida-ayer; aqui `due_date == hoy` con el flag activo.
    """
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-qa-debit-today")
    assert (
        client.put(
            f"/api/v1/credits/loans/{loan_id}/auto-debit", json={"enabled": True}
        ).status_code
        == 200
    )
    first = _schedules(session, uuid.UUID(loan_id))[0]
    first.due_date = _today()
    session.commit()
    prior_avail = _avail(session, own)

    result = run_auto_debit_once(session, today=_today(), sender=MockNotificationSender())

    assert result["paid"] == [str(first.id)]
    assert session.get(LoanSchedule, first.id).status == "PAID"
    assert _avail(session, own) == prior_avail - int(first.total_minor)
    assert _count(session, "credits.loan_payments") == 1


def test_qa_full_cycle_autodebit_then_manual_payoff_closes_loan(env):
    """CA-01/02/03: ciclo completo desembolso -> debito automatico -> pagos -> `PAID`.

    La primera cuota (vencida) la cobra el job; el resto se paga por el endpoint
    por defecto (proxima exigible); re-ejecutar el job no duplica cobros y el
    prestamo cierra con `outstanding=0` y ambos eventos emitidos.
    """
    from app.modules.accounts import repository as accounts_repo

    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-qa-cycle")
    assert (
        client.put(
            f"/api/v1/credits/loans/{loan_id}/auto-debit", json={"enabled": True}
        ).status_code
        == 200
    )
    rows = _schedules(session, uuid.UUID(loan_id))
    total = sum(int(r.total_minor) for r in rows)
    accounts_repo.apply_delta(session, own, available_delta=total, held_delta=0, currency="PEN")
    rows[0].due_date = _today() - timedelta(days=2)
    session.commit()

    sender = MockNotificationSender()
    first_run = run_auto_debit_once(session, today=_today(), sender=sender)
    assert first_run["paid"] == [str(rows[0].id)]
    assert session.get(LoanSchedule, rows[0].id).status == "PAID"

    for pos in range(1, TERM):
        resp = _post_pay(client, loan_id, {}, key=f"k-qa-cycle-{pos}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "SETTLED"

    loan = _loan(session, loan_id)
    assert loan.status == "PAID"
    assert int(loan.outstanding_principal_minor) == 0
    assert _count(session, "credits.loan_payments") == TERM
    assert len(_outbox_by_type(session, "credit.disbursed")) == 1
    assert len(_outbox_by_type(session, "loan.installment.paid")) == TERM

    rerun = run_auto_debit_once(session, today=_today(), sender=sender)
    assert rerun["paid"] == [] and rerun["failed"] == []
    assert _count(session, "credits.loan_payments") == TERM


# ------------------------------------------------- CA-04: recordatorio, ventana exacta y unica


def test_qa_reminder_exact_window_boundaries(env):
    """CA-04: solo `due == hoy+3` avisa (4 y 1 dia no; E3-T19 no cubre el borde 4/1)."""
    client, session = env["client"], env["session"]
    loan_id = _disbursed_loan_id(session, client, env["own"], key="k-qa-rem-bound")
    rows = _schedules(session, uuid.UUID(loan_id))
    today = _today()
    rows[0].due_date = today + timedelta(days=4)  # justo fuera
    rows[1].due_date = today + timedelta(days=3)  # en ventana
    rows[2].due_date = today + timedelta(days=1)  # justo fuera
    session.commit()

    result = run_installment_reminders_once(session, today=today, sender=MockNotificationSender())

    assert result["days_before"] == 3
    assert result["notified"] == [str(rows[1].id)]
    assert result["failed"] == []


def test_qa_reminder_once_per_window_and_paid_excluded(env):
    """CA-04: el aviso es unico por ventana aunque avance el dia; la `PAID` no avisa."""
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-qa-rem-once")
    rows = _schedules(session, uuid.UUID(loan_id))
    today = _today()
    rows[0].due_date = today + timedelta(days=4)
    rows[1].due_date = today + timedelta(days=3)
    session.commit()
    sender = MockNotificationSender()

    first = run_installment_reminders_once(session, today=today, sender=sender)
    assert first["notified"] == [str(rows[1].id)]

    paid = _post_pay(client, loan_id, {"schedule_id": str(rows[1].id)}, key="k-qa-rem-pay")
    assert paid.status_code == 200, paid.text

    next_day = today + timedelta(days=1)
    second = run_installment_reminders_once(session, today=next_day, sender=sender)
    # rows[0] entra en ventana (hoy+3) y avisa; rows[1] esta PAID y no re-avisa.
    assert second["notified"] == [str(rows[0].id)]
    third = run_installment_reminders_once(session, today=next_day, sender=sender)
    assert third["notified"] == []
    assert third["skipped"] == [str(rows[0].id)]

    from app.modules.notifications.models import Notification

    notes = list(
        session.scalars(
            sa.select(Notification).where(Notification.template_code == "loan_installment_reminder")
        ).all()
    )
    assert len(notes) == 2
    assert all(n.status == "SENT" and n.channel == "push" for n in notes)


# ------------------------------------------------- Seguridad: PII, 404 neutro, aislamiento


def test_qa_no_pii_in_responses_or_logs_during_full_cycle(env, caplog):
    """Sin PII en respuestas ni logs del ciclo desembolso -> pago; cuenta enmascarada."""
    client, session, own = env["client"], env["session"], env["own"]
    with caplog.at_level(logging.INFO):
        app_id = _signed_app_id(session)
        disb = client.post(
            f"/api/v1/credits/applications/{app_id}/disburse",
            json={"account_id": str(own)},
            headers={"Idempotency-Key": "k-qa-pii"},
        )
        assert disb.status_code == 201, disb.text
        loan_id = disb.json()["data"]["loan_id"]
        listed = client.get("/api/v1/credits/loans")
        assert listed.status_code == 200
        detail = client.get(f"/api/v1/credits/loans/{loan_id}/schedule")
        assert detail.status_code == 200
        pay = _post_pay(client, loan_id, {}, key="k-qa-pii-pay")
        assert pay.status_code == 200, pay.text

    bodies = [disb.text, listed.text, detail.text, pay.text]
    for text in bodies:
        dumped = json.dumps(json.loads(text))
        assert ACCOUNT_NUMBER not in dumped
        assert OTHER_ACCOUNT_NUMBER not in dumped
        assert str(USER) not in dumped, "UUID del titular en respuesta"
    assert disb.json()["data"]["account_masked"] == f"****{ACCOUNT_NUMBER[-4:]}"
    assert "account_number" not in listed.text.replace("account_number_masked", "")

    assert ACCOUNT_NUMBER not in caplog.text
    assert OTHER_ACCOUNT_NUMBER not in caplog.text
    assert PIN not in caplog.text


def test_qa_foreign_and_unknown_404_are_neutral(env):
    """`404` neutro: recurso ajeno e inexistente responden identico, sin montos."""
    client, session, own = env["client"], env["session"], env["own"]
    loan_id = _disbursed_loan_id(session, client, own, key="k-qa-404")
    _CURRENT["user"] = OTHER
    try:
        foreign = _post_pay(client, loan_id, {}, key="k-qa-404-foreign")
    finally:
        _CURRENT["user"] = USER
    unknown = _post_pay(client, str(uuid.uuid4()), {}, key="k-qa-404-unknown")

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["error"]["code"] == unknown.json()["error"]["code"] == "NOT_FOUND"
    assert set(foreign.json()) == set(unknown.json())
    for resp in (foreign, unknown):
        dumped = json.dumps(resp.json())
        assert str(AMOUNT) not in dumped
        assert loan_id not in dumped
    assert _count(session, "credits.loan_payments") == 0


def test_qa_jobs_and_disbursement_motor_keep_isolation():
    """Aislamiento: los jobs y el motor de desembolso no tocan tablas ajenas.

    E3-T17/E3-T20 revisan `disbursement.py`/`loan_payments.py` de credits; aqui
    se revisan los archivos no cubiertos: ambos jobs, el `notify` y el motor
    `transactions.service.disbursement` (nunca importa `credits`).
    """
    backend_dir = Path(__file__).resolve().parents[1]
    jobs_dir = backend_dir / "app" / "modules" / "credits" / "jobs"
    for name in ("auto_debit.py", "installment_reminders.py", "notify.py"):
        source = (jobs_dir / name).read_text(encoding="utf-8")
        for forbidden in (
            "journal_entries",
            "postings",
            "Posting",
            "post_entry",
            "apply_delta",
            "accounts.repository",
            "accounts.models",
            "ledger.repository",
            "ledger.models",
            "transactions.repository",
            "transactions.models",
        ):
            assert forbidden not in source, f"{name}: {forbidden}"
    motor = (
        backend_dir / "app" / "modules" / "transactions" / "service" / "disbursement.py"
    ).read_text(encoding="utf-8")
    assert "credits.models" not in motor
    assert "credits.repository" not in motor
