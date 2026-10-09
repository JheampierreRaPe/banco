"""Tope pre-aprobado como techo absoluto de transferencia rapida (E2-T32, HU07/HU08).

Cambia la semantica de E2-T14: con `total = amount + fee <= tope` (misma
moneda) la operacion NO pide biometria aunque `effective = acumulado +
total` supere el umbral global; por encima del tope rige el gate normal.
El limite diario y los bloqueos nunca se saltan. El tope compara por
operacion (`total`); el umbral y el limite diario usan el acumulado
(`effective`).

Motor y ledger con adaptadores reales, fachada real de parametros
(`shared.config`, E2-T18) y tope por fachada de `accounts`: dinero de
juguete (UUIDs sinteticos, sin PII), SQLite en memoria + schemas ATTACH,
sin red, sin `float`.

Umbral S/1,000 (100000, semilla `0025`) y limite diario S/5,000 (500000):
el tope de prueba (S/2,000/S/4,000/S/5,000) vive POR ENCIMA del umbral,
que es el caso que E2-T32 habilita.

Mapeo brief -> test:
- techo exime sobre el umbral (con y sin acumulado) .... `test_ceiling_*`
- borde `== tope` exime / `== tope + 1` gate normal .. `test_border_*`
- limite diario intacto ............................... `test_daily_*`
- moneda distinta -> `VALIDATION_ERROR` ................ `test_currency_*`
- sin beneficiario gate normal ....................... `test_no_beneficiary_*`
- via `metadata["beneficiary_id"]` (E2-T30) ........... `test_metadata_*`
- propia (`OWN_TRANSFER`) con techo .................. `test_own_*`
- por encima del tope la firma/PIN siguen OK .......... `test_over_ceiling_*`
- idempotencia ....................................... `test_idempotency_*`
- concurrencia (D-13) ................................ `test_concurrent_*`
- frontera / sin PII ................................. `test_facade_*`, `test_no_pii_*`
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
DEVICE_A = "dev-preapproved-ceiling-1"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

ALIAS_UNICO = "XqzAliasTechoT32"
CCI_UNICO = "00110001111111111111"

# Semilla `0025`: umbral S/1,000 < limite diario S/5,000.
THRESHOLD = 100_000
DAILY_LIMIT = 500_000
CEILING = 200_000  # S/2,000: por encima del umbral global.

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
def t32_session():
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
            session, a1.id, available_delta=3_000_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a2.id, available_delta=1_000_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, str(THRESHOLD))
        _seed_param(session, DAILY_KEY, str(DAILY_LIMIT))
        _seed_param(session, DAILY_STANDARD_KEY, str(DAILY_LIMIT))
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
        "idempotency_key": f"e2t32-{uuid.uuid4()}",
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


# ------------------------------------------------- CA-01: techo sobre el umbral


def test_ceiling_exempts_above_threshold_without_accumulated(t32_session):
    """`total <= tope` con tope > umbral: `SETTLED` con exencion (CA-01)."""
    from app.modules.transactions.service import biometric as bio_svc

    session = t32_session["session"]
    view = _make_beneficiary(
        session, preapproved_limit_minor=CEILING, currency="PEN", holder_name="Titular Unico"
    )
    session.commit()
    tx = _third_party(
        session, t32_session["a1"], t32_session["b1"], 50_000, beneficiary_id=view["id"]
    )
    session.commit()
    # 50_000 < umbral pero el tope igual audita la exencion (techo E2-T32).
    assert tx.status == "SETTLED"
    reasons = _history_reasons(session, tx.id)
    assert bio_svc.PREAPPROVED_EXEMPT_REASON in reasons
    assert bio_svc.REQUIRED_REASON not in reasons
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    block = meta.get(bio_svc.PREAPPROVED_META_KEY)
    assert isinstance(block, dict)
    assert block["beneficiary_id"] == view["id"]
    assert block["limit_minor"] == CEILING and block["total_minor"] == 50_000
    assert block["exemption_reason"] == bio_svc.PREAPPROVED_EXEMPTION_REASON
    payloads = _initiated_events(session, tx.id)
    assert len(payloads) == 1
    assert payloads[0]["exemption_reason"] == bio_svc.PREAPPROVED_EXEMPTION_REASON
    assert payloads[0]["beneficiary_id"] == view["id"]


def test_ceiling_exempts_above_threshold_with_accumulated(t32_session):
    """Con acumulado que supera el umbral, el techo igual exime (CA-01)."""
    from app.modules.transactions.service import biometric as bio_svc

    session = t32_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=CEILING, currency="PEN")
    session.commit()
    # Acumulado previo S/900 (bajo el umbral, liquida y cuenta para el dia).
    prior = _third_party(session, t32_session["a1"], t32_session["b1"], 90_000)
    session.commit()
    assert prior.status == "SETTLED"
    # `effective = 90_000 + 50_000 = 140_000 > umbral 100_000`, pero
    # `total = 50_000 <= tope 200_000`: techo absoluto, sin biometria.
    tx = _third_party(
        session, t32_session["a1"], t32_session["b1"], 50_000, beneficiary_id=view["id"]
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert bio_svc.PREAPPROVED_EXEMPT_REASON in _history_reasons(session, tx.id)
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    assert meta[bio_svc.PREAPPROVED_META_KEY]["exemption_reason"] == (
        bio_svc.PREAPPROVED_EXEMPTION_REASON
    )


def test_ceiling_exempts_with_fee_included(t32_session):
    """El techo compara `amount + fee` (no solo `amount`)."""
    from app.modules.transactions.service import biometric as bio_svc

    session = t32_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=CEILING, currency="PEN")
    session.commit()
    # `total = 150_000 + 50_000 = 200_000 == tope` -> exime.
    tx = _third_party(
        session,
        t32_session["a1"],
        t32_session["b1"],
        150_000,
        fee_minor=50_000,
        beneficiary_id=view["id"],
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert bio_svc.PREAPPROVED_EXEMPT_REASON in _history_reasons(session, tx.id)


# ------------------------------------------------- CA-02: bordes y gate normal


def test_border_equal_limit_exempts(t32_session):
    """`total == tope` exime (borde inclusivo)."""
    from app.modules.transactions.service import biometric as bio_svc

    session = t32_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=CEILING, currency="PEN")
    session.commit()
    tx = _third_party(
        session, t32_session["a1"], t32_session["b1"], CEILING, beneficiary_id=view["id"]
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert bio_svc.PREAPPROVED_EXEMPT_REASON in _history_reasons(session, tx.id)


def test_border_one_over_limit_falls_to_gate(t32_session):
    """`total == tope + 1` cae al gate normal (biometria sobre el umbral)."""
    session = t32_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=CEILING, currency="PEN")
    session.commit()
    tx = _third_party(
        session, t32_session["a1"], t32_session["b1"], CEILING + 1, beneficiary_id=view["id"]
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    assert "preapproved_exemption" not in meta
    payloads = _initiated_events(session, tx.id)
    assert len(payloads) == 1 and payloads[0]["exemption_reason"] is None


def test_over_ceiling_signature_still_settles(t32_session):
    """Por encima del tope la firma del reto liquida con normalidad (E2-T17 intacto)."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session = t32_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=50_000, currency="PEN")
    session.commit()
    tx = _third_party(
        session, t32_session["a1"], t32_session["b1"], 150_000, beneficiary_id=view["id"]
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


def test_no_beneficiary_gate_unchanged(t32_session):
    """Sin beneficiario el gate no cambia: bajo umbral liquida, sobre el umbral reta."""
    session = t32_session["session"]
    small = _third_party(session, t32_session["a1"], t32_session["b1"], 10_000)
    session.commit()
    assert small.status == "SETTLED"
    meta = small.meta if isinstance(small.meta, dict) else {}
    assert "preapproved_exemption" not in meta
    big = _third_party(session, t32_session["a1"], t32_session["b1"], 150_000)
    session.commit()
    assert big.status == "PENDING_AUTHORIZATION"


def test_unknown_beneficiary_no_exemption_no_oracle(t32_session):
    session = t32_session["session"]
    tx = _third_party(
        session,
        t32_session["a1"],
        t32_session["b1"],
        150_000,
        beneficiary_id=str(uuid.uuid4()),
    )
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"


def test_metadata_beneficiary_id_path_applies_ceiling(t32_session):
    """`metadata["beneficiary_id"]` (E2-T30) habilita el techo sobre el umbral."""
    from app.modules.transactions.service import biometric as bio_svc

    session = t32_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=CEILING, currency="PEN")
    session.commit()
    prior = _third_party(session, t32_session["a1"], t32_session["b1"], 90_000)
    session.commit()
    assert prior.status == "SETTLED"
    tx = _third_party(
        session,
        t32_session["a1"],
        t32_session["b1"],
        50_000,
        metadata={"beneficiary_id": view["id"]},
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert bio_svc.PREAPPROVED_EXEMPT_REASON in _history_reasons(session, tx.id)


def test_own_transfer_supports_ceiling_above_threshold(t32_session):
    """La ruta propia atomica tambien respeta el techo (E2-T32)."""
    from app.modules.transactions import service as svc
    from app.modules.transactions.service import biometric as bio_svc

    session = t32_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=CEILING, currency="PEN")
    session.commit()
    prior = _third_party(session, t32_session["a1"], t32_session["b1"], 90_000)
    session.commit()
    assert prior.status == "SETTLED"
    tx = svc.transfer_own(
        session,
        initiator_user_id=USER_A,
        source_account_id=t32_session["a1"],
        target_account_id=t32_session["a2"],
        amount_minor=50_000,
        currency="PEN",
        idempotency_key=f"e2t32-own-{uuid.uuid4()}",
        balance_port=_real_port(),
        beneficiary_id=view["id"],
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert bio_svc.PREAPPROVED_EXEMPT_REASON in _history_reasons(session, tx.id)


# ------------------------------------------------- CA-03: limite diario manda


def test_daily_limit_wins_over_ceiling(t32_session):
    """`effective > limite` -> `REJECTED` aunque el techo cubra la operacion (CA-03)."""
    from app.modules.transactions import repository as tx_repo

    session = t32_session["session"]
    # Acumulado S/1,000.01 en `PENDING` (cuenta para el dia sin liquidar).
    held = _third_party(session, t32_session["a1"], t32_session["b1"], 100_000 + 1)
    session.commit()
    assert held.status == "PENDING_AUTHORIZATION"
    # `total = 450_000 <= tope 500_000`, pero
    # `effective = 100_001 + 450_000 = 550_001 > 500_000` -> REJECTED.
    view2 = _make_beneficiary(
        session,
        owner=USER_A,
        alias="XqzAliasTechoT32B",
        account_or_cci="00220002222222222222",
        preapproved_limit_minor=500_000,
        currency="PEN",
    )
    session.commit()
    tx = _third_party(
        session, t32_session["a1"], t32_session["b1"], 450_000, beneficiary_id=view2["id"]
    )
    session.commit()
    assert tx.status == "REJECTED"
    fresh = tx_repo.get_transaction(session, tx.id)
    assert fresh.failure_reason == "DAILY_LIMIT_EXCEEDED"


def test_daily_limit_exact_boundary_settles_with_ceiling(t32_session):
    """`effective == limite` no excede: el techo liquida (borde del diario)."""
    from app.modules.transactions.service import biometric as bio_svc

    session = t32_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=500_000, currency="PEN")
    session.commit()
    tx = _third_party(
        session, t32_session["a1"], t32_session["b1"], 500_000, beneficiary_id=view["id"]
    )
    session.commit()
    assert tx.status == "SETTLED"
    assert bio_svc.PREAPPROVED_EXEMPT_REASON in _history_reasons(session, tx.id)


# ------------------------------------------------- CA-04: moneda


def test_currency_mismatch_rejects(t32_session):
    from app.modules.transactions.service import biometric as bio_svc

    session = t32_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=CEILING, currency="USD")
    session.commit()
    with pytest.raises(bio_svc.PreapprovedCurrencyMismatchError):
        _third_party(
            session, t32_session["a1"], t32_session["b1"], 50_000, beneficiary_id=view["id"]
        )
    session.rollback()


# ------------------------------------------------- Idempotencia / concurrencia


def test_idempotency_replay_no_duplicate(t32_session):
    from app.modules.transactions import service as svc

    session = t32_session["session"]
    view = _make_beneficiary(session, preapproved_limit_minor=CEILING, currency="PEN")
    session.commit()
    key = f"e2t32-idem-{uuid.uuid4()}"
    first = svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=t32_session["a1"],
        target_account_id=t32_session["b1"],
        amount_minor=50_000,
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
        source_account_id=t32_session["a1"],
        target_account_id=t32_session["b1"],
        amount_minor=50_000,
        currency="PEN",
        fee_minor=0,
        idempotency_key=key,
        balance_port=_real_port(),
        beneficiary_id=view["id"],
    )
    session.commit()
    assert replayed.id == first.id and replayed.status == "SETTLED"
    assert _avail(session, t32_session["a1"]) == 3_000_000 - 50_000


def test_concurrent_within_and_over_ceiling_consistent():
    """D-13: dentro del techo exime y fuera exige, sin exencion indebida."""
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
            setup, src.id, available_delta=3_000_000, held_delta=0, currency="PEN"
        )
        _seed_param(setup, THRESHOLD_KEY, str(THRESHOLD))
        _seed_param(setup, DAILY_KEY, str(DAILY_LIMIT))
        _seed_param(setup, DAILY_STANDARD_KEY, str(DAILY_LIMIT))
        _seed_param(setup, MAX_ATTEMPTS_KEY, "5", module="identity")
        view = _make_beneficiary(setup, preapproved_limit_minor=CEILING, currency="PEN")
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
                    idempotency_key=f"e2t32-conc-{name}-{uuid.uuid4()}",
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
        threading.Thread(target=_worker, args=("within", 50_000)),
        threading.Thread(target=_worker, args=("over", 250_000)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert outcomes == {"within": "SETTLED", "over": "PENDING_AUTHORIZATION"}, outcomes
    finally:
        engine.dispose()


# ------------------------------------------------- Frontera / higiene


def test_facade_boundary_static():
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "app" / "modules" / "transactions"
    service_src = (base / "service" / "__init__.py").read_text(encoding="utf-8")
    bio_src = (base / "service" / "biometric.py").read_text(encoding="utf-8")
    for token in ("accounts.models", "accounts.repository"):
        assert token not in service_src, f"frontera rota en service: {token}"
        assert token not in bio_src, f"frontera rota en biometric: {token}"
    assert "accounts.service" in service_src or "accounts.service" in bio_src
    assert "get_for_operation" in bio_src


def test_no_pii_in_logs(t32_session, caplog):
    session = t32_session["session"]
    view = _make_beneficiary(
        session,
        preapproved_limit_minor=CEILING,
        currency="PEN",
        holder_name="Nombre Sensible Unico",
    )
    session.commit()
    with caplog.at_level(logging.INFO, logger="app.modules.transactions"):
        tx = _third_party(
            session, t32_session["a1"], t32_session["b1"], 50_000, beneficiary_id=view["id"]
        )
        session.commit()
    assert tx.status == "SETTLED"
    assert CCI_UNICO not in caplog.text
    assert ALIAS_UNICO not in caplog.text
    assert "Nombre Sensible Unico" not in caplog.text
