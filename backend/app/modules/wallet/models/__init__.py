"""Modelos ORM del schema `wallet` (E4-T01, HU13 CA-01).

Fuente: `docs/03b-diccionario-de-datos.md#91-wallets-y-wallet_links`.
Frontera: solo tablas propias (`wallets`, `wallet_links`). `user_id` y
`account_id` son UUID logicos (REF sin FK fisica, regla de oro 4); la
unica FK fisica es `wallet_links.wallet_id -> wallet.wallets.id`
(mismo schema).

Tokenizacion (regla de oro 7/9): no existe columna de PAN ni de numero
completo en ninguna tabla; solo `token_ref` (UQ) + `last4` + `brand`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

SCHEMA = "wallet"

WALLET_STATUSES = ("ACTIVE", "BLOCKED", "CLOSED")
WALLET_LINK_STATUSES = ("ACTIVE", "INACTIVE")


class Wallet(Base):
    """Billetera del titular (`03b#91`): una fila por `user_id` (UQ)."""

    __tablename__ = "wallets"
    __table_args__ = (
        sa.UniqueConstraint("user_id", name="uq_wallets_user_id"),
        sa.CheckConstraint(
            "status IN ('ACTIVE','BLOCKED','CLOSED')",
            name="ck_wallets_status",
        ),
        sa.Index("ix_wallets_user_id", "user_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # REF logica a `identity.users` (sin FK fisica).
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    links: Mapped[list[WalletLink]] = relationship(
        back_populates="wallet",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class WalletLink(Base):
    """Fuente de fondos vinculada (`03b#91`): cuenta (`account_id`) o
    tarjeta tokenizada (`token_ref`/`last4`/`brand`)."""

    __tablename__ = "wallet_links"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallet.wallets.id"],
            name="fk_wallet_links_wallet",
        ),
        sa.UniqueConstraint("token_ref", name="uq_wallet_links_token_ref"),
        sa.CheckConstraint(
            "status IN ('ACTIVE','INACTIVE')",
            name="ck_wallet_links_status",
        ),
        sa.Index("ix_wallet_links_wallet_id", "wallet_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # REF logica a `accounts.accounts` (sin FK fisica); NULL = tarjeta.
    account_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    # Referencia tokenizada (UQ). Nunca el PAN ni el numero completo.
    token_ref: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    last4: Mapped[str | None] = mapped_column(sa.String(4), nullable=True)
    brand: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    linked_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    wallet: Mapped[Wallet] = relationship(back_populates="links")


MERCHANT_STATUSES = ("ACTIVE", "BLOCKED", "CLOSED")
QR_CHARGE_MODES = ("DYNAMIC", "STATIC")
QR_CHARGE_STATUSES = ("ACTIVE", "PAID", "EXPIRED", "CANCELLED")


class MerchantAccount(Base):
    """Cuenta de comercio (`03b#92`, E4-T06 HU14): el comercio que cobra con QR."""

    __tablename__ = "merchant_accounts"
    __table_args__ = (
        sa.UniqueConstraint("user_id", name="uq_merchant_accounts_user_id"),
        sa.CheckConstraint(
            "status IN ('ACTIVE','BLOCKED','CLOSED')",
            name="ck_merchant_accounts_status",
        ),
        sa.Index("ix_merchant_accounts_user_id", "user_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # REF logica a `identity.users` (sin FK fisica).
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    business_name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    # RUC hasheado (`domain.qr.hash_tax_id`); nunca en claro (regla 7).
    tax_id_hash: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    charges: Mapped[list[QrCharge]] = relationship(
        back_populates="merchant",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


QR_PAYEE_TYPES = ("MERCHANT", "PERSONAL")


class QrCharge(Base):
    """Cargo QR firmado (`03b#93`, E4-T06 HU14 + E4-T21 QR personal).

    Destino polimorfico (`payee_type`): `MERCHANT` (comercio via
    `merchant_id`, con monto fijo) o `PERSONAL` (titular via
    `owner_user_id`, estatico: `amount_minor = NULL` —lo digita el
    pagador—, `expires_at = NULL` —sin caducidad— y `reusable = true`
    —multiples cobros). No mueve dinero al generarse."""

    __tablename__ = "qr_charges"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["wallet.merchant_accounts.id"],
            name="fk_qr_charges_merchant",
        ),
        sa.CheckConstraint(
            "amount_minor IS NULL OR amount_minor > 0",
            name="ck_qr_charges_amount_minor",
        ),
        sa.CheckConstraint(
            "mode IN ('DYNAMIC','STATIC')",
            name="ck_qr_charges_mode",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','PAID','EXPIRED','CANCELLED')",
            name="ck_qr_charges_status",
        ),
        sa.CheckConstraint(
            "payee_type IN ('MERCHANT','PERSONAL')",
            name="ck_qr_charges_payee_type",
        ),
        sa.CheckConstraint(
            "((payee_type = 'MERCHANT' AND merchant_id IS NOT NULL) "
            "OR (payee_type = 'PERSONAL' AND owner_user_id IS NOT NULL))",
            name="ck_qr_charges_payee",
        ),
        sa.CheckConstraint(
            "(payee_type <> 'PERSONAL' OR "
            "(amount_minor IS NULL AND expires_at IS NULL AND reusable = true))",
            name="ck_qr_charges_personal_static",
        ),
        sa.Index("ix_qr_charges_merchant_status", "merchant_id", "status"),
        sa.Index("ix_qr_charges_expires_at", "expires_at"),
        sa.Index("ix_qr_charges_owner_user_id", "owner_user_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # Nullable desde `0047` (E4-T21): un QR personal no tiene comercio.
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    # Nullable desde `0047` (E4-T21): el QR personal estatico va sin
    # monto (`NULL`; el pagador aporta `amount_minor > 0` al pagar).
    # Dinero entero en centimos; nunca `float` (regla 3).
    amount_minor: Mapped[int | None] = mapped_column(sa.BigInteger(), nullable=True)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    concept: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    mode: Mapped[str] = mapped_column(
        sa.String(10), nullable=False, default="DYNAMIC", server_default="DYNAMIC"
    )
    # Destino polimorfico (E4-T21, migracion `0047`): `MERCHANT` abona
    # al comercio; `PERSONAL` abona a la cuenta del titular
    # (`owner_user_id`, REF logica a `identity.users`; cuenta resuelta
    # por la fachada `accounts`, fijada en `payee_account_id`, REF
    # logica a `accounts.accounts`).
    payee_type: Mapped[str] = mapped_column(
        sa.String(10), nullable=False, default="MERCHANT", server_default="MERCHANT"
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    payee_account_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    # `true` = admite multiples cobros (QR personal estatico); la
    # guarda de doble cobro solo aplica con `false` (migracion `0047`).
    reusable: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.text("false")
    )
    # SHA-256 hex del payload canonico (64) + firma HMAC hex (<= 256).
    payload_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    signature: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(12), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    # `DYNAMIC`: `now + wallet.qr_ttl_seconds`; `STATIC` de comercio:
    # NULL (sin caducidad estricta); `PERSONAL`: siempre NULL (sin
    # caducidad; el job de `E4-T07` nunca lo vence).
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    merchant: Mapped[MerchantAccount | None] = relationship(back_populates="charges")


BILLER_CATEGORIES = ("ELECTRICIDAD", "AGUA", "GAS", "INTERNET", "TELEFONIA")

SERVICE_PAYMENT_STATUSES = ("PENDING", "PAID", "REJECTED")
TOPUP_STATUSES = ("PENDING", "PAID", "REJECTED")
SERVICE_SUBSCRIPTION_STATUSES = ("ACTIVE", "INACTIVE")


class Biller(Base):
    """Empresa de servicios del catalogo (`03b#95`, E4-T16 HU16).

    Catalogo extensible sin cambios estructurales: agregar `billers`
    es insertar filas (columnas fijas + `required_fields` JSONB con
    los campos que cada empresa requiere, p. ej. `supply_ref` o
    `phone`). `required_fields` usa `sa.JSON()` generico para que los
    tests corran en SQLite; la migracion Postgres usa `JSONB`.
    """

    __tablename__ = "billers"
    __table_args__ = (
        sa.Index("ix_billers_category", "category"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    category: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    required_fields: Mapped[list] = mapped_column(sa.JSON(), nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=True)

    payments: Mapped[list[ServicePayment]] = relationship(
        back_populates="biller",
        lazy="selectin",
    )
    subscriptions: Mapped[list[ServiceSubscription]] = relationship(
        back_populates="biller",
        lazy="selectin",
    )


class ServicePayment(Base):
    """Pago de servicio registrado (`03b#95`, E4-T16 HU16).

    El dinero lo movera el pago de `E4-T17` (motor `SERVICE_PAYMENT`):
    aqui solo el registro (`transaction_id` REF logica a
    `transactions`, sin FK fisica; `biller_id` FK mismo schema;
    `receipt_data` JSONB con el comprobante de `E4-T17`). Dinero
    entero en centimos; nunca `float` (regla 3).
    """

    __tablename__ = "service_payments"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["biller_id"],
            ["wallet.billers.id"],
            name="fk_service_payments_biller",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_service_payments_amount_minor"),
        sa.Index("ix_service_payments_supply_ref", "supply_ref"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # REF logica a `identity.users` (sin FK fisica).
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    biller_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # Numero de suministro (opaco; enmascarar fuera de lo necesario, regla 7).
    supply_ref: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    # Dinero entero en centimos; nunca `float` (regla 3).
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    # REF logica a `transactions.transactions` (sin FK fisica; NULL
    # hasta que `E4-T17` liquida).
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    receipt_data: Mapped[dict | None] = mapped_column(sa.JSON(), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="PENDING", server_default="PENDING"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    biller: Mapped[Biller] = relationship(back_populates="payments")


class Topup(Base):
    """Recarga de celular registrada (`03b#95`, E4-T16 HU16).

    El dinero lo movera la recarga de `E4-T17` (motor `TOPUP`):
    `transaction_id` REF logica (NULL hasta liquidar). `phone` opaco
    (regla 7: nunca en logs en claro). Dinero entero en centimos.
    """

    __tablename__ = "topups"
    __table_args__ = (
        sa.CheckConstraint("amount_minor > 0", name="ck_topups_amount_minor"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # REF logica a `identity.users` (sin FK fisica).
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    operator: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    # Telefono de recarga (opaco; nunca en logs en claro, regla 7).
    phone: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    # Dinero entero en centimos; nunca `float` (regla 3).
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(
        sa.String(3), nullable=False, default="PEN", server_default="PEN"
    )
    # REF logica a `transactions.transactions` (sin FK fisica; NULL
    # hasta que `E4-T17` liquida).
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="PENDING", server_default="PENDING"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class ServiceSubscription(Base):
    """Servicio registrado para recordatorios (`03b#95`, E4-T16 HU16).

    `notify_days_before` default `3` (coherente con HU16); el job que
    lo consume es `E4-T18`. `supply_ref` opaco (regla 7).
    """

    __tablename__ = "service_subscriptions"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["biller_id"],
            ["wallet.billers.id"],
            name="fk_service_subscriptions_biller",
        ),
        sa.CheckConstraint(
            "notify_days_before BETWEEN 1 AND 30",
            name="ck_service_subscriptions_notify_days",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # REF logica a `identity.users` (sin FK fisica).
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    biller_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # Numero de suministro (opaco; nunca en logs en claro, regla 7).
    supply_ref: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    notify_days_before: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, default=3, server_default="3"
    )
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )

    biller: Mapped[Biller] = relationship(back_populates="subscriptions")


QR_PAYMENT_STATUSES = ("PENDING", "PENDING_AUTHORIZATION", "PAID", "REJECTED")


class QrPayment(Base):
    """Pago de un QR por el motor (`03b#94`, E4-T11 HU15 + E4-T21 personal).

    El dinero vive en el motor (`Transaction` + asientos `docs/04#7.4`
    via `pay_express` sin comision); aqui solo el registro del cobro:
    `transaction_id` (REF logica a `transactions`, UQ, sin FK fisica),
    `charge_id` (FK mismo schema), `payer_user_id` (REF logica a
    `identity.users`), `merchant_id` (FK mismo schema, nullable desde
    `0047`: el pago de un QR personal no tiene comercio y deja
    `payee_user_id` con el titular destino). `reusable` se
    denormaliza del cargo al insertar: la guarda de doble cobro solo
    aplica con `false`. Dinero entero en centimos; nunca `float`
    (regla 3).
    """

    __tablename__ = "qr_payments"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["charge_id"],
            ["wallet.qr_charges.id"],
            name="fk_qr_payments_charge",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["wallet.merchant_accounts.id"],
            name="fk_qr_payments_merchant",
        ),
        sa.UniqueConstraint("transaction_id", name="uq_qr_payments_transaction_id"),
        sa.CheckConstraint("amount_minor > 0", name="ck_qr_payments_amount_minor"),
        sa.CheckConstraint(
            "status IN ('PENDING','PENDING_AUTHORIZATION','PAID','REJECTED')",
            name="ck_qr_payments_status",
        ),
        # Guarda de doble cobro (ALTO-2, migracion
        # `0041_wallet_qr_payment_guard`; acotada a un solo uso por
        # `0047_wallet_personal_qr`, E4-T21): un cargo NO reutilizable
        # admite UN solo pago con cobro real/pendiente
        # (`PENDING`/`PENDING_AUTHORIZATION`/`PAID`); los `REJECTED`
        # (sin mover dinero) quedan fuera para el reintento legitimo
        # (MEDIO-2) y los `reusable = true` (QR personal) admiten
        # varios cobros.
        sa.Index(
            "uq_qr_payments_charge_active",
            "charge_id",
            unique=True,
            postgresql_where=sa.text("status <> 'REJECTED' AND reusable = false"),
            sqlite_where=sa.text("status <> 'REJECTED' AND reusable = false"),
        ),
        sa.Index("ix_qr_payments_payer_user_id", "payer_user_id"),
        sa.Index("ix_qr_payments_payee_user_id", "payee_user_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    # REF logica a `transactions.transactions` (sin FK fisica).
    transaction_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    charge_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # REF logica a `identity.users` (sin FK fisica).
    payer_user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # Nullable desde `0047` (E4-T21): el pago de un QR personal no
    # tiene comercio.
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    # Titular destino del QR personal (REF logica a `identity.users`;
    # `NULL` en pagos a comercio).
    payee_user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    # Denormalizado del cargo al insertar (E4-T21): la guarda de doble
    # cobro solo aplica con `false`.
    reusable: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.text("false")
    )
    # Dinero entero en centimos (el monto realmente cobrado); nunca
    # `float` (regla 3). Sigue `NOT NULL`.
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="PENDING", server_default="PENDING"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
