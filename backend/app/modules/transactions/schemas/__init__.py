"""Esquemas Pydantic de transferencias (E2-T07, HU06 CA-01/CA-03).

Fuente: `docs/05-contratos-api.md#63` (`POST /transfers/own`,
`POST /transfers/third-party`) y formato `05#4` (`{"data", "meta"}`).

Reglas:
- Dinero entero en centimos (`amount_minor >= 1`, `fee_minor >= 0`);
  nunca `float` (regla de oro 3).
- Moneda ISO-4217 (`^[A-Z]{3}$`).
- Solo UUIDs de cuentas (la API nunca expone numeros en claro, regla 9).
- `Idempotency-Key` obligatoria (cabecera, regla de oro 5; la exige el
  router antes de ejecutar).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

CURRENCY_PATTERN = r"^[A-Z]{3}$"


class TransferOwnRequest(BaseModel):
    """`POST /transfers/own`: entre cuentas del mismo titular (sin comision)."""

    source_account_id: uuid.UUID
    target_account_id: uuid.UUID
    amount_minor: int = Field(..., ge=1, description="Monto en centimos (> 0)")
    currency: str = Field(..., pattern=CURRENCY_PATTERN, description="ISO-4217")
    external_ref: str | None = Field(default=None, max_length=80)
    metadata: dict | None = None


class TransferThirdPartyRequest(BaseModel):
    """`POST /transfers/third-party`: a otra cuenta del banco (con comision)."""

    source_account_id: uuid.UUID
    target_account_id: uuid.UUID
    amount_minor: int = Field(..., ge=1, description="Monto en centimos (> 0)")
    currency: str = Field(..., pattern=CURRENCY_PATTERN, description="ISO-4217")
    fee_minor: int | None = Field(
        default=None,
        ge=0,
        description="Comision en centimos; None = `transfer.third_party_fee_minor`",
    )
    external_ref: str | None = Field(default=None, max_length=80)
    metadata: dict | None = None


class TransferData(BaseModel):
    """Detalle de la transferencia creada (o rechazada por fondos)."""

    transaction_id: uuid.UUID
    type: str
    status: str
    source_account_id: uuid.UUID | None = None
    target_account_id: uuid.UUID | None = None
    amount_minor: int
    currency: str
    fee_minor: int = 0
    failure_code: str | None = Field(
        default=None,
        description="`INSUFFICIENT_FUNDS`/`DAILY_LIMIT_EXCEEDED`/"
        "`BIOMETRIC_PREVENTIVE_BLOCK` cuando `status` es `REJECTED`",
    )
    nonce: str | None = Field(
        default=None,
        description="`nonce` a firmar con el dispositivo cuando `status` es "
        "`PENDING_AUTHORIZATION` (E2-T19, HU08); `None` en otro caso",
    )
    expires_in: int | None = Field(
        default=None,
        description="Segundos de vigencia del `nonce` (E2-T19, HU08)",
    )


class TransferResponse(BaseModel):
    """Envoltorio `05#4` (`data` + `meta` con `request_id` y `replayed`)."""

    data: TransferData
    meta: dict = Field(default_factory=dict)


class InterbankTransferRequest(BaseModel):
    """`POST /transfers/interbank`: salida a otro banco (E2-T09, HU06).

    El titular NO se confia del cliente: `cci` + `holder_name` se validan
    contra la fachada `validate_holder` (E2-T08) antes de mover dinero.
    """

    source_account_id: uuid.UUID
    amount_minor: int = Field(..., ge=1, description="Monto en centimos (> 0)")
    currency: str = Field(..., pattern=CURRENCY_PATTERN, description="ISO-4217")
    cci: str = Field(..., min_length=20, max_length=24, description="CCI destino (20 digitos)")
    holder_name: str | None = Field(
        default=None, max_length=150, description="Titular esperado (debe coincidir)"
    )
    bank_code: str | None = Field(default=None, max_length=6, description="Banco destino")
    fee_minor: int | None = Field(
        default=None,
        ge=0,
        description="Comision en centimos; None = `transfer.interbank_fee_minor`",
    )
    external_ref: str | None = Field(
        default=None, max_length=80, description="Referencia externa unica (se genera si falta)"
    )
    metadata: dict | None = None


class InterbankTransferData(BaseModel):
    """Detalle de la saga (`202` + seguimiento por `GET /transactions/{id}`)."""

    transaction_id: uuid.UUID
    type: str
    status: str
    source_account_id: uuid.UUID | None = None
    amount_minor: int
    currency: str
    fee_minor: int = 0
    external_ref: str | None = None
    tracking: dict = Field(default_factory=dict)
    failure_code: str | None = Field(
        default=None,
        description="`INSUFFICIENT_FUNDS`/`TRANSFER_TIMEOUT`/`INTERBANK_REJECTED`",
    )
    nonce: str | None = Field(
        default=None,
        description="`nonce` a firmar con el dispositivo cuando `status` es "
        "`PENDING_AUTHORIZATION` (E2-T19, HU08); `None` en otro caso",
    )
    expires_in: int | None = Field(
        default=None,
        description="Segundos de vigencia del `nonce` (E2-T19, HU08)",
    )


class InterbankTransferResponse(BaseModel):
    """Envoltorio `05#4` (`data` + `meta` con `request_id` y `replayed`)."""

    data: InterbankTransferData
    meta: dict = Field(default_factory=dict)


class TransactionHistoryItem(BaseModel):
    """Un cambio de estado del historial (`transaction_status_history`)."""

    from_status: str | None = None
    to_status: str
    reason: str | None = None
    actor_type: str | None = None
    created_at: str | None = None


class TransactionDetailData(BaseModel):
    """Detalle y seguimiento de una transaccion (`GET /transactions/{id}`)."""

    transaction_id: uuid.UUID
    type: str
    status: str
    source_account_id: uuid.UUID | None = None
    target_account_id: uuid.UUID | None = None
    amount_minor: int
    currency: str
    fee_minor: int = 0
    external_ref: str | None = None
    tracking: dict = Field(default_factory=dict)
    failure_code: str | None = None
    history: list[TransactionHistoryItem] = Field(default_factory=list)


class TransactionDetailResponse(BaseModel):
    """Envoltorio `05#4` para el detalle (`data` + `meta`)."""

    data: TransactionDetailData
    meta: dict = Field(default_factory=dict)


class AuthorizeTransactionRequest(BaseModel):
    """`POST /transactions/{id}/authorize`: firma del `nonce` (E2-T19, HU08 CA-01).

    `device_id` + `signature` obligatorios; `nonce` opcional como eco del
    reto vigente: si se envia y no coincide con el almacenado, el intento
    se rechaza generico (y cuenta como fallido). La verificacion siempre
    usa el reto almacenado en la transaccion (un solo uso, TTL corto).
    Sin material biometrico: solo la firma del reto.
    """

    device_id: str = Field(..., min_length=1, max_length=128)
    signature: str = Field(..., min_length=1, max_length=4096)
    nonce: str | None = Field(default=None, max_length=256)


class AuthorizeTransactionData(BaseModel):
    """Nuevo estado tras autorizar (con `tracking` si es interbancaria)."""

    transaction_id: uuid.UUID
    status: str
    tracking: dict = Field(default_factory=dict)
    failure_code: str | None = Field(
        default=None,
        description="`BIOMETRIC_PREVENTIVE_BLOCK` cuando el bloqueo preventivo "
        "deja `status` en `REJECTED` (E2-T17, HU08 CA-02)",
    )


class AuthorizeTransactionResponse(BaseModel):
    """Envoltorio `05#4` (`data` + `meta` con `request_id` y `replayed`)."""

    data: AuthorizeTransactionData
    meta: dict = Field(default_factory=dict)


class ChallengeTransactionData(BaseModel):
    """Reto vigente tras rotar (`POST .../authorize/challenge`, HU08).

    Solo el `nonce` nuevo + su vigencia: el cliente lo firma con el
    dispositivo y lo envia a `POST .../authorize` con una
    `Idempotency-Key` NUEVA (la anterior snapshotteo el `EXPIRED_NONCE`).
    """

    transaction_id: uuid.UUID
    nonce: str
    expires_in: int


class ChallengeTransactionResponse(BaseModel):
    """Envoltorio `05#4` (`data` + `meta` con `request_id`)."""

    data: ChallengeTransactionData
    meta: dict = Field(default_factory=dict)


class ReverseTransactionRequest(BaseModel):
    """`POST /transactions/{id}/reverse`: motivo opcional (E2-T25, HU06/HU17).

    Cuerpo opcional: sin cuerpo se reversa sin motivo registrado. El motivo
    es solo trazabilidad (viaja al historial, nunca a logs).
    """

    reason: str | None = Field(
        default=None, max_length=280, description="Motivo del reverso (opcional)"
    )


class ReverseTransactionData(BaseModel):
    """Resultado del reverso: operacion en `REVERSED` + asiento inverso."""

    transaction_id: uuid.UUID
    status: str
    reversal_entry_id: uuid.UUID | None = Field(
        default=None,
        description="Asiento inverso principal (el de liquidacion; `None` solo "
        "en rutas delegadas sin traza directa)",
    )
    reverses_entry_id: uuid.UUID | None = Field(
        default=None, description="Asiento original compensado (principal)"
    )
    reversal_entry_ids: list[uuid.UUID] = Field(
        default_factory=list, description="Todos los asientos inversos creados"
    )
    reverses_entry_ids: list[uuid.UUID] = Field(
        default_factory=list, description="Todos los asientos originales compensados"
    )


class ReverseTransactionResponse(BaseModel):
    """Envoltorio `05#4` (`data` + `meta` con `request_id` y `replayed`)."""

    data: ReverseTransactionData
    meta: dict = Field(default_factory=dict)


class TransactionListItem(BaseModel):
    """Fila del historial (`GET /transactions`, E2-T24, HU06/HU15/HU17).

    Cuentas siempre enmascaradas (`****1234`; interbancaria sin cuenta
    destino: `CCI **** (banco <code>)`); sin PII ni numeros en claro.
    """

    transaction_id: uuid.UUID
    type: str
    status: str
    amount_minor: int
    fee_minor: int = 0
    currency: str
    created_at: str | None = None
    settled_at: str | None = None
    source_account_masked: str | None = None
    target_account_masked: str | None = None
    external_ref: str | None = None


class TransactionListResponse(BaseModel):
    """Envoltorio `05#4` para el listado (`data` + `meta` de paginacion)."""

    data: list[TransactionListItem] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)


class ResolveTransferTargetDocument(BaseModel):
    """`target_document` del lookup previo (E2-T27, HU06).

    Campos en texto libre opcional: el formato (`DNI` 8 digitos, `CE`/`PASSPORT`
    6-12 alfanumericos, tipo en `DNI|CE|PASSPORT`) lo valida el servicio
    (`422 VALIDATION_ERROR` uniforme); el schema no rechaza por patron para
    dar un unico codigo de error.
    """

    type: str | None = Field(default=None, description="`DNI|CE|PASSPORT`")
    number: str | None = Field(default=None, description="Numero de documento (sin formato fijo)")


class ResolveTransferTargetRequest(BaseModel):
    """`POST /transfers/third-party/resolve-target` (E2-T27, HU06).

    Exactamente UNO de `target_document` / `target_phone` (ambos o ninguno
    -> `422 VALIDATION_ERROR` desde el servicio). Solo lectura: sin
    `Idempotency-Key`.
    """

    target_document: ResolveTransferTargetDocument | None = Field(default=None)
    target_phone: str | None = Field(default=None)


class ResolveTransferTargetData(BaseModel):
    """Destino resuelto: referencia + mascaras (sin PII completa)."""

    target_account_id: uuid.UUID
    holder_name_masked: str
    account_number_masked: str
    currency: str


class ResolveTransferTargetResponse(BaseModel):
    """Envoltorio `05#4` (`data` + `meta` con `request_id`)."""

    data: ResolveTransferTargetData
    meta: dict = Field(default_factory=dict)


__all__ = [
    "AuthorizeTransactionData",
    "AuthorizeTransactionRequest",
    "AuthorizeTransactionResponse",
    "ChallengeTransactionData",
    "ChallengeTransactionResponse",
    "InterbankTransferData",
    "InterbankTransferRequest",
    "InterbankTransferResponse",
    "ResolveTransferTargetData",
    "ResolveTransferTargetDocument",
    "ResolveTransferTargetRequest",
    "ResolveTransferTargetResponse",
    "ReverseTransactionData",
    "ReverseTransactionRequest",
    "ReverseTransactionResponse",
    "TransactionDetailData",
    "TransactionDetailResponse",
    "TransactionHistoryItem",
    "TransactionListItem",
    "TransactionListResponse",
    "TransferData",
    "TransferOwnRequest",
    "TransferResponse",
    "TransferThirdPartyRequest",
]
