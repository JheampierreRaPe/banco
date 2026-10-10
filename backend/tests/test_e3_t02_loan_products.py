"""Catalogo `credits.loan_products` configurable (E3-T02, HU09 datos).

- Camino feliz: sembrar 3 productos demo y listarlos; `get_product(code)`
  devuelve tasas y topes esperados (CA-01/CA-02, parte de datos: el
  simulador puede tomar la tasa del producto).
- Errores: rango invalido -> `VALIDATION_ERROR`; `code` duplicado ->
  `DUPLICATE_PRODUCT` (UQ); inexistente -> `NOT_FOUND`; selector
  ausente/duplicado o UUID malformado -> `VALIDATION_ERROR`.
- Idempotencia: correr la semilla dos veces no duplica (upsert por `code`).
- Sin `float`: tasas `Decimal` exactas, dinero en centimos enteros.

SQLite en memoria + schema ATTACH (patron del repo); sin red, sin PII.
En Postgres (`banca_test`, fixture `db_session`) se repite el ciclo
feliz + idempotencia con el tipo `SMALLINT[]` real; se omite sin BD.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.db import Base
from app.modules.credits import repository as repo
from app.modules.credits import service as catalog


@pytest.fixture()
def sqlite_session():
    """Sesion SQLite aislada con schema `credits` (ATTACH)."""
    import app.modules.credits.models as _c  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS credits")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[Base.metadata.tables["credits.loan_products"]],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(session: Session):
    return repo.seed_demo_products(session)


# ------------------------------------------------- camino feliz
def test_seed_lists_three_demo_products(sqlite_session: Session):
    rows = _seed(sqlite_session)
    assert [r.code for r in rows] == ["PERSONAL_LIBRE", "VEHICULAR", "PYME_CAPITAL"]

    listed = catalog.list_products(sqlite_session)
    assert [p["code"] for p in listed] == ["PERSONAL_LIBRE", "PYME_CAPITAL", "VEHICULAR"]
    assert all(p["enabled"] for p in listed)


def test_get_product_by_code_returns_rates_and_caps(sqlite_session: Session):
    _seed(sqlite_session)
    product = catalog.get_product(sqlite_session, code="VEHICULAR")
    assert product["name"] == "Credito vehicular"
    assert product["tcea"] == Decimal("0.18500000")
    assert product["tea"] == Decimal("0.16500000")
    assert product["insurance_rate"] == Decimal("0.00280000")
    assert product["min_amount_minor"] == 1_000_000
    assert product["max_amount_minor"] == 20_000_000
    assert product["min_term_months"] == 12
    assert product["max_term_months"] == 60
    assert product["allowed_terms"] == [12, 24, 36, 48, 60]


def test_get_product_by_id(sqlite_session: Session):
    rows = _seed(sqlite_session)
    target = next(r for r in rows if r.code == "PYME_CAPITAL")
    found = catalog.get_product(sqlite_session, product_id=target.id)
    assert found["code"] == "PYME_CAPITAL"
    found_str = catalog.get_product(sqlite_session, product_id=str(target.id))
    assert found_str["id"] == target.id


def test_list_hides_disabled_by_default(sqlite_session: Session):
    rows = _seed(sqlite_session)
    target = next(r for r in rows if r.code == "PYME_CAPITAL")
    target.enabled = False
    sqlite_session.flush()

    assert [p["code"] for p in catalog.list_products(sqlite_session)] == [
        "PERSONAL_LIBRE",
        "VEHICULAR",
    ]
    all_products = catalog.list_products(sqlite_session, only_enabled=False)
    assert len(all_products) == 3
    assert catalog.get_product(sqlite_session, code="PYME_CAPITAL")["enabled"] is False


def test_facade_rates_are_decimal_without_float(sqlite_session: Session):
    _seed(sqlite_session)
    for product in catalog.list_products(sqlite_session):
        for field in ("tcea", "tea", "insurance_rate"):
            assert isinstance(product[field], Decimal), field
        for field in ("min_amount_minor", "max_amount_minor"):
            assert type(product[field]) is int, field


# ------------------------------------------------- errores
def test_get_product_not_found(sqlite_session: Session):
    _seed(sqlite_session)
    with pytest.raises(repo.LoanProductNotFoundError) as exc:
        catalog.get_product(sqlite_session, code="HIPOTECARIO")
    assert exc.value.code == "NOT_FOUND"
    with pytest.raises(repo.LoanProductNotFoundError):
        catalog.get_product(sqlite_session, product_id=uuid.uuid4())


def test_get_product_selector_errors(sqlite_session: Session):
    _seed(sqlite_session)
    with pytest.raises(repo.LoanProductValidationError) as exc:
        catalog.get_product(sqlite_session)
    assert exc.value.code == "VALIDATION_ERROR"
    with pytest.raises(repo.LoanProductValidationError):
        catalog.get_product(sqlite_session, code="VEHICULAR", product_id=uuid.uuid4())
    with pytest.raises(repo.LoanProductValidationError):
        catalog.get_product(sqlite_session, product_id="no-es-uuid")


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_amount_minor": 5_000_000, "max_amount_minor": 100_000},  # min > max
        {"min_term_months": 36, "max_term_months": 6},  # min > max
        {"tcea": Decimal("-0.01")},  # tasa negativa
        {"tea": Decimal(-1)},
        {"insurance_rate": Decimal("-0.5")},
        {"tcea": 0.25},  # float prohibido (regla de oro 3)
        {"min_amount_minor": 1000.0},  # float en centimos
        {"min_amount_minor": -1},
        {"allowed_terms": [6, 99]},  # fuera del rango del producto
        {"allowed_terms": []},
        {"allowed_terms": "12"},
        {"code": "  "},
        {"name": ""},
    ],
)
def test_create_rejects_invalid_ranges(sqlite_session: Session, overrides):
    spec = dict(repo.DEMO_PRODUCTS[0])
    spec["code"] = f"TMP-{uuid.uuid4().hex[:8].upper()}"
    spec.update(overrides)
    with pytest.raises(repo.LoanProductValidationError) as exc:
        repo.create_product(sqlite_session, **spec)
    assert exc.value.code == "VALIDATION_ERROR"
    sqlite_session.rollback()
    assert repo.list_products(sqlite_session, only_enabled=False) == []


def test_duplicate_code_conflicts_without_residue(sqlite_session: Session):
    _seed(sqlite_session)
    spec = dict(repo.DEMO_PRODUCTS[0])
    with pytest.raises(repo.LoanProductConflictError) as exc:
        repo.create_product(sqlite_session, **spec)
    assert exc.value.code == "DUPLICATE_PRODUCT"
    assert len(repo.list_products(sqlite_session, only_enabled=False)) == 3


# ------------------------------------------------- idempotencia
def test_seed_is_idempotent(sqlite_session: Session):
    first = _seed(sqlite_session)
    second = _seed(sqlite_session)
    assert [r.id for r in first] == [r.id for r in second]
    assert len(repo.list_products(sqlite_session, only_enabled=False)) == 3


# ------------------------------------------------- Postgres
def test_pg_seed_crud_and_idempotency(db_session: Session):
    """En Postgres el ciclo feliz + idempotencia con `SMALLINT[]` real."""
    first = repo.seed_demo_products(db_session)
    assert len(first) == 3
    product = catalog.get_product(db_session, code="PERSONAL_LIBRE")
    assert product["tcea"] == Decimal("0.32500000")
    assert product["allowed_terms"] == [6, 12, 18, 24, 36]

    spec = dict(repo.DEMO_PRODUCTS[1])
    with pytest.raises(repo.LoanProductConflictError):
        repo.create_product(db_session, **spec)

    second = repo.seed_demo_products(db_session)
    assert [r.id for r in first] == [r.id for r in second]
    assert len(repo.list_products(db_session, only_enabled=False)) == 3
