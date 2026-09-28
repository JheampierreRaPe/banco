"""Tope pre-aprobado por beneficiario integrado al motor (E2-T14, HU07 CA-03).

Sobre el motor y el ledger con adaptadores reales (`ACCOUNTS_BALANCE_PORT`,
cuentas por `accounts.repository`, tope por la fachada
`accounts.service.beneficiaries.get_for_operation`) y fachada real de
parametros (`shared.config`, E2-T18): dinero de juguete (UUIDs sinteticos,
sin PII), SQLite en memoria + schemas ATTACH, sin red, sin `float`.

Mapeo brief -> test:
- bajo tope -> `AUTHORIZED` directo y `SETTLED` sin `PENDING` ... `test_under_limit_*`
- sobre tope -> ruta de autorizacion (biometria exigible) ..... `test_over_limit_*`
- sin tope / beneficiario normal -> como hoy .................. `test_no_limit_*`
- moneda distinta -> `VALIDATION_ERROR` ....................... `test_currency_*`
- beneficiario inexistente/ajeno -> sin exencion .............. `test_unknown_*`
- limite diario y `user=None` no se omiten .................... `test_daily_*`
- idempotencia: reintento no duplica ni reevalua .............. `test_idempotency_*`
- concurrencia (D-13): dentro + fuera del tope consistentes .... `test_concurrent_*`
- frontera: sin modelos de `accounts`, solo fachada ........... `test_facade_*`
- sin PII en logs ............................................. `test_no_pii_*`
- via `metadata["beneficiary_id"]` (contrato HTTP intacto) .... `test_metadata_*`
"""

from __future__ import annotations

import logging
import threading
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE_A = "dev-preapproved-1"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

ALIAS_UNICO = "XqzAliasUnicoT14"
CCI_UNICO = "00110001111111111111"

TABLES = [
    "accounts.accounts",
    "accounts.account_balances",
    "accounts.daily_balance_snapshots",
    "accounts.beneficiaries",
    "accounts.beneficiary_search_tokens",
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
                "module TEXT NOT NULL, description TEXT, updated_at TEXT, "
                "created_at TEXT, updated_by TEXT)"
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


@pytest.fixture()
def t14_session():
    """Sesion SQLite aislada con cuentas fondeadas y parametros E2-T18."""
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
            session, a1.id, available_delta=200_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a2.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, "2000")
        _seed_param(session, DAILY_KEY, "500000")
        _seed_param(session, DAILY_STANDARD_KEY, "500000")
        _seed_param(session, MAX_ATTEMPTS_KEY, "5", module="identity")
        session.commit()
        yield {"session": session, "engine": engine, "a1": a1.id, "a2": a2.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


def _real_port():
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT

    return ACCOUNTS_BALANCE_PORT


def _avail(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _make_beneficiary(session, owner=USER_A, **overrides):
    from app.modules.accounts.service import beneficiaries as beneficiaries_service

    body = {"alias": ALIAS_UNICO, "account_or_cci": CCI_UNICO}
    body.update(overrides)
    return beneficiaries_service.create_beneficiary(session, owner, **body)


def _third_party(session, a1, b1, amount, **overrides):
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "target_account_id": b1,
        "amount_minor": amount,
        "currency": "PEN",
        "fee_minor": 0,
        "idempotency_key": f"e2t14-{uuid.uuid4()}",
        "device_id": DEVICE_A,
        "balance_port": _real_port(),
    }
    params.update(overrides)
    return svc.transfer_third_party(session, **params)


def _history_reasons(session, tx_id) -> list[str]:
    from app.modules.transactions import repository as tx_repo

    return [h.reason or "" for h in tx_repo.list_history(session, tx_id)]


def _initiated_events(session, tx_id) -> list[dict]:
    from app.modules.shared.models import OutboxEntry

    rows = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    return [r.payload for r in rows if r.event_type == "transaction.initiated"]


# ------------------------------------------------------- Parte A: regla pura


def test_pure_under_and_equal_limit_exempt():
    from app.modules.transactions.service import biometric as bio_svc

    assert (
        bio_svc.is_preapproved_exempt(
            limit_minor=50_000, limit_currency="PEN", total_minor=10_000, currency="PEN"
        )
        is True
    )
    assert (
        bio_svc.is_preapproved_exempt(
            limit_minor=10_000, limit_currency="PEN", total_minor=10_000, currency="PEN"
        )
        is True
    )


def test_pure_over_limit_not_exempt():
    from app.modules.transactions.service import biometric as bio_svc

    assert (
        bio_svc.is_preapproved_exempt(
            limit_minor=50_000, limit_currency="PEN", total_minor=50_001, currency="PEN"
        )
        is False
    )


def test_pure_no_limit_or_no_currency_not_exempt():
    from app.modules.transactions.service import biometric as bio_svc

    assert (
        bio_svc.is_preapproved_exempt(
            limit_minor=None, limit_currency="PEN", total_minor=100, currency="PEN"
        )
        is False
    )
    assert (
        bio_svc.is_preapproved_exempt(
            limit_minor=50_000, limit_currency=None, total_minor=100, currency="PEN"
        )
        is False
    )


def test_pure_currency_mismatch_raises_validation():
    from app.modules.transactions.service import biometric as bio_svc

    with pytest.raises(bio_svc.PreapprovedCurrencyMismatchError):
        bio_svc.is_preapproved_exempt(
            limit_minor=50_000, limit_currency="USD", total_minor=100, currency="PEN"
        )
    # La API lo traduce a `VALIDATION_ERROR` (400): es `ValueError`.
    assert issubclass(bio_svc.PreapprovedCurrencyMismatchError, ValueError)


# ------------------------------------------------- Parte B: motor + fachada


def test_under_limit_skips_pending_and_settles(t14_session):
    from app.modules.transactions.service import biometric as bio_svc

    session = t14_session["session"]
    view = _make_beneficiary(
        session, preapproved_limit_minor=50_000, currency="PEN", holder_name="Titular Unico"
    )
    session.commit()
    tx = _third_party(
        session, t14_session["a1"], t14_session["b1"], 10_000, beneficiary_id=view["id"]
    )
    session.commit()
    assert tx.status == "SETTLED"
    reasons = _history_reasons(session, tx.id)
    assert bio_svc.PREAPPROVED_EXEMPT_REASON in reasons
    assert bio_svc.REQUIRED_REASON not in reasons
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    block = meta.get(bio_svc.PREAPPROVED_META_KEY)
    assert isinstance(block, dict)
    assert block["beneficiary_id"] == view["id"]
    assert block["limit_minor"] == 50_000 and block["total_minor"] == 10_000
    assert block["exemption_reason"] == bio_svc.PREAPPROVED_EXEMPTION_REASON
    assert ALIAS_UNICO not in str(block) and CCI_UNICO not in str(block)
    assert _avail(session, t14_session["a1"]) == 190_000
    assert _avail(session, t14_session["b1"]) == 60_000


def test_under_limit_initiated_event_carries_reason(t14_session):
    from app.modules.transactions.service import biometric as bio_svc

    session = t14_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=50_000, currency="PEN")
    session.commit()
    tx = _third_party(
        session, t14_session["a1"], t14_session["b1"], 10_000, beneficiary_id=view["id"]
    )
    session.commit()
    payloads = _initiated_events(session, tx.id)
    assert len(payloads) == 1
    assert payloads[0]["exemption_reason"] == bio_svc.PREAPPROVED_EXEMPTION_REASON
    assert payloads[0]["beneficiary_id"] == view["id"]


def test_over_limit_requires_biometry(t14_session):
    session = t14_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=5_000, currency="PEN")
    session.commit()
    tx = _third_party(
        session, t14_session["a1"], t14_session["b1"], 10_000, beneficiary_id=view["id"]
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    assert "preapproved_exemption" not in meta
    payloads = _initiated_events(session, tx.id)
    assert len(payloads) == 1 and payloads[0]["exemption_reason"] is None


def test_over_limit_authorize_still_settles(t14_session):
    """Por encima del tope la biometria aplica con normalidad (no debilita HU08)."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session = t14_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=5_000, currency="PEN")
    session.commit()
    tx = _third_party(
        session, t14_session["a1"], t14_session["b1"], 10_000, beneficiary_id=view["id"]
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    fresh = tx_repo.get_transaction(session, tx.id)
    nonce = (bio_svc.get_block(fresh) or {}).get("nonce") or ""
    done = bio_svc.authorize_transaction(
        session,
        tx.id,
        device_id=DEVICE_A,
        signature=f"SIG:{nonce}",
        balance_port=_real_port(),
        binding_reader=lambda _s, _u, _d: {
            "device_id": DEVICE_A,
            "public_key": "hmac:" + "ab" * 16,
            "status": "ACTIVE",
        },
        signature_verifier=lambda n, s, _k: bool(n) and s == f"SIG:{n}",
    )
    session.commit()
    assert done.status == "SETTLED"
    assert _avail(session, t14_session["a1"]) == 190_000


def test_no_limit_behaves_as_today(t14_session):
    session = t14_session["session"]
    view = _make_beneficiary(session)  # sin tope: `None`
    session.commit()
    tx = _third_party(
        session, t14_session["a1"], t14_session["b1"], 10_000, beneficiary_id=view["id"]
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"


def test_no_beneficiary_behaves_as_today(t14_session):
    session = t14_session["session"]
    tx = _third_party(session, t14_session["a1"], t14_session["b1"], 1_000)
    session.commit()
    assert tx.status == "SETTLED"  # bajo umbral, sin biometria
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    assert "preapproved_exemption" not in meta
    big = _third_party(session, t14_session["a1"], t14_session["b1"], 10_000)
    session.commit()
    assert big.status == "PENDING_AUTHORIZATION"


def test_unknown_beneficiary_no_exemption_no_oracle(t14_session):
    session = t14_session["session"]
    tx = _third_party(
        session,
        t14_session["a1"],
        t14_session["b1"],
        10_000,
        beneficiary_id=str(uuid.uuid4()),
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"


def test_other_owner_beneficiary_no_exemption_no_oracle(t14_session):
    session = t14_session["session"]
    view = _make_beneficiary(session, owner=USER_B, preapproved_limit_minor=50_000, currency="PEN")
    session.commit()
    tx = _third_party(
        session, t14_session["a1"], t14_session["b1"], 10_000, beneficiary_id=view["id"]
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"


def test_currency_mismatch_rejects(t14_session):
    from app.modules.transactions.service import biometric as bio_svc

    session = t14_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=50_000, currency="USD")
    session.commit()
    with pytest.raises(bio_svc.PreapprovedCurrencyMismatchError):
        _third_party(
            session, t14_session["a1"], t14_session["b1"], 10_000, beneficiary_id=view["id"]
        )
    session.rollback()


def test_daily_limit_still_enforced_with_limit(t14_session):
    """La exencion nunca ignora el limite diario por perfil (CA-03 intacto)."""
    from app.modules.transactions import repository as tx_repo

    session = t14_session["session"]
    _set_param(session, DAILY_KEY, 5_000)
    _set_param(session, DAILY_STANDARD_KEY, 5_000)
    view = _make_beneficiary(session, preapproved_limit_minor=50_000, currency="PEN")
    session.commit()
    tx = _third_party(
        session, t14_session["a1"], t14_session["b1"], 6_000, beneficiary_id=view["id"]
    )
    session.commit()
    assert tx.status == "REJECTED"
    fresh = tx_repo.get_transaction(session, tx.id)
    assert fresh.failure_reason == "DAILY_LIMIT_EXCEEDED"


def test_metadata_beneficiary_id_path(t14_session):
    """`metadata["beneficiary_id"]` habilita la exencion sin cambiar el contrato HTTP."""
    session = t14_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=50_000, currency="PEN")
    session.commit()
    tx = _third_party(
        session,
        t14_session["a1"],
        t14_session["b1"],
        10_000,
        metadata={"beneficiary_id": view["id"]},
    )
    session.commit()
    assert tx.status == "SETTLED"
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    assert meta["preapproved_exemption"]["beneficiary_id"] == view["id"]


def test_own_transfer_supports_limit(t14_session):
    from app.modules.transactions import service as svc

    session = t14_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=50_000, currency="PEN")
    session.commit()
    tx = svc.transfer_own(
        session,
        initiator_user_id=USER_A,
        source_account_id=t14_session["a1"],
        target_account_id=t14_session["a2"],
        amount_minor=10_000,
        currency="PEN",
        idempotency_key=f"e2t14-own-{uuid.uuid4()}",
        balance_port=_real_port(),
        beneficiary_id=view["id"],
    )
    session.commit()
    assert tx.status == "SETTLED"


def test_idempotency_replay_no_duplicate(t14_session):
    from app.modules.transactions import service as svc

    session = t14_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=50_000, currency="PEN")
    session.commit()
    key = f"e2t14-idem-{uuid.uuid4()}"
    first = svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=t14_session["a1"],
        target_account_id=t14_session["b1"],
        amount_minor=10_000,
        currency="PEN",
        fee_minor=0,
        idempotency_key=key,
        balance_port=_real_port(),
        beneficiary_id=view["id"],
    )
    session.commit()
    replayed = svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=t14_session["a1"],
        target_account_id=t14_session["b1"],
        amount_minor=10_000,
        currency="PEN",
        fee_minor=0,
        idempotency_key=key,
        balance_port=_real_port(),
        beneficiary_id=view["id"],
    )
    session.commit()
    assert replayed.id == first.id and replayed.status == "SETTLED"
    assert _avail(session, t14_session["a1"]) == 190_000  # un solo debito
    assert _avail(session, t14_session["b1"]) == 60_000


def test_concurrent_within_and_over_limit_consistent():
    """D-13: dentro del tope exime y fuera exige, sin exencion indebida ni carrera."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    setup = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        src = accounts_repo.create_account(
            setup, user_id=USER_A, account_number="00990001111", type="AHORRO", currency="PEN"
        )
        dst = accounts_repo.create_account(
            setup, user_id=USER_B, account_number="00880003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            setup, src.id, available_delta=200_000, held_delta=0, currency="PEN"
        )
        _seed_param(setup, THRESHOLD_KEY, "2000")
        _seed_param(setup, DAILY_KEY, "500000")
        _seed_param(setup, DAILY_STANDARD_KEY, "500000")
        _seed_param(setup, MAX_ATTEMPTS_KEY, "5", module="identity")
        view = _make_beneficiary(setup, preapproved_limit_minor=50_000, currency="PEN")
        setup.commit()
        src_id, dst_id, beneficiary_id = src.id, dst.id, view["id"]
    finally:
        setup.close()

    from app.modules.transactions import service as svc

    barrier = threading.Barrier(2)
    gate = threading.Lock()
    outcomes: dict[str, str] = {}

    def _worker(name: str, amount: int):
        session = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            barrier.wait(timeout=10)
            with gate:  # SQLite no bloquea filas: se serializa el acceso (criterio E5-T07)
                tx = svc.transfer_third_party(
                    session,
                    initiator_user_id=USER_A,
                    source_account_id=src_id,
                    target_account_id=dst_id,
                    amount_minor=amount,
                    currency="PEN",
                    fee_minor=0,
                    idempotency_key=f"e2t14-conc-{name}-{uuid.uuid4()}",
                    balance_port=_real_port(),
                    beneficiary_id=beneficiary_id,
                )
                session.commit()
                outcomes[name] = tx.status
        except Exception as exc:  # noqa: BLE001 - se reporta como fallo del hilo
            session.rollback()
            outcomes[name] = f"ERROR:{type(exc).__name__}"
        finally:
            session.close()

    threads = [
        threading.Thread(target=_worker, args=("within", 10_000)),
        threading.Thread(target=_worker, args=("over", 60_000)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert outcomes == {"within": "SETTLED", "over": "PENDING_AUTHORIZATION"}, outcomes
    finally:
        engine.dispose()


def test_facade_boundary_static():
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "app" / "modules" / "transactions"
    service_src = (base / "service" / "__init__.py").read_text(encoding="utf-8")
    bio_src = (base / "service" / "biometric.py").read_text(encoding="utf-8")
    for token in ("accounts.models", "accounts.repository"):
        assert token not in service_src, f"frontera rota en service: {token}"
        assert token not in bio_src, f"frontera rota en biometric: {token}"
    # La unica via a `accounts` es la fachada `service/` (D-10).
    assert "accounts.service" in service_src or "accounts.service" in bio_src
    assert "get_for_operation" in bio_src


def test_no_pii_in_logs(t14_session, caplog):
    session = t14_session["session"]
    view = _make_beneficiary(
        session,
        preapproved_limit_minor=50_000,
        currency="PEN",
        holder_name="Nombre Sensible Unico",
    )
    session.commit()
    with caplog.at_level(logging.INFO, logger="app.modules.transactions"):
        tx = _third_party(
            session, t14_session["a1"], t14_session["b1"], 10_000, beneficiary_id=view["id"]
        )
        session.commit()
    assert tx.status == "SETTLED"
    assert CCI_UNICO not in caplog.text
    assert ALIAS_UNICO not in caplog.text
    assert "Nombre Sensible Unico" not in caplog.text
