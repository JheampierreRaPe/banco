"""Esquema `fx` + repositorio base (E7-T01, HU24/HU25).

Cubre `docs/tasks/E7-T01.md#pruebas-requeridas`:

- Migracion: `0049` encadena `0048`, crea las 5 tablas con CHECKs e
  indices, `downgrade` reversible; un solo head.
- Repositorio: CRUD base (tasas, cotizaciones, operaciones, bolsillos,
  movimientos); moneda/monto invalido -> `ValueError`; UQ de `quote_id`
  (una cotizacion se ejecuta una sola vez).
- Integridad: `balance_minor >= 0`; `amount_minor > 0`; `rate > 0`;
  `expires_at > created_at`.
- No mueve dinero.

SQLite en memoria con schema `fx` (ATTACH); las REF logicas
(`user_id`, `account_id`, `transaction_id`, `journal_entry_id`) son
UUID sin FK fisica, asi que no requieren las tablas de otros modulos.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.modules.fx.models as _fx_models
from app.core.db import Base
from app.modules.fx import repository as fx_repo
from app.modules.fx.models import (
    FxOperation,
    FxQuote,
    FxRate,
    PocketMovement,
    SavingsPocket,
)

assert _fx_models is not None

BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_DIR / "migrations" / "versions" / "0049_fx.py"

TABLES = [
    "fx.fx_rates",
    "fx.fx_quotes",
    "fx.fx_operations",
    "fx.savings_pockets",
    "fx.pocket_movements",
]


@pytest.fixture()
def db():
    """SQLite en memoria con las 5 tablas de `fx`."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS fx")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def _future(seconds: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


# ------------------------------------------------- migracion (estatica)
def test_migration_chains_head_with_five_tables():
    """`0049` encadena `0048` y crea las 5 tablas con CHECKs/indices."""
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0049_fx"' in text
    assert 'down_revision = "0048_risk_rules"' in text
    for token in (
        "fx_rates",
        "fx_quotes",
        "fx_operations",
        "savings_pockets",
        "pocket_movements",
        "ck_fx_rates_rate_pos",
        "ck_fx_quotes_status",
        "ck_fx_quotes_expiry_after_creation",
        "uq_fx_operations_quote_id",
        "fk_fx_operations_quote",
        "fk_pocket_movements_pocket",
        "ck_pockets_balance_nonneg",
        "ck_pocket_movements_amount_pos",
        "ck_pocket_movements_direction",
        "ix_fx_rates_base_quote_fetched",
        "ix_fx_quotes_user_status",
        "ix_fx_quotes_expires_at",
        "ix_savings_pockets_user_status",
        "ix_pocket_movements_pocket_created",
        "Numeric(18, 8)",
        "def downgrade",
    ):
        assert token in text, f"migracion incompleta: {token}"
    assert text.count("op.create_table") == 5, "solo las 5 tablas de `fx`"
    assert text.count('schema="fx"') >= 5, "todo se crea en el schema `fx`"
    for arg in ('schema="accounts"', 'schema="transactions"', 'schema="ledger"'):
        assert arg not in text, f"la migracion no debe tocar {arg}"


def test_migration_has_no_cross_schema_fk():
    """REF logicas sin FK fisica: solo 2 FK, ambas dentro de `fx`."""
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    assert text.count("ForeignKeyConstraint") == 2, "solo quote_id y pocket_id llevan FK"
    assert '["fx.fx_quotes.id"]' in text
    assert '["fx.savings_pockets.id"]' in text


def test_single_head_is_0049():
    """Un solo head tras el brief (`alembic check` limpio lo exige)."""
    from tests.db_utils import get_head_revisions

    assert get_head_revisions() == ("0049_fx",)


# ------------------------------------------------- tasas (camino feliz)
def test_create_and_get_latest_rate(db: Session):
    fx_repo.create_rate(db, base="pen", quote="USD", rate=Decimal("3.80"), source="seed")
    db.commit()
    old = fx_repo.get_latest_rate(db, "PEN", "USD")
    assert old is not None and old.rate == Decimal("3.8")
    assert old.base == "PEN" and old.quote == "USD", "moneda normalizada a mayusculas"
    fx_repo.create_rate(
        db,
        base="PEN",
        quote="USD",
        rate=Decimal("3.85"),
        source="seed",
        fetched_at=datetime.now(UTC) + timedelta(seconds=5),
    )
    db.commit()
    latest = fx_repo.get_latest_rate(db, "PEN", "USD")
    assert latest is not None and latest.rate == Decimal("3.85")
    assert fx_repo.get_latest_rate(db, "PEN", "EUR") is None


def test_get_latest_rate_invalid_pair_raises(db: Session):
    with pytest.raises(ValueError):
        fx_repo.get_latest_rate(db, "PEN", "PEN")


# ------------------------------------------------- cotizaciones / operaciones
def test_quote_lifecycle_and_single_execution(db: Session):
    user = _uid()
    quote = fx_repo.create_quote(
        db,
        user_id=user,
        base="PEN",
        quote="USD",
        rate=Decimal("3.80"),
        spread=Decimal("0.005"),
        source_amount_minor=38000,
        target_amount_minor=10000,
        expires_at=_future(),
    )
    db.commit()
    assert quote.status == "ACTIVE"
    assert fx_repo.get_quote(db, quote.id) is not None
    assert fx_repo.get_quote(db, "no-es-uuid") is None

    op = fx_repo.create_operation(db, quote_id=quote.id, transaction_id=_uid())
    db.commit()
    assert op.status == "EXECUTED"
    assert op.quote_id == quote.id

    # Una cotizacion no se ejecuta dos veces (UQ `quote_id`, CA-03).
    with pytest.raises(ValueError):
        fx_repo.create_operation(db, quote_id=quote.id, transaction_id=_uid())
    db.rollback()

    # Expirar una cotizacion libre.
    quote2 = fx_repo.create_quote(
        db,
        user_id=user,
        base="PEN",
        quote="USD",
        rate=Decimal("3.80"),
        source_amount_minor=38000,
        target_amount_minor=10000,
        expires_at=_future(),
    )
    db.commit()
    assert fx_repo.expire_quote(db, quote2.id).status == "EXPIRED"
    assert fx_repo.expire_quote(db, quote2.id).status == "EXPIRED", "idempotente"
    db.commit()
    with pytest.raises(ValueError):
        fx_repo.expire_quote(db, _uid())


def test_expire_consumed_quote_conflicts(db: Session):
    quote = fx_repo.create_quote(
        db,
        user_id=_uid(),
        base="PEN",
        quote="USD",
        rate=Decimal("3.80"),
        source_amount_minor=100,
        target_amount_minor=26,
        expires_at=_future(),
    )
    quote.status = "CONSUMED"
    db.commit()
    with pytest.raises(ValueError):
        fx_repo.expire_quote(db, quote.id)
    db.rollback()


# ------------------------------------------------- bolsillos y movimientos
def test_pocket_crud_and_movements(db: Session):
    user = _uid()
    pocket = fx_repo.create_pocket(
        db,
        user_id=user,
        name="Viaje",
        currency="pen",
        target_minor=100000,
        yield_rate=Decimal("0.03"),
    )
    db.commit()
    assert pocket.currency == "PEN"
    assert pocket.balance_minor == 0
    assert pocket.status == "ACTIVE"
    assert fx_repo.get_pocket(db, pocket.id) is not None
    assert fx_repo.get_pocket(db, "no-es-uuid") is None
    assert [p.id for p in fx_repo.list_pockets(db, user)] == [pocket.id]
    assert fx_repo.list_pockets(db, user, status="CLOSED") == []
    assert fx_repo.list_pockets(db, _uid()) == []

    fx_repo.add_pocket_movement(
        db,
        pocket_id=pocket.id,
        movement_type="DEPOSIT",
        direction="CREDIT",
        amount_minor=50000,
        currency="PEN",
        journal_entry_id=_uid(),
    )
    fx_repo.add_pocket_movement(
        db,
        pocket_id=pocket.id,
        movement_type="WITHDRAWAL",
        direction="DEBIT",
        amount_minor=20000,
        currency="PEN",
    )
    db.commit()
    assert fx_repo.require_pocket(db, pocket.id).balance_minor == 30000

    with pytest.raises(ValueError):
        fx_repo.require_pocket(db, _uid())
    db.rollback()


def test_debit_beyond_balance_conflicts(db: Session):
    pocket = fx_repo.create_pocket(db, user_id=_uid(), name="Fondo", currency="USD")
    db.commit()
    with pytest.raises(ValueError):
        fx_repo.add_pocket_movement(
            db,
            pocket_id=pocket.id,
            movement_type="WITHDRAWAL",
            direction="DEBIT",
            amount_minor=1,
            currency="USD",
        )
    db.rollback()
    assert fx_repo.require_pocket(db, pocket.id).balance_minor == 0


def test_movement_on_closed_or_missing_pocket(db: Session):
    pocket = fx_repo.create_pocket(db, user_id=_uid(), name="Fondo", currency="PEN")
    pocket.status = "CLOSED"
    db.commit()
    with pytest.raises(ValueError):
        fx_repo.add_pocket_movement(
            db,
            pocket_id=pocket.id,
            movement_type="DEPOSIT",
            direction="CREDIT",
            amount_minor=100,
            currency="PEN",
        )
    db.rollback()
    with pytest.raises(ValueError):
        fx_repo.add_pocket_movement(
            db,
            pocket_id=_uid(),
            movement_type="DEPOSIT",
            direction="CREDIT",
            amount_minor=100,
            currency="PEN",
        )
    db.rollback()


# ------------------------------------------------- errores de validacion
@pytest.mark.parametrize("bad", ["PE", "PENU", "P3N", "P N", "", 123, None, "USDD"])
def test_invalid_currency_rejected(db: Session, bad):
    with pytest.raises(ValueError):
        fx_repo.create_rate(db, base=bad, quote="USD", rate=Decimal("3.8"), source="s")
    db.rollback()
    with pytest.raises(ValueError):
        fx_repo.create_pocket(db, user_id=_uid(), name="X", currency=bad)
    db.rollback()


def test_same_currency_pair_rejected(db: Session):
    with pytest.raises(ValueError):
        fx_repo.create_rate(db, base="PEN", quote="pen", rate=Decimal(1), source="s")
    db.rollback()


@pytest.mark.parametrize("bad_rate", [0, -1, "0", "-3.8", Decimal(0), Decimal("NaN"), 3.8])
def test_invalid_rate_rejected(db: Session, bad_rate):
    with pytest.raises(ValueError):
        fx_repo.create_rate(db, base="PEN", quote="USD", rate=bad_rate, source="s")
    db.rollback()


@pytest.mark.parametrize("bad_amount", [0, -5, True, 10.5, "100"])
def test_invalid_amounts_rejected(db: Session, bad_amount):
    with pytest.raises(ValueError):
        fx_repo.create_quote(
            db,
            user_id=_uid(),
            base="PEN",
            quote="USD",
            rate=Decimal("3.8"),
            source_amount_minor=bad_amount,
            target_amount_minor=100,
            expires_at=_future(),
        )
    db.rollback()
    pocket = fx_repo.create_pocket(db, user_id=_uid(), name="X", currency="PEN")
    db.commit()
    with pytest.raises(ValueError):
        fx_repo.add_pocket_movement(
            db,
            pocket_id=pocket.id,
            movement_type="DEBIT",
            direction="DEBIT",
            amount_minor=bad_amount,
            currency="PEN",
        )
    db.rollback()


def test_past_expiry_rejected(db: Session):
    with pytest.raises(ValueError):
        fx_repo.create_quote(
            db,
            user_id=_uid(),
            base="PEN",
            quote="USD",
            rate=Decimal("3.8"),
            source_amount_minor=100,
            target_amount_minor=26,
            expires_at=datetime.now(UTC) - timedelta(seconds=5),
        )
    db.rollback()


def test_invalid_status_and_type_rejected(db: Session):
    pocket = fx_repo.create_pocket(db, user_id=_uid(), name="X", currency="PEN")
    db.commit()
    with pytest.raises(ValueError):
        fx_repo.create_operation(db, quote_id=_uid(), transaction_id=_uid(), status="PENDING")
    db.rollback()
    with pytest.raises(ValueError):
        fx_repo.list_pockets(db, pocket.user_id, status="FROZEN")
    db.rollback()
    with pytest.raises(ValueError):
        fx_repo.add_pocket_movement(
            db,
            pocket_id=pocket.id,
            movement_type="BONUS",
            direction="CREDIT",
            amount_minor=100,
            currency="PEN",
        )
    db.rollback()
    with pytest.raises(ValueError):
        fx_repo.add_pocket_movement(
            db,
            pocket_id=pocket.id,
            movement_type="DEPOSIT",
            direction="UP",
            amount_minor=100,
            currency="PEN",
        )
    db.rollback()


def test_float_rate_never_allowed(db: Session):
    """Regla de oro 3: tasas `Decimal`, nunca `float`."""
    with pytest.raises(ValueError):
        fx_repo.create_rate(db, base="PEN", quote="USD", rate=3.8, source="s")
    db.rollback()


# ------------------------------------------------- integridad a nivel BD
def test_db_checks_balance_and_amount(db: Session):
    """`balance_minor >= 0` y `amount_minor > 0` tambien en BD."""
    pocket_id = _uid()
    with pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "INSERT INTO fx.savings_pockets (id, user_id, name, currency, balance_minor) "
                "VALUES (:id, :user_id, 'X', 'PEN', -1)"
            ),
            {"id": pocket_id.hex, "user_id": _uid().hex},
        )
    db.rollback()

    pocket = fx_repo.create_pocket(db, user_id=_uid(), name="X", currency="PEN")
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "INSERT INTO fx.pocket_movements "
                "(id, pocket_id, movement_type, direction, amount_minor, currency) "
                "VALUES (:id, :pocket_id, 'DEPOSIT', 'CREDIT', 0, 'PEN')"
            ),
            {"id": _uid().hex, "pocket_id": pocket.id.hex},
        )
    db.rollback()


def test_db_checks_currency_and_pair(db: Session):
    """Moneda ISO y `base <> quote` tambien en BD."""
    with pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "INSERT INTO fx.fx_rates (id, base, quote, rate, source) "
                "VALUES (:id, 'PE', 'USD', 3.8, 's')"
            ),
            {"id": _uid().hex},
        )
    db.rollback()
    with pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "INSERT INTO fx.fx_rates (id, base, quote, rate, source) "
                "VALUES (:id, 'PEN', 'PEN', 1, 's')"
            ),
            {"id": _uid().hex},
        )
    db.rollback()


def test_db_quote_expiry_check(db: Session):
    """`expires_at > created_at` (`03b#16.5`) tambien en BD."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    with pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "INSERT INTO fx.fx_quotes (id, user_id, base, quote, rate, "
                "source_amount_minor, target_amount_minor, expires_at, created_at) "
                "VALUES (:id, :user_id, 'PEN', 'USD', 3.8, 100, 26, :past, :now)"
            ),
            {"id": _uid().hex, "user_id": _uid().hex, "past": "2000-01-01 00:00:00", "now": now},
        )
    db.rollback()


def test_db_unique_quote_execution(db: Session):
    """UQ `quote_id` tambien en BD (CA-03)."""
    quote_id, tx_id = _uid(), _uid()
    db.execute(
        sa.text(
            "INSERT INTO fx.fx_operations (id, quote_id, transaction_id) "
            "VALUES (:id, :quote_id, :tx_id)"
        ),
        {"id": _uid().hex, "quote_id": quote_id.hex, "tx_id": tx_id.hex},
    )
    db.flush()
    with pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "INSERT INTO fx.fx_operations (id, quote_id, transaction_id) "
                "VALUES (:id, :quote_id, :tx_id)"
            ),
            {"id": _uid().hex, "quote_id": quote_id.hex, "tx_id": _uid().hex},
        )
    db.rollback()


def test_no_money_movement():
    """`fx` no importa el motor ni el ledger (regla de oro 1/4)."""
    for rel in (
        "app/modules/fx/models/__init__.py",
        "app/modules/fx/repository/__init__.py",
    ):
        text = (BACKEND_DIR / rel).read_text(encoding="utf-8")
        for forbidden in (
            "app.modules.transactions",
            "app.modules.ledger",
            "app.modules.accounts",
            "post_entry",
        ):
            assert forbidden not in text, f"frontera rota en {rel}: {forbidden}"


def test_models_registered_without_pii_columns():
    """Ninguna columna de `fx` guarda PII: solo UUIDs/monedas/montos/estados.

    H-5: verificacion real (antes tautologica). Nombres prohibidos en
    cualquier tabla de `fx`, con allowlist explicita para las unicas
    columnas de texto legitimas (`savings_pockets.name` = nombre del
    bolsillo, `fx_rates.source` = origen de la tasa; ninguna es PII de
    persona). Ademas, las REF logicas (`user_id`, `account_id`,
    `transaction_id`, `journal_entry_id`) son UUID, nunca texto con PII.
    """
    pii_forbidden = {
        "email",
        "phone",
        "phone_number",
        "document_number",
        "doc_number",
        "document",
        "national_id",
        "full_name",
        "first_name",
        "last_name",
        "business_name",
        "address",
        "password",
        "password_hash",
        "pin",
        "pin_hash",
        "secret",
        "token",
    }
    # `name` solo vive en `savings_pockets`; `source` solo en `fx_rates`.
    text_allowlist = {"fx.savings_pockets": {"name"}, "fx.fx_rates": {"source"}}
    uuid_refs = {"user_id", "account_id", "transaction_id", "journal_entry_id"}
    for table in TABLES:
        assert table in Base.metadata.tables, f"falta {table}"
        cols = {c.name: c for c in Base.metadata.tables[table].columns}
        assert "id" in cols
        for col_name, column in cols.items():
            lowered = col_name.lower()
            assert lowered not in pii_forbidden, f"PII en {table}.{col_name}"
            if col_name in {"name", "source"}:
                assert col_name in text_allowlist.get(table, set()), (
                    f"{col_name} solo permitido en "
                    f"{[t for t, cs in text_allowlist.items() if col_name in cs]}"
                )
            if col_name in uuid_refs:
                assert isinstance(
                    column.type, sa.Uuid
                ), f"{table}.{col_name} debe ser UUID, no texto con PII"
    for cls in (FxRate, FxQuote, FxOperation, SavingsPocket, PocketMovement):
        cols = {c.name for c in cls.__table__.columns}
        assert "id" in cols
    for text_col in ("name", "source"):
        assert text_col not in {c.name for c in FxQuote.__table__.columns}


# ------------------------------------------------- regresion H-2/H-3/H-6
def test_invalid_journal_or_currency_leaves_balance_untouched(db: Session):
    """H-2: validar `journal_entry_id`/`currency` ANTES de mutar el saldo.

    Falla sin el fix (el saldo quedaba dirty aunque la validacion lanzara
    `FxValidationError`, y un `commit` del llamante lo persistia sin el
    movimiento). Con el fix, el saldo no cambia ni tras `commit`.
    """
    pocket = fx_repo.create_pocket(db, user_id=_uid(), name="Fondo", currency="PEN")
    fx_repo.add_pocket_movement(
        db,
        pocket_id=pocket.id,
        movement_type="DEPOSIT",
        direction="CREDIT",
        amount_minor=50000,
        currency="PEN",
        journal_entry_id=_uid(),
    )
    db.commit()
    assert fx_repo.require_pocket(db, pocket.id).balance_minor == 50000

    with pytest.raises(ValueError):
        fx_repo.add_pocket_movement(
            db,
            pocket_id=pocket.id,
            movement_type="DEPOSIT",
            direction="CREDIT",
            amount_minor=1000,
            currency="PEN",
            journal_entry_id="no-es-uuid",
        )
    # El llamante commitea tras el error: el saldo NO debe haberse movido
    # (sin el fix, el `balance_minor` dirty se persistia sin el movimiento).
    db.commit()
    db.expire_all()
    assert fx_repo.require_pocket(db, pocket.id).balance_minor == 50000

    with pytest.raises(ValueError):
        fx_repo.add_pocket_movement(
            db,
            pocket_id=pocket.id,
            movement_type="DEPOSIT",
            direction="CREDIT",
            amount_minor=1000,
            currency="XX",
            journal_entry_id=_uid(),
        )
    db.commit()
    db.expire_all()
    assert fx_repo.require_pocket(db, pocket.id).balance_minor == 50000
    count = db.scalar(
        sa.select(sa.func.count())
        .select_from(PocketMovement)
        .where(PocketMovement.pocket_id == pocket.id)
    )
    assert count == 1, "ningun movimiento invalido se persistio"


def test_pocket_movements_have_no_delete_cascade(db: Session):
    """H-3: los movimientos son inmutables; borrar el bolsillo no los borra.

    Falla sin el fix (`cascade="all, delete-orphan"` eliminaba las filas
    hijas por ORM). Con el fix, la relacion no configura borrado y la fila
    del movimiento sobrevive al `DELETE` del bolsillo.
    """
    cascade = set(SavingsPocket.movements.property.cascade)
    assert "delete" not in cascade, f"cascade de borrado prohibido: {sorted(cascade)}"
    assert "delete-orphan" not in cascade, f"delete-orphan prohibido: {sorted(cascade)}"

    pocket = fx_repo.create_pocket(db, user_id=_uid(), name="Fondo", currency="PEN")
    movement = fx_repo.add_pocket_movement(
        db,
        pocket_id=pocket.id,
        movement_type="DEPOSIT",
        direction="CREDIT",
        amount_minor=1000,
        currency="PEN",
    )
    db.commit()
    movement_id, pocket_id = movement.id, pocket.id
    db.expunge_all()

    db.delete(fx_repo.require_pocket(db, pocket_id))
    db.flush()
    assert (
        db.get(PocketMovement, movement_id) is not None
    ), "el movimiento debe sobrevivir al borrado del bolsillo"
    db.rollback()
    assert db.get(PocketMovement, movement_id) is not None


def test_validation_errors_do_not_echo_values(db: Session):
    """H-6: `FxValidationError` sin eco del valor recibido (`422 sin eco`).

    Falla sin el fix (los mensajes interpolaban `{value!r}`). Con el fix,
    el campo/motivo se conserva pero el valor hostil nunca se refleja.
    """
    sentinel_uuid = "no-es-uuid-H6"
    with pytest.raises(ValueError) as exc_uuid:
        fx_repo.create_pocket(db, user_id=sentinel_uuid, name="X", currency="PEN")
    assert sentinel_uuid not in str(exc_uuid.value)
    assert "user_id" in str(exc_uuid.value)
    db.rollback()

    sentinel_currency = "Q9!"
    with pytest.raises(ValueError) as exc_cur:
        fx_repo.create_pocket(db, user_id=_uid(), name="X", currency=sentinel_currency)
    assert sentinel_currency not in str(exc_cur.value)
    db.rollback()

    sentinel_currency_raw = 98765
    with pytest.raises(ValueError) as exc_cur_raw:
        fx_repo.create_pocket(db, user_id=_uid(), name="X", currency=sentinel_currency_raw)
    assert str(sentinel_currency_raw) not in str(exc_cur_raw.value)
    db.rollback()

    pocket = fx_repo.create_pocket(db, user_id=_uid(), name="X", currency="PEN")
    db.commit()
    sentinel_journal = "diario-H6-invalido"
    with pytest.raises(ValueError) as exc_journal:
        fx_repo.add_pocket_movement(
            db,
            pocket_id=pocket.id,
            movement_type="DEPOSIT",
            direction="CREDIT",
            amount_minor=100,
            currency="PEN",
            journal_entry_id=sentinel_journal,
        )
    assert sentinel_journal not in str(exc_journal.value)
    assert "journal_entry_id" in str(exc_journal.value)
    db.rollback()
    assert fx_repo.require_pocket(db, pocket.id).balance_minor == 0
