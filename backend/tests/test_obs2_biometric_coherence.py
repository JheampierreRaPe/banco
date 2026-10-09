"""Coherencia umbral biometrico < limite diario (OBS-2).

Sin el fix (`transfer.biometric_threshold_minor = 1000000` >
`transfer.daily_limit_minor` /
`transfer.daily_limit_STANDARD_minor = 500000`), `evaluate_gate`
(`requires_biometry = (not exceeded) and (total > threshold)`, E2-T34) jamas
puede exigir biometria: todo monto sobre el umbral ya excedio el limite
diario (`DAILY_LIMIT_EXCEEDED`). El reto (`PENDING_AUTHORIZATION`) es
inalcanzable.

Con el fix (`0025_biometric_coherence`: umbral `100000`, limites
`500000`):

- invariante: el umbral queda por debajo de cada limite diario sembrado
  (generico + perfiles) y de los defaults de codigo.
- funcional: con la config sembrada (sin overrides), `200000` (banda
  `(100000, 500000]`) genera `PENDING_AUTHORIZATION`; `600000`
  (`> 500000`) sigue `DAILY_LIMIT_EXCEEDED`.

SQLite en memoria + schemas ATTACH (como `test_e2_t17_biometric.py`);
dinero de juguete (UUIDs sinteticos, sin PII), sin red, sin `float`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE = "dev-obs2-coherence"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

# Banda alcanzable tras el fix: sobre el umbral, dentro del limite.
IN_BAND_MINOR = 200_000
# Sobre el limite diario: rechazo, sin reto.
OVER_LIMIT_MINOR = 600_000

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


def _seed_coherent_defaults(session: Session) -> None:
    """Siembra con los defaults de codigo (la coherencia viene del fix)."""
    from app.modules.shared.config import parameters as params

    _seed_param(
        session, params.BIOMETRIC_THRESHOLD_KEY, str(params.DEFAULT_BIOMETRIC_THRESHOLD_MINOR)
    )
    _seed_param(session, params.DAILY_LIMIT_KEY, str(params.DEFAULT_DAILY_LIMIT_MINOR))
    for profile in params.SEEDED_PROFILES:
        key = params.daily_limit_key_for_profile(profile)
        assert key is not None
        _seed_param(session, key, str(params.DEFAULT_DAILY_LIMIT_MINOR))
    _seed_param(session, params.AUTH_MAX_FAILED_ATTEMPTS_KEY, "5", module="identity")


@pytest.fixture(autouse=True)
def _clean_holder_cache():
    from app.modules.transactions import service as svc

    svc.clear_holder_cache()
    yield
    svc.clear_holder_cache()


@pytest.fixture()
def obs2_session():
    """Sesion SQLite con fondos suficientes y parametros coherentes."""
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
            session, a1.id, available_delta=2_000_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=2_000_000, held_delta=0, currency="PEN"
        )
        _seed_coherent_defaults(session)
        session.commit()
        yield {"session": session, "engine": engine, "a1": a1.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


def _third_party(session, a1, b1, amount, **overrides):
    from app.modules.accounts.repository import ACCOUNTS_BALANCE_PORT
    from app.modules.transactions import service as svc

    params = {
        "initiator_user_id": USER_A,
        "source_account_id": a1,
        "target_account_id": b1,
        "amount_minor": amount,
        "currency": "PEN",
        "fee_minor": 0,
        "idempotency_key": f"obs2-{uuid.uuid4()}",
        "device_id": DEVICE,
        "balance_port": ACCOUNTS_BALANCE_PORT,
    }
    params.update(overrides)
    return svc.transfer_third_party(session, **params)


# ------------------------------------------------------- Invariante (OBS-2)
def test_defaults_coherent_threshold_below_daily_limit():
    """Puro: el default del umbral queda por debajo del limite diario."""
    from app.modules.shared.config import parameters as params

    assert params.DEFAULT_BIOMETRIC_THRESHOLD_MINOR == 100_000
    assert params.DEFAULT_DAILY_LIMIT_MINOR == 500_000
    assert params.DEFAULT_BIOMETRIC_THRESHOLD_MINOR < params.DEFAULT_DAILY_LIMIT_MINOR


def test_seeded_threshold_below_every_daily_limit(obs2_session):
    """El umbral sembrado es < cada limite diario (generico + perfiles)."""
    from app.modules.shared.config import parameters as params

    session = obs2_session["session"]
    threshold = params.get_biometric_threshold_minor(session)
    assert threshold < params.get_daily_limit_minor(session)
    for profile in params.SEEDED_PROFILES:
        assert threshold < params.get_daily_limit_minor(session, profile), profile
    # Banda no vacia: existe un entero sobre el umbral dentro del limite.
    assert threshold < params.get_daily_limit_minor(session, "STANDARD")


def test_migration_0025_fixes_threshold_coherence():
    """La migracion 0025 baja el umbral a 100000 (reversible a 1000000)."""
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0025_biometric_coherence.py"
    )
    assert path.exists(), "falta migracion 0025_biometric_coherence.py"
    content = path.read_text(encoding="utf-8")
    for token in (
        'revision = "0025_biometric_coherence"',
        'down_revision = "0024_accounts_numeric_backfill"',
        '"transfer.biometric_threshold_minor"',
        "100000",
        "def upgrade",
        "def downgrade",
    ):
        assert token in content, f"migracion sin {token}"


# ------------------------------------------------------- Funcional (OBS-2)
def test_in_band_amount_requires_biometric_challenge(obs2_session):
    """`200000` en `(100000, 500000]`: `PENDING_AUTHORIZATION` con reto."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = obs2_session["session"], obs2_session["a1"], obs2_session["b1"]
    tx = _third_party(session, a1, b1, IN_BAND_MINOR)
    session.commit()
    assert tx.status == "PENDING_AUTHORIZATION"
    assert tx.failure_reason is None
    block = bio_svc.get_block(tx)
    assert isinstance(block, dict) and block.get("nonce")


def test_over_limit_amount_still_rejected(obs2_session):
    """`600000` en `(500000, +inf)`: `REJECTED` (`DAILY_LIMIT_EXCEEDED`)."""
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = obs2_session["session"], obs2_session["a1"], obs2_session["b1"]
    tx = _third_party(session, a1, b1, OVER_LIMIT_MINOR)
    session.commit()
    assert tx.status == "REJECTED"
    assert tx.failure_reason == bio_svc.DAILY_LIMIT_EXCEEDED
    assert (tx.meta or {}).get("failure_code") == bio_svc.DAILY_LIMIT_EXCEEDED
    assert bio_svc.get_block(tx) is None


# ------------------------------------------------------- Postgres (semilla real)
def test_postgres_seeded_threshold_below_daily_limits(db_session: Session):
    """Con las semillas aplicadas (`0001`..`0025`): umbral < limites."""
    from app.modules.shared.config import parameters as params

    assert params.get_biometric_threshold_minor(db_session) == 100_000
    assert params.get_biometric_threshold_minor(db_session) < params.get_daily_limit_minor(
        db_session
    )
    assert params.get_biometric_threshold_minor(db_session) < params.get_daily_limit_minor(
        db_session, "STANDARD"
    )
