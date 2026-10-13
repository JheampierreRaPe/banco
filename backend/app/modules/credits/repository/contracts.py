"""Repositorio de documentos contractuales `credits.contracts` (E3-T12, HU11).

Capa de datos sin endpoints ni logica de negocio: solo SQLAlchemy sobre
el schema propio (`credits`), sin `JOIN` entre schemas ni imports de
modelos ajenos (regla de oro 4). Convencion: `flush` sin `commit`
(decide el llamante).

Un juego contractual = tres filas por expediente (`CONTRATO` +
`PAGARE` + `RESUMEN`, UQ por `(application_id, doc_type)`); cada fila
con su `doc_ref` y su `doc_hash` (decision del dueno: tres documentos
separados, no un PDF combinado).

Errores de negocio con `code` estable (`docs/05`, formato de errores):
`NOT_FOUND` (contrato inexistente), `VALIDATION_ERROR` (tipos/valores
invalidos) y `CONFLICT` (duplicado por UQ o estado no generable).

Sin PII ni secretos en logs (regla de oro 7): aqui solo se loguean
`doc_type` y conteos, jamas montos, tasas ni identificadores.
"""

from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.credits.models import CONTRACT_DOC_TYPES, Contract

logger = logging.getLogger(__name__)


class ContractError(ValueError):
    """Error base de contratos (mapeable a 4xx)."""

    code = "CREDITS_CONTRACT_ERROR"


class ContractNotFoundError(ContractError, LookupError):
    """Contrato inexistente o de otro titular (`NOT_FOUND` neutro)."""

    code = "NOT_FOUND"


class ContractValidationError(ContractError):
    """Dato del contrato invalido (`VALIDATION_ERROR`)."""

    code = "VALIDATION_ERROR"


class ContractConflictError(ContractError):
    """Juego duplicado o expediente no generable (`CONFLICT`)."""

    code = "CONFLICT"


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ContractValidationError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def _coerce_doc_type(value: object) -> str:
    if not isinstance(value, str) or value not in CONTRACT_DOC_TYPES:
        raise ContractValidationError(f"doc_type debe ser uno de {list(CONTRACT_DOC_TYPES)}")
    return value


def create_contract(
    session: Session,
    *,
    application_id: uuid.UUID | str,
    doc_type: str,
    doc_ref: str,
    doc_hash: str,
    loan_id: uuid.UUID | str | None = None,
) -> Contract:
    """Crea una fila documental del juego (`GENERATED`, sin firma).

    `doc_ref` (ruta/almacen logica) y `doc_hash` (SHA-256 canonico de
    64 hex) obligatorios; `loan_id` logico opcional (`NULL` en HU11).
    Un `(application_id, doc_type)` duplicado lanza
    `ContractConflictError` (UQ) sin dejar cambios. Hace `flush`, no
    `commit`.
    """
    app_id = _coerce_uuid(application_id, "application_id")
    clean_type = _coerce_doc_type(doc_type)
    if not isinstance(doc_ref, str) or not doc_ref.strip():
        raise ContractValidationError("doc_ref debe ser texto no vacio")
    if not isinstance(doc_hash, str) or len(doc_hash) != 64:
        raise ContractValidationError("doc_hash debe ser SHA-256 en hex (64 caracteres)")
    clean_loan: uuid.UUID | None = None
    if loan_id is not None:
        clean_loan = _coerce_uuid(loan_id, "loan_id")
    row = Contract(
        application_id=app_id,
        loan_id=clean_loan,
        doc_type=clean_type,
        doc_ref=doc_ref.strip(),
        doc_hash=doc_hash.lower(),
        signature_token_hash=None,
        status="GENERATED",
        signed_at=None,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        logger.debug("contracts duplicado doc_type=%s: conflicto UQ", clean_type)
        raise ContractConflictError("el expediente ya tiene generado ese documento") from exc
    return row


def list_by_application(session: Session, application_id: uuid.UUID | str) -> list[Contract]:
    """Juego documental del expediente (orden canonico de tipos)."""
    app_id = _coerce_uuid(application_id, "application_id")
    stmt = sa.select(Contract).where(Contract.application_id == app_id)
    rows = list(session.scalars(stmt).all())
    order = {doc_type: pos for pos, doc_type in enumerate(CONTRACT_DOC_TYPES)}
    rows.sort(key=lambda row: order.get(row.doc_type, len(order)))
    return rows


def get_by_application_and_type(
    session: Session, application_id: uuid.UUID | str, doc_type: str
) -> Contract | None:
    """Un documento del juego (`None` si no existe)."""
    app_id = _coerce_uuid(application_id, "application_id")
    clean_type = _coerce_doc_type(doc_type)
    stmt = sa.select(Contract).where(
        Contract.application_id == app_id, Contract.doc_type == clean_type
    )
    return session.scalars(stmt).one_or_none()


def count_by_application(session: Session, application_id: uuid.UUID | str) -> int:
    """Conteo de documentos del expediente (trazabilidad/idempotencia)."""
    app_id = _coerce_uuid(application_id, "application_id")
    stmt = sa.select(sa.func.count()).select_from(Contract).where(Contract.application_id == app_id)
    return int(session.scalar(stmt) or 0)


def find_by_token_hash(session: Session, token_hash: str) -> Contract | None:
    """Fila firmada con ese token (`None` si el token no se uso).

    Segunda capa de la regla de no reutilizacion (E3-T13, HU11): el
    `nonce` de un solo uso ya impide reusar la firma; esta igualdad
    detecta un token repetido en otro contrato/firma. Solo lectura
    (basta la primera coincidencia: las filas de un mismo juego
    comparten token).
    """
    if not isinstance(token_hash, str) or not token_hash.strip():
        return None
    stmt = sa.select(Contract).where(Contract.signature_token_hash == token_hash.strip().lower())
    return session.scalars(stmt).first()


__all__ = [
    "ContractConflictError",
    "ContractError",
    "ContractNotFoundError",
    "ContractValidationError",
    "count_by_application",
    "create_contract",
    "find_by_token_hash",
    "get_by_application_and_type",
    "list_by_application",
]
