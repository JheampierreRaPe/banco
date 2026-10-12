"""`nonce` biometrico de un solo uso estricto (OBS-4, HU08 CA-02/CA-04).

Sobre el motor y el ledger con adaptadores reales (`ACCOUNTS_BALANCE_PORT`,
cuentas por `accounts.repository`) y fachada real de parametros
(`shared.config`, E2-T18), con puertos de fachada `identity` en memoria
(patron `test_e2_t17_biometric.FakeBiometry`): dinero de juguete (UUIDs
sinteticos, sin PII), SQLite en memoria + schemas ATTACH, sin red, sin
`float`.

Mapeo OBS-4 -> test:
- `nonce` usado en un fallo no se reutiliza .... `test_failed_nonce_*`
  (2o uso: generico 403, sin contar de nuevo, sin evento nuevo).
- rotacion legitima preserva el conteo y liquida
  (`EXPIRED_NONCE` -> reto nuevo -> exito) ...... `test_rotation_after_*`.
- el fallback por PIN sigue disponible tras un
  fallo de firma (el `nonce` no aplica al PIN) .. `test_pin_fallback_*`.
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
DEVICE_A = "dev-obs4-1"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

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


@pytest.fixture()
def obs4_session():
    """Sesion SQLite aislada con cuentas fondeadas y parametros E2-T18."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00990001111", type="AHORRO", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00880003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, "2000")
        _seed_param(session, DAILY_KEY, "500000")
        _seed_param(session, DAILY_STANDARD_KEY, "500000")
        _seed_param(session, MAX_ATTEMPTS_KEY, "5", module="identity")
        session.commit()
        yield {"session": session, "engine": engine, "a1": a1.id, "b1": b1.id}
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


def _third_party(session, a1, b1, amount=5_000):
    """Transferencia sobre el umbral (-> `PENDING_AUTHORIZATION` con hold)."""
    from app.modules.transactions import service as svc

    return svc.transfer_third_party(
        session,
        initiator_user_id=USER_A,
        source_account_id=a1,
        target_account_id=b1,
        amount_minor=amount,
        currency="PEN",
        fee_minor=0,
        idempotency_key=f"obs4-{uuid.uuid4()}",
        device_id=DEVICE_A,
        balance_port=_real_port(),
    )


def _authorize(session, tx_id, *, bio: FakeBiometry, **overrides):
    """`authorize` por firma leyendo el `nonce` vigente (fachadas en memoria)."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    tx = tx_repo.get_transaction(session, tx_id)
    nonce = (bio_svc.get_block(tx) or {}).get("nonce") or ""
    params = {
        "device_id": DEVICE_A,
        "signature": bio.sign(nonce),
        "balance_port": _real_port(),
        "binding_reader": bio.binding_reader,
        "signature_verifier": bio.verifier,
    }
    params.update(overrides)
    return bio_svc.authorize_transaction(session, tx_id, **params)


def _security_events(session, tx_id) -> list[dict]:
    from app.modules.shared.models import OutboxEntry

    rows = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == tx_id)).all()
    return [r.payload for r in rows if r.event_type == "auth.failed_attempt"]


# --------------------------------- Un solo uso estricto (fallo consume el reto)
def test_failed_nonce_cannot_be_reused(obs4_session):
    """Tras un fallo, el mismo `nonce` se rechaza (generico, sin contar de nuevo)."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = obs4_session["session"], obs4_session["a1"], obs4_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1)
    session.commit()
    with pytest.raises(bio_svc.NotAuthorizedError) as exc:
        _authorize(session, tx.id, bio=bio, signature="mala")
    assert str(exc.value) == bio_svc.NOT_AUTHORIZED_MESSAGE  # sin oraculo
    session.commit()
    fresh = tx_repo.get_transaction(session, tx.id)
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 1
    assert (bio_svc.get_block(fresh) or {})["used"] is True  # consumido en el fallo
    assert len(_security_events(session, tx.id)) == 1
    # Reintento con el mismo `nonce` (ya consumido): rechazo generico, el
    # conteo y los eventos quedan intactos (patron `identity.domain.nonce`).
    with pytest.raises(bio_svc.NotAuthorizedError) as exc2:
        _authorize(session, tx.id, bio=bio, signature="mala")
    assert str(exc2.value) == bio_svc.NOT_AUTHORIZED_MESSAGE
    session.commit()
    reread = tx_repo.get_transaction(session, tx.id)
    assert reread.status == "PENDING_AUTHORIZATION"
    assert (bio_svc.get_block(reread) or {})["failed_attempts"] == 1
    assert len(_security_events(session, tx.id)) == 1


# ----------------------- Rotacion legitima: el flujo normal sigue funcionando
def test_rotation_after_failure_preserves_count_and_settles(obs4_session):
    """Fallo -> reto nuevo (mismo conteo) -> firma valida liquida."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = obs4_session["session"], obs4_session["a1"], obs4_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1)
    session.commit()
    old_nonce = (bio_svc.get_block(tx_repo.get_transaction(session, tx.id)) or {}).get("nonce")
    with pytest.raises(bio_svc.NotAuthorizedError):
        _authorize(session, tx.id, bio=bio, signature="mala")
    session.commit()
    rotated = bio_svc.request_biometric_challenge(session, tx.id)
    session.commit()
    assert rotated["nonce"] and rotated["nonce"] != old_nonce
    fresh = tx_repo.get_transaction(session, tx.id)
    assert (bio_svc.get_block(fresh) or {})["used"] is False
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 1  # ventana por operacion
    done = _authorize(session, tx.id, bio=bio)
    session.commit()
    assert done.status == "SETTLED"
    assert done.id == tx.id


# ----------------- El PIN sigue disponible tras un fallo de firma (sin rotar)
def test_pin_fallback_still_works_after_signature_failure(obs4_session):
    """El `nonce` no aplica al PIN: un reto consumido no bloquea el fallback."""
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = obs4_session["session"], obs4_session["a1"], obs4_session["b1"]
    bio = FakeBiometry()
    bio.add_binding(USER_A, DEVICE_A)
    tx = _third_party(session, a1, b1)
    session.commit()
    with pytest.raises(bio_svc.NotAuthorizedError):
        _authorize(session, tx.id, bio=bio, signature="mala")
    session.commit()
    assert (bio_svc.get_block(tx_repo.get_transaction(session, tx.id)) or {})["used"] is True
    done = bio_svc.authorize_transaction_with_pin(
        session,
        tx.id,
        pin="cualquiera",
        balance_port=_real_port(),
        pin_verifier=lambda _s, _u, _p: True,
    )
    session.commit()
    assert done.status == "SETTLED"
