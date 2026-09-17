"""Modelos ORM del schema `identity` (E1-T03, HU01 CA-03).

Fuente: `docs/03b-diccionario-de-datos.md#4-schema-identity` (solo `4.1 users`
y `4.2 credentials`: ninguna tabla de otro modulo),
`docs/03c-modelo-er.md#2-modulo-identity` (agregado `User`),
`docs/modules/README.md#identity`.

Frontera: solo tablas propias (`users`, `credentials`, `kyc_verifications`,
`otp_codes`).
Sin FK entre schemas: `credentials.user_id`, `kyc_verifications.user_id` y
`otp_codes.user_id` son FK contenidas en `identity.users` (mismo schema,
permitido); no hay referencias a `accounts`/`ledger`/`transactions` aqui
(regla de oro 4). Dinero no aplica; nunca `float`.
Prohibido persistir frames/PII biometrica: no hay columnas de imagenes
(`KycVerification` solo guarda resultados + `failure_reason`, E1-T04).

Desviacion documentada de `03b#4.1`: `email` se define como `VARCHAR(320)`
en vez de `CITEXT` porque el repo aun no habilita la extension `citext`
de Postgres (sin precedente en `migrations/`). Unicidad (UQ) y nulabilidad
se mantienen; la comparacion case-insensitive queda para E1-T14 (auth).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

SCHEMA = "identity"

USER_STATUSES = ("PENDING_ACTIVATION", "ACTIVE", "BLOCKED", "CLOSED")
KYC_STATUSES = ("PENDING", "VERIFIED", "REJECTED", "MANUAL_REVIEW")
DOC_TYPES = ("DNI", "CE", "PASSPORT")
#: Propositos de OTP (`docs/03b#1` enum `otp_purpose`).
OTP_PURPOSES = ("ACTIVATION", "RECOVERY", "PAYMENT", "LOGIN")
#: Estados de OTP (`docs/03b#4.5`: `PENDING`/`USED`/`EXPIRED`; `USED` es el
#: estado de consumo de un solo uso — sinonimo documentado `CONSUMED` —).
OTP_STATUSES = ("PENDING", "USED", "EXPIRED")


class User(Base):
    """Usuario del banco (`03b#4.1`). Raiz del agregado `User`.

    Nace en `PENDING_ACTIVATION` (activa en HU02) con `kyc_status`
    `VERIFIED` cuando el alta viene de un KYC exitoso. `doc_number_hash`
    es unico: evita clientes duplicados por documento. Solo se guarda el
    hash y la version enmascarada, nunca el numero en claro en otra
    columna ni PII biometrica.
    """

    __tablename__ = "users"
    __table_args__ = (
        sa.UniqueConstraint("doc_number_hash", name="uq_users_doc_number_hash"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint(
            "doc_type IN ('DNI', 'CE', 'PASSPORT')",
            name="ck_users_doc_type",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_ACTIVATION', 'ACTIVE', 'BLOCKED', 'CLOSED')",
            name="ck_users_status",
        ),
        sa.Index("ix_users_phone", "phone"),
        sa.Index("ix_users_status", "status"),
        sa.Index("ix_users_kyc_status", "kyc_status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    doc_type: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    doc_number_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    doc_number_masked: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    first_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    birth_date: Mapped[date | None] = mapped_column(sa.Date(), nullable=True)
    email: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="PENDING_ACTIVATION",
        server_default="PENDING_ACTIVATION",
    )
    kyc_status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="PENDING", server_default="PENDING",
    )
    risk_profile: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="STANDARD", server_default="STANDARD",
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    credential: Mapped[Credential | None] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )


class Credential(Base):
    """Credenciales del usuario (`03b#4.2`, 1:1 con `users`).

    `password_hash`/`pin_hash` guardan hashes (Argon2/bcrypt), nunca
    secretos en claro. El PIN inicial lo pone `onboard_customer` si llega
    `initial_pin_hash`; si no, lo pone E1-T14 (activacion, HU02).
    """

    __tablename__ = "credentials"
    __table_args__ = (
        sa.CheckConstraint("failed_attempts >= 0", name="ck_credentials_failed_attempts_min"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_credentials_user",
        ),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    password_hash: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    pin_hash: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    biometric_enabled: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default="false",
    )
    failed_attempts: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, default=0, server_default="0",
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True,
    )
    password_updated_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    user: Mapped[User] = relationship(back_populates="credential")


class KycVerification(Base):
    """Resultado de un intento de KYC (`03b#4.4`, E1-T04, HU01 CA-02/CA-04).

    Un registro por intento (exito o fallo con `failure_reason`); retencion:
    no se borra. Solo guarda el resultado (`overall_result`, `*_json` de
    resultado y hash del token de desafio); prohibido persistir frames
    base64 o imagenes biometricas (regla de oro 7): no hay columnas de
    imagen y el repositorio rechaza payloads con material biometrico.

    Desviacion documentada de `03b#4.4`: se agrega `failure_reason TEXT`
    nulable porque E1-T04 exige motivo por intento (CA-04) y `03b#4.4` no
    trae columna dedicada (el resto de columnas es exacto). `user_id`
    nulable segun `03b` (null si aun no existe usuario). Sin FK entre
    schemas: `user_id` referencia `identity.users` (mismo schema,
    permitido por la regla de oro 4).
    """

    __tablename__ = "kyc_verifications"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_kyc_verifications_user",
        ),
        sa.Index("ix_kyc_verifications_user", "user_id"),
        sa.Index("ix_kyc_verifications_created", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    provider: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    overall_result: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False)
    document_json: Mapped[dict | None] = mapped_column(sa.JSON(), nullable=True)
    liveness_json: Mapped[dict | None] = mapped_column(sa.JSON(), nullable=True)
    face_match_json: Mapped[dict | None] = mapped_column(sa.JSON(), nullable=True)
    challenge_token_hash: Mapped[str | None] = mapped_column(
        sa.String(128), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


class OtpCode(Base):
    """Codigo OTP de un solo uso (`03b#4.5`, E1-T08, HU02 CA-01/CA-02/CA-03).

    Columnas exactas de `03b#4.5` (`user_id`, `purpose`, `destination`,
    `code_hash`, `expires_at`, `attempts`, `max_attempts`, `status`,
    `created_at`, `consumed_at`) con dos desviaciones documentadas:

    - `destination` nulable (en `03b` es NOT NULL): `generate_otp` puede
      emitirse antes de conocer el canal (E1-T10 lo completa al reenviar);
      `NULL` significa "canal aun no resuelto".
    - `code_hash` guarda `"salt_hex$sha256_hex"` (32 + 1 + 64 = 97
      caracteres, cabe en `VARCHAR(128)`): nunca el codigo en claro; el
      salt viaja junto al digest porque `03b#4.5` no trae columna de salt
      y anadir una rompería el diccionario.
    - Extension aditiva `resend_count SMALLINT` (default 0): la regla HU02
      "max 3 reenvios" (`config.parameters: otp.max_resends`) exige contar
      reenvios por ciclo; sin esta columna el parametro seria inaplicable.
      Se hereda de fila en fila dentro del ciclo (`generate` = 0).

    `user_id` es FK contenida en `identity.users` (mismo schema, regla de
    oro 4). Sin `float`, sin PII en logs (el servicio jamas loguea el
    codigo ni el destinatario).
    """

    __tablename__ = "otp_codes"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_otp_codes_user",
        ),
        sa.CheckConstraint(
            "purpose IN ('ACTIVATION', 'RECOVERY', 'PAYMENT', 'LOGIN')",
            name="ck_otp_codes_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'USED', 'EXPIRED')",
            name="ck_otp_codes_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_otp_codes_attempts_min"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_otp_codes_max_attempts_min"),
        sa.CheckConstraint(
            "resend_count >= 0", name="ck_otp_codes_resend_count_min"
        ),
        sa.Index("ix_otp_codes_user_purpose_status", "user_id", "purpose", "status"),
        sa.Index("ix_otp_codes_expires_at", "expires_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    purpose: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    destination: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    code_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    attempts: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, default=0, server_default="0",
    )
    max_attempts: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, default=3, server_default="3",
    )
    resend_count: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, default=0, server_default="0",
    )
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="PENDING",
        server_default="PENDING",
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=_utcnow,
        server_default=sa.func.now(),
        nullable=False,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True,
    )


class DeviceBinding(Base):
    """Dispositivo confiable para login biometrico (`03b#4.3`, E1-T13, HU03).

    Columnas exactas de `03b#4.3` (`user_id`, `device_id`, `public_key`,
    `platform`, `biometric_type`, `status`, `registered_at`,
    `last_used_at`) con UQ(`user_id`, `device_id`). `user_id` es FK
    contenida en `identity.users` (mismo schema, regla de oro 4).
    `public_key` guarda la clave publica del dispositivo para verificar la
    firma del `nonce`; la biometria es local al telefono (D07) y aqui nunca
    se persisten frames ni material biometrico (regla de oro 7).

    Formatos aceptados de `public_key` (ver `service/device_login.py`):

    - `"hmac:<hex>"`: secreto HMAC-SHA256 del dispositivo (fallback
      documentado mientras el `.venv` no trae `cryptography`: solo PyJWT,
      que sin `cryptography` no verifica ECDSA/EdDSA).
    - PEM `Ed25519`/`EC`: se verifica con `cryptography` cuando esta
      disponible (ruta preferida en produccion).
    """

    __tablename__ = "device_bindings"
    __table_args__ = (
        sa.UniqueConstraint(
            "user_id", "device_id", name="uq_device_bindings_user_device"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_device_bindings_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_device_bindings_user",
        ),
        sa.Index("ix_device_bindings_user", "user_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    device_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    public_key: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    platform: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    biometric_type: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(15), nullable=False, default="ACTIVE",
        server_default="ACTIVE",
    )
    registered_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True,
    )


class UserSession(Base):
    """Sesion abierta con refresh rotativo (`03b#4.6`, E1-T13, HU03).

    Columnas exactas de `03b#4.6` (`user_id`, `refresh_token_hash` UQ,
    `device_id`, `device_info`, `ip`, `expires_at`, `revocado`, `created_at`)
    con una desviacion documentada:

    - `ip` es `VARCHAR(45)` en vez de `INET`: el `INET` de Postgres no
      existe en el SQLite de las pruebas (`TestClient` + ATTACH); se guarda
      la forma textual (IPv4/IPv6 caben en 45). Nulabilidad e indices segun
      `03b`.

    `refresh_token_hash` guarda el SHA-256 hex del refresh opaco (64
    caracteres, cabe en `VARCHAR(128)`): nunca el token en claro. `user_id`
    es FK contenida en `identity.users` (mismo schema, regla de oro 4).
    Sin `float`.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        sa.UniqueConstraint(
            "refresh_token_hash", name="uq_sessions_refresh_token_hash"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_sessions_user",
        ),
        sa.Index("ix_sessions_user", "user_id"),
        sa.Index("ix_sessions_device", "device_id"),
        sa.Index("ix_sessions_expires_at", "expires_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    device_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    device_info: Mapped[dict | None] = mapped_column(sa.JSON(), nullable=True)
    ip: Mapped[str | None] = mapped_column(sa.String(45), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


#: Estados de `device_bindings` (`03b#4.3`: `ACTIVE`/`REVOKED`).
DEVICE_BINDING_STATUSES = ("ACTIVE", "REVOKED")

#: Plataformas aceptadas (`03b#4.3`: `android`/`ios`).
DEVICE_PLATFORMS = ("android", "ios")

#: Tipos de biometria del dispositivo (`03b#4.3`: `FACE`/`FINGERPRINT`).
BIOMETRIC_TYPES = ("FACE", "FINGERPRINT")


__all__ = [
    "BIOMETRIC_TYPES",
    "DEVICE_BINDING_STATUSES",
    "DEVICE_PLATFORMS",
    "DOC_TYPES",
    "KYC_STATUSES",
    "OTP_PURPOSES",
    "OTP_STATUSES",
    "SCHEMA",
    "USER_STATUSES",
    "Credential",
    "DeviceBinding",
    "KycVerification",
    "OtpCode",
    "User",
    "UserSession",
]
