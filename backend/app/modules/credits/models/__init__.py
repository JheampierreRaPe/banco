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

Expediente `credits.loan_applications` (E3-T08, HU10): `LoanApplication`
(`docs/03b#82`). El enum `loan_application_status` (`docs/03b#1`) se
aplica como `CHECK` sobre `VARCHAR(20)` (no como enum Postgres: evita
drift y es portable a SQLite en pruebas).

Prestamo y cronograma definitivo (E3-T18, HU12): `Loan`
(`docs/03b#83`), `LoanSchedule` (`docs/03b#84`) y `LoanPayment`
(`docs/03b#85`); estados por `CHECK` sobre `VARCHAR` (mismo criterio).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
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


#: Estados del expediente (`docs/03b#1`, enum `loan_application_status`).
LOAN_APPLICATION_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "SUBMITTED",
    "IN_EVALUATION",
    "APPROVED",
    "REJECTED",
    "IN_REVIEW",
    "EXPIRED",
)

#: Estados con dictamen final (no reevaluables, E3-T08).
DECIDED_STATUSES: frozenset[str] = frozenset({"APPROVED", "REJECTED"})


class LoanApplication(Base):
    """Expediente de solicitud de credito (`docs/03b#82`, E3-T08 HU10).

    `user_id` es UUID logico (REF `identity.users` solo a nivel
    aplicacion, regla de oro 4); `product_id` es FK fisica al catalogo
    del mismo schema (`credits.loan_products`). Dinero entero en
    centimos; `dti_ratio` es `NUMERIC(12,4)` (`Decimal`, nunca `float`).

    `dti_ratio` es solo informativo (el dictamen lo decide el motor de
    reglas E3-T07 con el DTI exacto): el repositorio lo acota a
    `±99999999.9999` antes de persistir para que un DTI extremo
    (p.ej. ingreso minimo + deuda maxima) nunca desborde la columna.

    El consentimiento de bureau (`bureau_authorized` +
    `bureau_authorized_at`) lo estampa el caso de uso al evaluar; sin el
    no hay dictamen (HU10 CA-01).
    """

    __tablename__ = "loan_applications"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["credits.loan_products.id"],
            name="fk_loan_applications_product",
        ),
        sa.CheckConstraint(
            "requested_amount_minor > 0",
            name="ck_loan_applications_amount_pos",
        ),
        sa.CheckConstraint("term_months > 0", name="ck_loan_applications_term_pos"),
        sa.CheckConstraint(
            "status IN "
            "('DRAFT','SUBMITTED','IN_EVALUATION','APPROVED','REJECTED','IN_REVIEW','EXPIRED')",
            name="ck_loan_applications_status",
        ),
        sa.Index("ix_loan_applications_user_id", "user_id"),
        sa.Index("ix_loan_applications_status", "status"),
        sa.Index("ix_loan_applications_created_at", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    requested_amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    term_months: Mapped[int] = mapped_column(sa.SmallInteger(), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="SUBMITTED", server_default="SUBMITTED"
    )
    bureau_authorized: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.false()
    )
    bureau_authorized_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    score: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    dti_ratio: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 4), nullable=True)
    total_income_minor: Mapped[int | None] = mapped_column(sa.BigInteger(), nullable=True)
    total_debt_minor: Mapped[int | None] = mapped_column(sa.BigInteger(), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
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
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


#: Documentos del juego contractual (E3-T12, HU11 CA-01): decision del
#: dueno (tres documentos separados, cada uno con su `doc_ref`/`doc_hash`).
CONTRACT_DOC_TYPES: tuple[str, ...] = ("CONTRATO", "PAGARE", "RESUMEN")

#: `slug` de archivo por tipo documental (sin PII en el `doc_ref`).
CONTRACT_DOC_SLUGS: dict[str, str] = {
    "CONTRATO": "contrato",
    "PAGARE": "pagare",
    "RESUMEN": "hoja-resumen",
}

#: Estados del contrato (`docs/03b#86`, enum `contract_status`).
CONTRACT_STATUSES: tuple[str, ...] = ("GENERATED", "SIGNED", "VOID")


class Contract(Base):
    """Documento contractual (`docs/03b#86`, E3-T12 HU11).

    Un juego = tres filas por expediente (`CONTRATO` + `PAGARE` +
    `RESUMEN`, UQ por `(application_id, doc_type)`); cada fila con su
    propio `doc_ref` (ruta/almacen logica) y `doc_hash` (SHA-256 del
    contenido canonico, reproducible).

    `application_id` es FK fisica al expediente del mismo schema
    (`credits.loan_applications`). `loan_id` es UUID **logico sin FK
    fisica** (`NULL` en HU11; lo puebla HU12 al desembolsar, pues
    `loans` aun no existe): sin `UQ` propia porque las tres filas del
    juego comparten el prestamo logico.
    """

    __tablename__ = "contracts"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["credits.loan_applications.id"],
            name="fk_contracts_application",
        ),
        sa.CheckConstraint(
            "doc_type IN ('CONTRATO','PAGARE','RESUMEN')",
            name="ck_contracts_doc_type",
        ),
        sa.CheckConstraint(
            "status IN ('GENERATED','SIGNED','VOID')",
            name="ck_contracts_status",
        ),
        sa.UniqueConstraint(
            "application_id",
            "doc_type",
            name="uq_contracts_application_doc_type",
        ),
        sa.Index("ix_contracts_application_id", "application_id"),
        sa.Index("ix_contracts_status", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    loan_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    doc_type: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    doc_ref: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    doc_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    signature_token_hash: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="GENERATED", server_default="GENERATED"
    )
    signed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )


#: Estados del prestamo (`docs/03b#1`, enum `loan_status`).
LOAN_STATUSES: tuple[str, ...] = ("ACTIVE", "PAID", "DEFAULTED", "WRITTEN_OFF")

#: Estados de cada cuota (`docs/03b#1`, enum `installment_status`).
INSTALLMENT_STATUSES: tuple[str, ...] = ("PENDING", "PARTIAL", "PAID", "OVERDUE")


class Loan(Base):
    """Prestamo desembolsado (`docs/03b#83`, E3-T18 HU12).

    Una sola fila por `application_id` (UQ: idempotencia de negocio).
    `account_id` es UUID **logico sin FK fisica** (REF `accounts`,
    cuenta de desembolso, regla de oro 4); `application_id` es FK
    fisica al expediente del mismo schema (`credits`).

    Dinero entero en centimos (`principal_minor` /
    `outstanding_principal_minor`, `BIGINT`); `tcea` e
    `insurance_rate` son `NUMERIC(18,8)` (`Decimal`, nunca `float`).
    `outstanding_principal_minor` arranca en `principal_minor` y
    nunca es negativo. `status` inicial `ACTIVE`.

    Columna aditiva fuera de `docs/03b#83` (gap E3-T18, G61):
    `auto_debit_enabled` (debito automatico de cuotas, CA-03 de HU12;
    la expone `E3-T19`).
    """

    __tablename__ = "loans"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["credits.loan_applications.id"],
            name="fk_loans_application",
        ),
        sa.UniqueConstraint(
            "application_id",
            name="uq_loans_application_id",
        ),
        sa.CheckConstraint(
            "principal_minor > 0",
            name="ck_loans_principal_pos",
        ),
        sa.CheckConstraint(
            "outstanding_principal_minor >= 0",
            name="ck_loans_outstanding_nonneg",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','PAID','DEFAULTED','WRITTEN_OFF')",
            name="ck_loans_status",
        ),
        sa.Index("ix_loans_status", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    principal_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    tcea: Mapped[Decimal] = mapped_column(sa.Numeric(18, 8), nullable=False)
    term_months: Mapped[int] = mapped_column(sa.SmallInteger(), nullable=False)
    insurance_rate: Mapped[Decimal] = mapped_column(
        sa.Numeric(18, 8), nullable=False, default=Decimal(0), server_default="0"
    )
    outstanding_principal_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    disbursed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    auto_debit_enabled: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.false()
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


class LoanSchedule(Base):
    """Cuota del cronograma definitivo (`docs/03b#84`, E3-T18 HU12).

    `loan_id` es FK fisica al prestamo del mismo schema (`credits`),
    con UQ por `(loan_id, installment_no)`: el cronograma se genera
    completo (`installment_no` 1..N). Por cuota
    `principal + interest + insurance == total` y
    `sum(principal) == loans.principal_minor` (cuadre del dominio
    puro `credits.domain.schedule`). Estado inicial `PENDING`.
    """

    __tablename__ = "loan_schedules"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["loan_id"],
            ["credits.loans.id"],
            name="fk_loan_schedules_loan",
        ),
        sa.UniqueConstraint(
            "loan_id",
            "installment_no",
            name="uq_loan_schedules_loan_installment",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','PARTIAL','PAID','OVERDUE')",
            name="ck_loan_schedules_status",
        ),
        sa.Index("ix_loan_schedules_loan_due_status", "loan_id", "due_date", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    installment_no: Mapped[int] = mapped_column(sa.SmallInteger(), nullable=False)
    due_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    principal_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    interest_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    insurance_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    total_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="PENDING", server_default="PENDING"
    )
    paid_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class LoanPayment(Base):
    """Aplicacion de un pago a la cuota (`docs/03b#85`, E3-T18 HU12).

    `loan_id` / `schedule_id` son FK fisicas dentro de `credits`;
    `transaction_id` es UUID **logico sin FK fisica** (REF
    `transactions`, operacion en el motor, regla de oro 4; la puebla
    `E3-T20` al pagar por el motor). El desglose aplicado
    (`principal/interest/insurance_applied_minor`) documenta a que
    componente fue cada centimo.
    """

    __tablename__ = "loan_payments"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["loan_id"],
            ["credits.loans.id"],
            name="fk_loan_payments_loan",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["credits.loan_schedules.id"],
            name="fk_loan_payments_schedule",
        ),
        sa.CheckConstraint(
            "amount_minor > 0",
            name="ck_loan_payments_amount_pos",
        ),
        sa.Index("ix_loan_payments_loan_id", "loan_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    principal_applied_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    interest_applied_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    insurance_applied_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    paid_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
