"""Modelo ORM del catalogo `credits.loan_products` (E3-T02, HU09).

Fuente: `docs/03b-diccionario-de-datos.md#81-loan_products` (columnas y
tipos exactos; se adopta ese detalle). Frontera E3-T02: solo esta tabla;
`loan_applications`/`loans`/cronogramas/contratos son de E3-T08 y del
Sprint 3 (`docs/08`), no se crean aqui.

Dinero entero en centimos (`min_amount_minor`/`max_amount_minor`,
`BIGINT`); tasas `NUMERIC(18,8)`; sin `float` (regla de oro 3,
`docs/16#2`). Sin FK entre schemas (regla de oro 4): el catalogo no
referencia a otros modulos.

`allowed_terms` (`SMALLINT[]`) usa `postgresql.ARRAY` con variante
`sa.JSON()` para SQLite (mismo truco que `risk.models` para
`usual_hours`); en Postgres rige `ARRAY` (sin drift en `alembic check`).

"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

SCHEMA = "credits"

_SmallIntArray = postgresql.ARRAY(sa.SmallInteger()).with_variant(sa.JSON(), "sqlite")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LoanProduct(Base):
    """Producto de credito configurable (`03b#81`).

    `code` es la clave de negocio (UQ, estable); `enabled` filtra el
    listado de cara al cliente. Las tasas y topes viven en datos, no en
    codigo (regla de oro 6): cambiarlos no requiere despliegue.
    """

    __tablename__ = "loan_products"
    __table_args__ = (
        sa.UniqueConstraint("code", name="uq_loan_products_code"),
        sa.CheckConstraint(
            "min_amount_minor <= max_amount_minor",
            name="ck_loan_products_amount_range",
        ),
        sa.CheckConstraint(
            "min_term_months <= max_term_months",
            name="ck_loan_products_term_range",
        ),
        sa.CheckConstraint("tcea >= 0", name="ck_loan_products_tcea_nonneg"),
        sa.CheckConstraint("tea IS NULL OR tea >= 0", name="ck_loan_products_tea_nonneg"),
        sa.CheckConstraint("insurance_rate >= 0", name="ck_loan_products_insurance_nonneg"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    tcea: Mapped[Decimal] = mapped_column(sa.Numeric(18, 8), nullable=False)
    tea: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 8), nullable=True)
    insurance_rate: Mapped[Decimal] = mapped_column(
        sa.Numeric(18, 8), nullable=False, default=Decimal(0), server_default="0"
    )
    min_amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    max_amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    min_term_months: Mapped[int] = mapped_column(sa.SmallInteger(), nullable=False)
    max_term_months: Mapped[int] = mapped_column(sa.SmallInteger(), nullable=False)
    allowed_terms: Mapped[list | None] = mapped_column(_SmallIntArray, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=True, server_default=sa.true(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
