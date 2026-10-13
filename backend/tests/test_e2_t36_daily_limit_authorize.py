"""Cierre del bypass del limite diario (E2-T36, HU08; afecta HU06).

Dos medidas de la decision del dueno (2026-10-09):

1. Las pendientes (`PENDING_AUTHORIZATION`) SI cuentan en el acumulado
   diario (`EXCLUDED_ACCUMULATED_STATUSES = ("REJECTED", "FAILED",
   "REVERSED")`): encolar N operaciones sobre el umbral ya no evade el
   limite (la que excede se rechaza en la iniciacion).
2. Re-chequeo del limite diario al autorizar (`PENDING_AUTHORIZATION ->
   AUTHORIZED`, firma/PIN/interbancaria, centralizado en
   `biometric._continue_after_authorized` /
   `interbank.resume_after_authorization`): si a ese instante
   `acumulado + total > daily_limit`, la operacion queda `REJECTED`
   (`DAILY_LIMIT_EXCEEDED`) sin liquidar, con el hold liberado mediante
   el asiento `HOLD_RELEASE` ya existente (idempotente; sin asientos
   nuevos).

Sobre el motor y el ledger con adaptadores reales
(`ACCOUNTS_BALANCE_PORT`, fachada real `shared.config`, E2-T18): dinero de
juguete (UUIDs sinteticos, sin PII), SQLite en memoria + schemas ATTACH,
sin red, sin `float`.

Mapeo brief -> test:
- encolado: N pendientes sobre el umbral hasta exceder; la que excede ->
  `REJECTED`/`DAILY_LIMIT_EXCEEDED` sin hold y las pendientes SI cuentan
  (CA-01/CA-04) .............. `test_enqueue_pending_counts_toward_limit`
- re-chequeo en `authorize` (firma): pendiente bajo el limite + otra
  operacion del dia que hace crecer el acumulado -> `REJECTED`,
  hold `RELEASED`, disponible restaurado, `HOLD_RELEASE` presente, sin
  `TRANSFER_SETTLE` ni `auth.failed_attempt`
  (CA-02) ..................... `test_authorize_rechecks_daily_limit_signature`
- re-chequeo en `authorize` (PIN), misma ruta
  (CA-02) ..................... `test_authorize_rechecks_daily_limit_pin`
- paridad: propia (atomica, sin hold), tercero (hold) e interbancaria
  (hold) sobre el mismo limite -> las tres `REJECTED` con el mismo
  codigo y sin mover dinero
  (CA-05) ..................... `test_parity_across_routes_at_authorize`
- caso normal intacto: pendiente bajo el limite -> `authorize` liquida
  (CA-03) ..................... `test_authorize_under_limit_still_settles`
- la rechazada por limite no infla el acumulado
  (CA-04) ..................... (dentro de `test_enqueue_*` y `test_*_signature`)
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE_A = "dev-e2t36-limit"
PIN = "4829"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

THRESHOLD = 2_000
DAILY_LIMIT = 10_000

TABLES = [
    "accounts.accounts",
    "accounts.account_balances",
    "accounts.daily_balance_snapshots",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
]


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # registro de tablas
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s
    import app.modules.transactions.models as _t

    assert _a is not None and _l is not None and _s is not None and _t is not None
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions", "config"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    return engine


def _seed_param(session: Session, key: str, value: str, module: str = "transactions") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


def _set_param(session: Session, key: str, value: int) -> None:
    session.execute(
        sa.text("UPDATE config.parameters SET value_json = :value WHERE key = :key"),
        {"value": str(value), "key": key},
    )
    session.flush()


@pytest.fixture(autouse=True)
def _clean_holder_cache():
    from app.modules.transactions import service as svc

    svc.clear_holder_cache()
    yield
    svc.clear_holder_cache()


@pytest.fixture()
def t36_session():
    """Sesion SQLite aislada: umbral 2000 / limite diario 10000."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00990001111", type="AHORRO", currency="PEN"
        )
        a2 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00990002222", type="CORRIENTE", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00880003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a2.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, str(THRESHOLD))
        _seed_param(session, DAILY_KEY, str(DAILY_LIMIT))
        _seed_param(session, DAILY_STANDARD_KEY, str(DAILY_LIMIT))
        _seed_param(session, MAX_ATTEMPTS_KEY, "5", module="identity")
        session.commit()
        yield {"session": session, "a1": a1.id, "a2": a2.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


class FakeBiometry:
    """Puertos de fachada `identity` en memoria (sin tablas `identity`)."""

    def __init__(self) -> None:
        self.bindings: dict[tuple[str, str], dict] = {}

    def add_binding(self, user_id: uuid.UUID, device_id: str, *, status: str = "ACTIVE") -> None:
        self.bindings[(str(user_id), device_id)] = {
            "device_id": device_id,
            "public_key": "hmac:" + "ab" * 16,
            "status": status,
        }

    def binding_reader(self, session: Session, user_id: uuid.UUID, device_id: str):
        found = self.bindings.get((str(user_id), device_id))
        return dict(found) if found is not None else None

    @staticmethod
    def sign(nonce: str) -> str:
        return f"SIG:{nonce}"

    def verifier(self, nonce: str, signature: str, public_key: str) -> bool:
        return bool(nonce) and signature == f"SIG:{nonce}" and bool(public_key)


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _avail(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _holds(session, tx_id):
    from app.modules.transactions import repository as tx_repo

    return list(tx_repo.list_holds_by_transaction(session, tx_id))


def _tx_entries(session, tx_id):
    from app.modules.ledger.models import JournalEntry

    return list(
        session.scalars(sa.select(JournalEntry).where(JournalEntry.transaction_id == tx_id)).all()
    )


def _entry_types(session, tx_id) -> list[str]:
    return sorted(e.entry_type for e in _tx_entries(session, tx_id))


def _outbox(session, tx_id):
    from app.modules.shared.models import OutboxEntry

    return list(
        session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    )


def _security_events(session, tx_id) -> list[dict]:
    return [r.payload for r in _outbox(session, tx_id) if r.event_type == "auth.failed_attempt"]


def _history_reasons(session, tx_id) -> list[str]:
    from app.modules.transactions import repository as tx_repo

    return [h.reason or "" for h in tx_repo.list_history(session, tx_id)]


def _third_party(session, a1, b1, amount, **overrides):
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "target_account_id": b1,
        "amount_minor": amount,
        "currency": "PEN",
        "fee_minor": 0,
        "idempotency_key": f"e2t36-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return svc.transfer_third_party(session, **params)


def _own(session, a1, a2, amount, **overrides):
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "target_account_id": a2,
        "amount_minor": amount,
        "currency": "PEN",
        "idempotency_key": f"e2t36-own-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return svc.transfer_own(session, **params)


CCI_OK = "00200100200100200101"
BANK_CODE = "BCP01"


def _interbank(session, a1, gateway, amount, **overrides):
    from app.adapters.holder_lookup import MockHolderLookupProvider, mock_holder_name_for
    from app.modules.transactions.service import interbank as saga

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "amount_minor": amount,
        "currency": "PEN",
        "cci": CCI_OK,
        "holder_name": mock_holder_name_for(CCI_OK),
        "bank_code": BANK_CODE,
        "fee_minor": 200,
        "idempotency_key": f"e2t36-ib-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "gateway": gateway,
        "holder_provider": MockHolderLookupProvider(mode="success"),
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return saga.transfer_interbank(session, **params)


def _confirm_gateway():
    from app.adapters.interbank_gateway import MockInterbankGateway

    return MockInterbankGateway(default_send="confirm")


def _authorize(session, tx_id, *, bio: FakeBiometry, device: str = DEVICE_A, **overrides):
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    tx = tx_repo.get_transaction(session, tx_id)
    nonce = (bio_svc.get_block(tx) or {}).get("nonce") or ""
    params = {
        "device_id": device,
        "signature": bio.sign(nonce),
        "balance_port": _real_port(),
        "binding_reader": bio.binding_reader,
        "signature_verifier": bio.verifier,
    }
    params.update(overrides)
    return bio_svc.authorize_transaction(session, tx_id, **params)


def _authorize_pin(session, tx_id, *, pin: str = PIN, **overrides):
    from app.modules.transactions.service import biometric as bio_svc

    params = {
        "pin": pin,
        "balance_port": _real_port(),
        "pin_verifier": lambda _s, _u, presented: presented == PIN,
    }
    params.update(overrides)
    return bio_svc.authorize_transaction_with_pin(session, tx_id, **params)


def _seed_settled(session, amount: int):
    """Registra otra operacion del dia ya liquidada (simula un canal que no
    pasa por el gate con el acumulado actual: p. ej. backoffice/conciliacion).

    Solo transiciones validas por la maquina; sin movimientos de dinero
    (no altera saldos: el acumulado es contable, no la caja del test).
    """
    from app.modules.transactions import repository as tx_repo

    tx = tx_repo.create_transaction(
        session,
        type="THIRD_PARTY_TRANSFER",
        amount_minor=amount,
        currency="PEN",
        status="INITIATED",
        idempotency_key=f"e2t36-seed-{uuid.uuid4()}",
        initiator_user_id=USER_A,
    )
    for status in ("VALIDATED", "AUTHORIZED", "POSTED", "SETTLED"):
        tx = tx_repo.transition_transaction(session, tx.id, status)
    session.commit()
    return tx


# --------------------------------- CA-01/CA-04: encolar pendientes ya no evade
def test_enqueue_pending_counts_toward_limit(t36_session):
    """CA-01/CA-04: las pendientes suman; la que excede se rechaza al iniciar."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t36_session["session"], t36_session["a1"], t36_session["b1"]
    pendings = []
    for _ in range(3):
        tx = _third_party(session, a1, b1, 3_000)
        session.commit()
        assert tx.status == "PENDING_AUTHORIZATION"
        pendings.append(tx)
    accumulated = bio_svc.daily_accumulated_minor(session, USER_A)
    assert accumulated == 9_000  # 3 x 3000: las pendientes SI cuentan (medida 1)
    fourth = _third_party(session, a1, b1, 3_000)  # 9000 + 3000 > 10000
    session.commit()
    assert fourth.status == "REJECTED"
    assert fourth.failure_reason == bio_svc.DAILY_LIMIT_EXCEEDED
    assert (fourth.meta or {}).get("failure_code") == bio_svc.DAILY_LIMIT_EXCEEDED
    assert _holds(session, fourth.id) == []
    assert _tx_entries(session, fourth.id) == []
    assert "limite diario excedido" in _history_reasons(session, fourth.id)
    # CA-04: la rechazada no infla el acumulado; aun cabe una bajo el umbral.
    assert bio_svc.daily_accumulated_minor(session, USER_A) == 9_000
    small = _third_party(session, a1, b1, 1_000)  # 9000 + 1000 <= 10000
    session.commit()
    assert small.status == "SETTLED"


# --------------------------------- CA-02: re-chequeo al autorizar (firma)
def test_authorize_rechecks_daily_limit_signature(t36_session):
    """CA-02: pendiente bajo el limite + crecimiento del acumulado ->
    `authorize` (firma) rechaza sin liquidar y libera el hold."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t36_session["session"], t36_session["a1"], t36_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1, 3_000)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    assert _avail(session, a1) == 100_000 - 3_000  # hold retenido
    _seed_settled(session, 8_000)  # el dia crece: 3000 + 8000 > 10000
    assert bio_svc.daily_accumulated_minor(session, USER_A, exclude_tx_id=tx.id) == 8_000
    done = _authorize(session, tx.id, bio=bio)
    session.commit()
    assert done.status == "REJECTED"
    assert done.failure_reason == bio_svc.DAILY_LIMIT_EXCEEDED
    assert (done.meta or {}).get("failure_code") == bio_svc.DAILY_LIMIT_EXCEEDED
    assert "limite diario excedido" in _history_reasons(session, tx.id)
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "RELEASED"
    assert _avail(session, a1) == 100_000  # disponible restaurado
    assert _avail(session, b1) == 50_000  # destino nunca tocado
    types = _entry_types(session, tx.id)
    assert "HOLD_RELEASE" in types  # asiento de liberacion ya existente
    assert "TRANSFER_SETTLE" not in types  # sin liquidar
    assert _security_events(session, tx.id) == []  # no es fallo de factor
    # CA-04: la rechazada no infla el acumulado (queda solo lo sembrado).
    assert bio_svc.daily_accumulated_minor(session, USER_A) == 8_000


# --------------------------------- CA-02: re-chequeo al autorizar (PIN)
def test_authorize_rechecks_daily_limit_pin(t36_session):
    """CA-02 (via PIN): mismo re-chequeo y mismo rechazo que con firma."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t36_session["session"], t36_session["a1"], t36_session["b1"]
    tx = _third_party(session, a1, b1, 3_000)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    _seed_settled(session, 8_000)
    done = _authorize_pin(session, tx.id)
    session.commit()
    assert done.status == "REJECTED"
    assert done.failure_reason == bio_svc.DAILY_LIMIT_EXCEEDED
    assert (done.meta or {}).get("failure_code") == bio_svc.DAILY_LIMIT_EXCEEDED
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "RELEASED"
    assert _avail(session, a1) == 100_000
    assert _avail(session, b1) == 50_000
    types = _entry_types(session, tx.id)
    assert "HOLD_RELEASE" in types
    assert "TRANSFER_SETTLE" not in types
    assert _security_events(session, tx.id) == []


# --------------------------------- CA-05: paridad de rutas al autorizar
def test_parity_across_routes_at_authorize(t36_session):
    """CA-05: propia (atomica, sin hold), tercero e interbancaria (con hold)
    terminan `REJECTED` con el mismo codigo y sin mover dinero."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, a2, b1 = (
        t36_session["session"],
        t36_session["a1"],
        t36_session["a2"],
        t36_session["b1"],
    )
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    gw = _confirm_gateway()
    own = _own(session, a1, a2, 3_000)
    third = _third_party(session, a1, b1, 3_000)
    ib = _interbank(session, a1, gw, 3_000)  # total 3200 con comision
    session.commit()
    assert own.status == "PENDING_AUTHORIZATION"
    assert third.status == "PENDING_AUTHORIZATION"
    assert ib.status == "PENDING_AUTHORIZATION"
    assert _holds(session, own.id) == []  # atomica: nada retenido
    _seed_settled(session, 8_000)  # 8000 + 3000 > 10000 en las tres
    avail_before = _avail(session, a1)
    for tx_id in (own.id, third.id, ib.id):
        done = _authorize(session, tx_id, bio=bio)
        session.commit()
        assert done.status == "REJECTED"
        assert done.failure_reason == bio_svc.DAILY_LIMIT_EXCEEDED
        assert (done.meta or {}).get("failure_code") == bio_svc.DAILY_LIMIT_EXCEEDED
    assert _holds(session, own.id) == []
    assert _tx_entries(session, own.id) == []  # atomica: ningun asiento
    for tx_id in (third.id, ib.id):
        holds = _holds(session, tx_id)
        assert len(holds) == 1 and holds[0].status == "RELEASED"
        assert "HOLD_RELEASE" in _entry_types(session, tx_id)
    assert "TRANSFER_SETTLE" not in _entry_types(session, third.id)
    assert "INTERBANK_SEND" not in _entry_types(session, ib.id)
    assert "INTERBANK_SETTLE" not in _entry_types(session, ib.id)
    assert _avail(session, a1) == avail_before + 3_000 + 3_200  # holds devueltos
    assert _avail(session, b1) == 50_000
    assert _avail(session, a2) == 50_000


# --------------------------------- CA-03: caso normal intacto
def test_authorize_under_limit_still_settles(t36_session):
    """CA-03: pendiente que cabe en el limite se autoriza y liquida igual."""
    session, a1, b1 = t36_session["session"], t36_session["a1"], t36_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1, 3_000)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    done = _authorize(session, tx.id, bio=bio)
    session.commit()
    assert done.status == "SETTLED"
    assert _entry_types(session, tx.id) == ["TRANSFER_HOLD", "TRANSFER_SETTLE"]
    assert _avail(session, a1) == 100_000 - 3_000
    assert _avail(session, b1) == 50_000 + 3_000


# --------------------------------- Regresion: el hold liberado usa el puerto inyectado
def test_daily_limit_reject_release_uses_injected_balance_port(t36_session):
    """Regresion E2-T36 (obs. BAJA validador): el rechazo por limite diario
    al autorizar debe liberar el hold con el `balance_port` inyectado en
    `authorize_transaction` (igual que `_fail_attempt`), no con el puerto
    por defecto. Sin el fix el espia no registra la devolucion y falla."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = t36_session["session"], t36_session["a1"], t36_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1, 3_000)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    _seed_settled(session, 8_000)  # 3000 + 8000 > 10000 -> REJECTED al autorizar

    real = _real_port()
    calls: list[tuple[str, int, str]] = []

    class SpyBalancePort:
        def lock_and_get(self, session, account_id):
            return real.lock_and_get(session, account_id)

        def apply_delta(self, session, account_id, delta_minor, currency):
            calls.append((str(account_id), int(delta_minor), str(currency)))
            return real.apply_delta(session, account_id, delta_minor, currency)

    done = _authorize(session, tx.id, bio=bio, balance_port=SpyBalancePort())
    session.commit()
    assert done.status == "REJECTED"
    assert done.failure_reason == bio_svc.DAILY_LIMIT_EXCEEDED
    holds = _holds(session, tx.id)
    assert len(holds) == 1 and holds[0].status == "RELEASED"
    releases = [c for c in calls if c[0] == str(a1) and c[1] == 3_000 and c[2] == "PEN"]
    assert releases, "release_pending_holds no uso el balance_port inyectado"
