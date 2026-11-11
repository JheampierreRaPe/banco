"""Modelos ORM del schema `risk` (E2-T18, minimo HU08; HU20 lo amplia).

Fuente: `docs/03b-diccionario-de-datos.md#112-risk_profiles` (columnas ya
fijadas; no pisar el esquema que defina HU20, D-16). Frontera: solo
`risk.risk_profiles`; `alerts`/`risk_rules` los crea HU20 (E6-T03/E6-T04,
Sprint 4). Sin FK entre schemas: `user_id` es UUID logico (REF a
`identity.users`, sin constraint fisico).

`usual_countries` (`TEXT[]`) y `usual_hours` (`SMALLINT[]`) usan
`postgresql.ARRAY` con variante `sa.JSON()` para SQLite, asi los tests
unitarios corren en memoria (`shared/models` usa el mismo truco para
`JSON`/`JSONB`); en Postgres rige `ARRAY` (sin drift en `alembic check`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

SCHEMA = "risk"

_TextArray = postgresql.ARRAY(sa.Text()).with_variant(sa.JSON(), "sqlite")
_SmallIntArray = postgresql.ARRAY(sa.SmallInteger()).with_variant(sa.JSON(), "sqlite")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RiskProfile(Base):
    """Perfil de comportamiento del usuario (`03b#112`).

    Lo consulta el motor (E2-T17: `avg_amount_minor`, `score`, hábitos)
    y lo alimenta el historial (HU20 lo ampliara). `user_id` es PK:
    un usuario tiene como maximo un perfil (el upsert nunca duplica).
    """

    __tablename__ = "risk_profiles"
    __table_args__ = ({"schema": SCHEMA},)

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    avg_amount_minor: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default="0"
    )
    usual_countries: Mapped[list | None] = mapped_column(_TextArray, nullable=True)
    usual_hours: Mapped[list | None] = mapped_column(_SmallIntArray, nullable=True)
    last_device_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    score: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )


#: Acciones validas de una regla (`03b#11.1`, `docs/11#HU20`).
RULE_ACTIONS = frozenset({"ALERT", "HOLD", "BLOCK"})


class RiskRule(Base):
    """Regla antifraude configurable (`03b#11.1`, E6-T01, HU20).

    `code` es inmutable y unico (identifica la regla en la
    evaluacion de `E6-T02`); `name`/`enabled`/`priority`/`params_json`/
    `action` son editables por el analista via `PUT /risk/rules/{code}`
    y se reflejan sin reiniciar. `params_json` guarda SOLO umbrales
    (sin PII). Sin FK entre schemas: `updated_by` es UUID logico (REF
    a `identity.users`, sin constraint fisico).
    """

    __tablename__ = "risk_rules"
    __table_args__ = (
        sa.Index("ix_risk_rules_enabled", "enabled"),
        sa.Index("ix_risk_rules_priority", "priority"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(sa.String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=True, server_default="true"
    )
    priority: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, default=100, server_default="100"
    )
    params_json: Mapped[dict] = mapped_column(
        sa.JSON(), nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    action: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="ALERT", server_default="ALERT"
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
    updated_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
