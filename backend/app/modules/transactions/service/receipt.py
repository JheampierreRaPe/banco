"""Comprobante de transferencia en PDF (E2-T10, HU06 CA-04).

Vista de SOLO lectura sobre `transactions` (+ fachada `accounts` para
enmascarar): no recalcula saldos, no crea asientos ni filas nuevas
(`docs/03c#14`); la fuente de verdad es `ledger`/`transactions`.

- Numero de operacion: derivado determinista del `transaction_id`
  (`OP-` + 12 hex de `sha256`, mayusculas): unico por transaccion,
  estable entre reintentos, no secuencial/adivinable. No se persiste
  (no hay migracion): repetir la consulta nunca genera uno nuevo.
- Solo emitible en `POSTED`/`SETTLED`/`CONCILIATED`; en otro estado
  (`REJECTED`/`FAILED`/intermedios) se rechaza con
  `ReceiptNotIssuableError` (-> 409, no se emite comprobante de dinero
  no movido).
- Cuentas siempre enmascaradas (`****1234`, patron
  `accounts.service.mask_account_number`); interbancaria: el CCI nunca
  se persiste en claro (E2-T09: solo `cci_hash`), asi que el destino se
  muestra como `CCI **** (banco <code>)` + titular enmascarado.
- Sin PII en logs (regla de oro 7): solo UUIDs de transaccion/estado.
- PDF real con `reportlab` (ya es dependencia del backend); sin backend
  PDF disponible -> `ReceiptFormatUnavailableError` (-> 501
  `RECEIPT_FORMAT_NOT_SUPPORTED`, patron E2-T03).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.modules.transactions.models import Transaction

logger = logging.getLogger(__name__)

#: Estados con dinero efectivamente ejecutado (emitibles).
ISSUABLE_STATUSES = frozenset({"POSTED", "SETTLED", "CONCILIATED"})

#: Prefijo del numero de operacion (no secuencial, no adivinable).
OPERATION_NUMBER_PREFIX = "OP-"
_OPERATION_DIGEST_CHARS = 12


class ReceiptNotIssuableError(ValueError):
    """La transaccion no esta en estado emitible (-> 409)."""


class ReceiptFormatUnavailableError(RuntimeError):
    """Sin backend PDF disponible (-> 501)."""


def operation_number_of(transaction_id: uuid.UUID | str) -> str:
    """Numero de operacion unico y estable derivado del `transaction_id`.

    `sha256(str(uuid))` truncado a 12 hex en mayusculas con prefijo
    `OP-` (p. ej. `OP-3F9A1C2E4B5D`): determinista (mismo input, mismo
    numero), unico por transaccion (colision despreciable) y no
    secuencial. Dominio puro, sin BD.
    """
    try:
        uid = (
            transaction_id
            if isinstance(transaction_id, uuid.UUID)
            else uuid.UUID(str(transaction_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"transaction_id debe ser UUID, recibido: {transaction_id!r}") from exc
    digest = hashlib.sha256(str(uid).encode("ascii")).hexdigest()[:_OPERATION_DIGEST_CHARS]
    return f"{OPERATION_NUMBER_PREFIX}{digest.upper()}"


def pdf_backend_available() -> str | None:
    """Backend PDF disponible (`reportlab`) o `None` (patron E2-T03)."""
    if importlib.util.find_spec("reportlab") is not None:
        return "reportlab"
    return None


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _masked_account(session: Session, account_id: uuid.UUID | None) -> str | None:
    """Cuenta enmascarada via fachada `accounts` (jamas en claro)."""
    if account_id is None:
        return None
    from app.modules.accounts.service import get_account as _get_account_info
    from app.modules.accounts.service import mask_account_number as _mask

    try:
        info = _get_account_info(session, account_id)
    except (ValueError, AttributeError, TypeError):
        return "****"
    if info is None:
        return "****"
    try:
        return _mask(str(info.get("account_number") or ""))
    except (ValueError, AttributeError, TypeError):
        return "****"


def build_receipt(session: Session, tx: Transaction) -> dict[str, Any]:
    """Arma los datos del comprobante (solo lectura, sin `flush`/`commit`).

    Lanza `ReceiptNotIssuableError` si el estado no es emitible. Nunca
    incluye numeros de cuenta en claro, documento, CCI ni titular sin
    enmascarar.
    """
    if tx.status not in ISSUABLE_STATUSES:
        raise ReceiptNotIssuableError(
            f"comprobante no emitible en estado {tx.status} (solo POSTED/SETTLED/CONCILIATED)"
        )
    meta = tx.meta if isinstance(tx.meta, dict) else {}
    interbank = meta.get("interbank") if isinstance(meta.get("interbank"), dict) else {}
    destination_masked: str | None
    holder_masked: str | None = None
    bank_code: str | None = None
    if tx.target_account_id is not None:
        destination_masked = _masked_account(session, tx.target_account_id)
    else:
        bank_code = str(interbank.get("bank_code") or "") or None
        holder_masked = str(interbank.get("holder_name_masked") or "") or None
        destination_masked = f"CCI **** (banco {bank_code})" if bank_code else "CCI ****"
    amount_minor = int(tx.amount_minor)
    fee_minor = int(tx.fee_minor or 0)
    return {
        "operation_number": operation_number_of(tx.id),
        "transaction_id": str(tx.id),
        "type": tx.type,
        "status": tx.status,
        "amount_minor": amount_minor,
        "fee_minor": fee_minor,
        "total_minor": amount_minor + fee_minor,
        "currency": tx.currency,
        "source_account_masked": _masked_account(session, tx.source_account_id),
        "destination_account_masked": destination_masked,
        "holder_name_masked": holder_masked,
        "bank_code": bank_code,
        "external_ref": tx.external_ref,
        "created_at": _iso_utc(tx.created_at),
        "settled_at": _iso_utc(tx.settled_at),
    }


def render_receipt_pdf(data: dict[str, Any]) -> bytes:
    """Renderiza el comprobante a PDF (`reportlab`); 501 si no hay backend.

    Etiquetas solo ASCII (fuentes estandar PDF) y `pageCompression=0`
    para que el contenido sea verificable en pruebas.
    """
    if pdf_backend_available() is None:
        raise ReceiptFormatUnavailableError(
            "comprobante pdf no disponible: instale reportlab para emitir el comprobante"
        )
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def _text(value: Any) -> str:
        if value is None:
            return "-"
        return str(value)

    rows = [
        ("Numero de operacion", _text(data.get("operation_number"))),
        ("Transaccion", _text(data.get("transaction_id"))),
        ("Tipo", _text(data.get("type"))),
        ("Estado", _text(data.get("status"))),
        ("Monto (centimos)", _text(data.get("amount_minor"))),
        ("Comision (centimos)", _text(data.get("fee_minor"))),
        ("Total debitado (centimos)", _text(data.get("total_minor"))),
        ("Moneda", _text(data.get("currency"))),
        ("Cuenta origen", _text(data.get("source_account_masked"))),
        ("Cuenta destino", _text(data.get("destination_account_masked"))),
        ("Titular destino", _text(data.get("holder_name_masked"))),
        ("Referencia externa", _text(data.get("external_ref"))),
        ("Creada (UTC)", _text(data.get("created_at"))),
        ("Liquidada (UTC)", _text(data.get("settled_at"))),
    ]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Comprobante de transferencia")
    doc.pageCompression = 0
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Comprobante de transferencia", styles["Heading1"]),
        Spacer(1, 12),
        Table(
            [[Paragraph(k, styles["Normal"]), Paragraph(v, styles["Normal"])] for k, v in rows],
            colWidths=[170, 340],
            style=TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]
            ),
        ),
    ]
    doc.build(story)
    return buf.getvalue()


def get_receipt_pdf(session: Session, tx: Transaction) -> tuple[dict[str, Any], bytes]:
    """Caso de uso: valida estado, arma datos y renderiza (solo lectura)."""
    data = build_receipt(session, tx)
    logger.info("receipt build transaction_id=%s status=%s", tx.id, tx.status)
    return data, render_receipt_pdf(data)


__all__ = [
    "ISSUABLE_STATUSES",
    "OPERATION_NUMBER_PREFIX",
    "ReceiptFormatUnavailableError",
    "ReceiptNotIssuableError",
    "build_receipt",
    "get_receipt_pdf",
    "operation_number_of",
    "pdf_backend_available",
    "render_receipt_pdf",
]
