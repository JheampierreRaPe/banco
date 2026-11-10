"""Generacion de documentos contractuales (E3-T12, HU11 CA-01).

Con un expediente `APPROVED` genera de forma **determinista** el juego
de tres documentos separados (**contrato + pagare + hoja resumen**,
decision del dueno) con las condiciones aprobadas (monto, plazo,
TEA/TCEA, cronograma del motor puro E3-T01), persiste las tres filas
`credits.contracts` en `GENERATED` (cada una con su `doc_ref` +
`doc_hash`) y emite `credit.contract.generated` por `outbox`.

- Las condiciones se **copian** del expediente + producto + motor; no
  se recalculan reglas ni se re-evalua al solicitante.
- `doc_hash` = SHA-256 del **contenido canonico** (JSON con claves
  ordenadas, montos en centimos `int`, tasas como strings decimales
  exactos): mismas condiciones -> mismo hash (reproducible).
- El PDF sigue el patron `simulation_pdf`/`receipt` (E3-T03/E2-T10):
  sin backend PDF la generacion/persistencia sigue intacta y solo la
  descarga falla con `501 EXPORT_FORMAT_NOT_SUPPORTED`.
- **No mueve dinero**: no crea `loans`/`loan_schedules`, no toca
  `transactions`/`ledger`, no hay asientos.
- Idempotente: un juego por expediente (UQ por
  `(application_id, doc_type)`); regenerar no duplica filas ni eventos.

Sin PII ni secretos en logs (regla de oro 7): los logs llevan
`doc_type` y conteos, jamas montos, tasas ni el contenido del
documento. Convencion: `flush` sin `commit`; el endpoint confirma.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from html import escape as _escape
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.credits import events as credit_events
from app.modules.credits import repository as credits_repo
from app.modules.credits.domain import financial
from app.modules.credits.models import (
    CONTRACT_DOC_SLUGS,
    CONTRACT_DOC_TYPES,
    Contract,
    LoanApplication,
)
from app.modules.credits.repository import contracts as contracts_repo
from app.modules.credits.repository.contracts import (
    ContractConflictError,
    ContractError,
    ContractNotFoundError,
    ContractValidationError,
)
from app.modules.credits.service.simulation_pdf import pdf_available

logger = logging.getLogger(__name__)

#: Endpoint logico para la idempotencia del `POST` (clave `shared`, UQ por usuario).
IDEMPOTENCY_ENDPOINT = "/credits/applications/contract"

#: Version del contenido canonico (cambia el hash si cambia el formato).
CANONICAL_VERSION = "credit-contract/v1"

#: `doc` publico (query `GET`) por tipo documental interno.
PUBLIC_DOC_BY_TYPE: dict[str, str] = {
    "CONTRATO": "contrato",
    "PAGARE": "pagare",
    "RESUMEN": "resumen",
}
TYPE_BY_PUBLIC_DOC: dict[str, str] = {v: k for k, v in PUBLIC_DOC_BY_TYPE.items()}


class ContractPdfUnavailableError(RuntimeError):
    """Sin backend PDF instalado (la descarga responde 501)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_owner(owner_user_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(owner_user_id, uuid.UUID):
        return owner_user_id
    try:
        return uuid.UUID(str(owner_user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ContractValidationError(
            f"user_id debe ser UUID, recibido: {owner_user_id!r}"
        ) from exc


def _minor_to_str(value: int) -> str:
    whole, cents = divmod(int(value), 100)
    return f"{whole:,}.{cents:02d}".replace(",", "_").replace(".", ",").replace("_", ".")


def _rate_to_pct(rate: Decimal | str) -> str:
    return f"{(Decimal(str(rate)) * 100):.4f} %".replace(".", ",")


def _product_dict(session: Session, product_id: object) -> dict[str, Any]:
    from app.modules.credits import service as catalog

    try:
        return catalog.get_product(session, product_id=product_id)  # type: ignore[arg-type]
    except credits_repo.LoanProductNotFoundError as exc:
        raise ContractNotFoundError("producto del expediente inexistente") from exc
    except credits_repo.LoanProductError as exc:
        raise ContractValidationError(str(exc)) from exc


def _load_own_approved(
    session: Session, owner: uuid.UUID, application_id: object
) -> LoanApplication:
    """Expediente `APPROVED` del titular (404 neutro / 409 si no genera)."""
    from app.modules.credits.repository import applications as apps_repo

    row = apps_repo.get_application(session, application_id)  # type: ignore[arg-type]
    if row is None or row.user_id != owner:
        raise ContractNotFoundError("expediente inexistente")
    if row.status != "APPROVED":
        raise ContractConflictError(
            f"el expediente en estado {row.status} no admite generacion de contrato"
        )
    return row


def build_context(session: Session, row: LoanApplication) -> dict[str, Any]:
    """Foto determinista de condiciones (expediente + producto + motor).

    Tasa = `tea` del producto (o su `tcea` si `tea` es `NULL`, mismo
    criterio que el simulador E3-T03); desgravamen = `insurance_rate`
    del producto cuando es > 0. Cronograma del motor puro E3-T01
    (cuotas sin `due_date`: aun no hay desembolso, HU12).
    """
    product = _product_dict(session, row.product_id)
    annual_rate: Decimal = product["tea"] if product["tea"] is not None else product["tcea"]
    insurance_rate: Decimal = product["insurance_rate"]
    if insurance_rate < 0:  # pragma: no cover - defensa en profundidad
        raise ContractValidationError("insurance_rate del producto invalido")
    sim = financial.simulate(
        int(row.requested_amount_minor),
        annual_rate,
        int(row.term_months),
        insurance_rate,
    )
    return {
        "application_id": str(row.id),
        "user_id": str(row.user_id),
        "product": {"code": product["code"], "name": product["name"]},
        "requested_amount_minor": int(row.requested_amount_minor),
        "term_months": int(row.term_months),
        "tea": str(annual_rate),
        "tcea": str(sim.tcea),
        "insurance_rate": str(insurance_rate),
        "payment_minor": int(sim.payment_minor),
        "total_principal_minor": int(sim.total_principal_minor),
        "total_interest_minor": int(sim.total_interest_minor),
        "total_insurance_minor": int(sim.total_insurance_minor),
        "total_paid_minor": int(sim.total_paid_minor),
        "schedule": [
            {
                "installment_no": item.installment_no,
                "due_date": None,
                "principal_minor": int(item.principal_minor),
                "interest_minor": int(item.interest_minor),
                "insurance_minor": int(item.insurance_minor),
                "total_minor": int(item.total_minor),
                "balance_minor": int(item.balance_minor),
            }
            for item in sim.schedule
        ],
        "decision_reason": row.decision_reason,
    }


def canonical_for(doc_type: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Contenido canonico del documento (estable para el hash)."""
    if doc_type not in CONTRACT_DOC_TYPES:
        raise ContractValidationError(f"doc_type debe ser uno de {list(CONTRACT_DOC_TYPES)}")
    return {"doc": CANONICAL_VERSION, "doc_type": doc_type, **ctx}


def doc_hash_for(canonical: dict[str, Any]) -> str:
    """SHA-256 del contenido canonico (JSON ordenado, reproducible)."""
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def doc_ref_for(application_id: object, doc_type: str) -> str:
    """Referencia logica del documento (ruta/almacen, sin PII)."""
    slug = CONTRACT_DOC_SLUGS[doc_type]
    return f"credits/contracts/{application_id}/{slug}.pdf"


def _contract_clauses(ctx: dict[str, Any]) -> list[str]:
    product = ctx["product"]
    return [
        (
            "PRIMERA (Partes): el presente contrato de credito vincula al titular del "
            f"expediente {ctx['application_id']} (EL CLIENTE) con la entidad (CUYCASH), "
            f"bajo el producto {product['code']} - {product['name']}."
        ),
        (
            "SEGUNDA (Monto y plazo): capital de "
            f"{_minor_to_str(ctx['requested_amount_minor'])} "
            f"a {ctx['term_months']} meses, con TEA {_rate_to_pct(ctx['tea'])} y TCEA "
            f"{_rate_to_pct(ctx['tcea'])}."
        ),
        (
            "TERCERA (Cuota): cuota base francesa de "
            f"{_minor_to_str(ctx['payment_minor'])} "
            "mas desgravamen cuando aplique; total a pagar "
            f"{_minor_to_str(ctx['total_paid_minor'])} (capital "
            f"{_minor_to_str(ctx['total_principal_minor'])}, interes "
            f"{_minor_to_str(ctx['total_interest_minor'])}, seguro "
            f"{_minor_to_str(ctx['total_insurance_minor'])})."
        ),
        (
            "CUARTA (Desembolso): el desembolso se ejecuta tras la firma del presente "
            "contrato (HU12); sin firma no hay acreditacion."
        ),
        (
            "QUINTA (Cronograma): las cuotas del anexo son parte integrante; la ultima "
            "absorbe el residuo de redondeo."
        ),
        (
            "SEXTA (Prepago): EL CLIENTE puede pre-pagar total o parcialmente sin "
            "penalidad distinta del interes devengado."
        ),
        (
            "SETIMA (Mora): el incumplimiento habilita el cobro segun la normativa "
            "vigente y lo informado en la hoja resumen."
        ),
        (
            "OCTAVA (Integridad): este ejemplar se identifica por su hash "
            "SHA-256 canonico; cualquier alteracion lo invalida."
        ),
    ]


def _pagare_lines(ctx: dict[str, Any]) -> list[str]:
    return [
        "PAGARE A LA ORDEN",
        (
            f"Por {_minor_to_str(ctx['requested_amount_minor'])} (capital) me comprometo a "
            "pagar incondicionalmente a la orden de CUYCASH, en "
            f"{ctx['term_months']} cuotas mensuales segun el cronograma del contrato del "
            f"expediente {ctx['application_id']}."
        ),
        (
            f"TEA {_rate_to_pct(ctx['tea'])} | TCEA {_rate_to_pct(ctx['tcea'])} | "
            f"Total a pagar {_minor_to_str(ctx['total_paid_minor'])}."
        ),
        f"Producto: {ctx['product']['code']} - {ctx['product']['name']}.",
        (
            "Lugar y fecha: los del desembolso (HU12). Firma del emitente: "
            "______________. Huella/token biometrico: se vincula al firmar (E3-T13)."
        ),
    ]


def _resumen_lines(ctx: dict[str, Any]) -> list[str]:
    product = ctx["product"]
    return [
        "HOJA RESUMEN DEL CREDITO APROBADO",
        f"Producto: {product['code']} - {product['name']}",
        (
            f"Monto: {_minor_to_str(ctx['requested_amount_minor'])} | "
            f"Plazo: {ctx['term_months']} meses"
        ),
        (
            f"TEA: {_rate_to_pct(ctx['tea'])} | TCEA: {_rate_to_pct(ctx['tcea'])} | "
            f"Desgravamen: {_rate_to_pct(ctx['insurance_rate'])} mensual"
        ),
        (
            f"Cuota base: {_minor_to_str(ctx['payment_minor'])} | "
            f"Total: {_minor_to_str(ctx['total_paid_minor'])} "
            f"(capital {_minor_to_str(ctx['total_principal_minor'])}, "
            f"interes {_minor_to_str(ctx['total_interest_minor'])}, "
            f"seguro {_minor_to_str(ctx['total_insurance_minor'])})"
        ),
        (
            f"Expediente: {ctx['application_id']}. Documento informativo: no reemplaza "
            "el contrato ni el pagare."
        ),
    ]


def _schedule_headers() -> tuple[str, ...]:
    return ("N°", "Capital", "Interes", "Seguro", "Cuota", "Saldo")


def _schedule_rows(ctx: dict[str, Any]) -> list[list[str]]:
    return [
        [
            str(item["installment_no"]),
            _minor_to_str(item["principal_minor"]),
            _minor_to_str(item["interest_minor"]),
            _minor_to_str(item["insurance_minor"]),
            _minor_to_str(item["total_minor"]),
            _minor_to_str(item["balance_minor"]),
        ]
        for item in ctx["schedule"]
    ]


def _doc_title(doc_type: str, ctx: dict[str, Any]) -> str:
    product = ctx["product"]
    titles = {
        "CONTRATO": f"Contrato de credito {product['code']}",
        "PAGARE": "Pagare a la orden",
        "RESUMEN": "Hoja resumen del credito",
    }
    return titles[doc_type]


def _doc_lines(doc_type: str, ctx: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    """Lineas + filas de tabla por tipo documental (puro, sin I/O)."""
    if doc_type == "CONTRATO":
        return _contract_clauses(ctx), _schedule_rows(ctx)
    if doc_type == "PAGARE":
        return _pagare_lines(ctx), []
    return _resumen_lines(ctx), _schedule_rows(ctx)


def render_doc_pdf(doc_type: str, ctx: dict[str, Any]) -> bytes:
    """Un documento del juego a PDF; 501 si no hay backend PDF."""
    if doc_type not in CONTRACT_DOC_TYPES:
        raise ContractValidationError(f"doc_type debe ser uno de {list(CONTRACT_DOC_TYPES)}")
    backend = pdf_available()
    if backend is None:
        raise ContractPdfUnavailableError(
            "export pdf no disponible: instale reportlab, fpdf2 o weasyprint"
        )
    title = _doc_title(doc_type, ctx)
    lines, rows = _doc_lines(doc_type, ctx)
    headers = _schedule_headers() if rows else ()
    if backend == "reportlab":
        return _to_pdf_reportlab(title, lines, headers, rows)
    if backend == "fpdf":
        return _to_pdf_fpdf(title, lines, headers, rows)
    return _to_pdf_weasyprint(title, lines, headers, rows)


def _to_pdf_reportlab(
    title: str, lines: list[str], headers: tuple[str, ...], rows: list[list[str]]
) -> bytes:
    """PDF real con `reportlab` platypus (cabecera repetida por pagina)."""
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4))
    styles = getSampleStyleSheet()
    story: list[Any] = [Paragraph(_escape(title), styles["Heading2"])]
    for line in lines:
        story.append(Paragraph(_escape(line), styles["Normal"]))
    if rows:
        story.append(Spacer(1, 12))
        table = Table([list(headers)] + rows, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(table)
    doc.build(story)
    return buf.getvalue()


def _to_pdf_fpdf(
    title: str, lines: list[str], headers: tuple[str, ...], rows: list[list[str]]
) -> bytes:
    """PDF real con `fpdf2` (cabecera repetida, salto de pagina manual)."""
    from fpdf import FPDF

    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for line in lines:
        pdf.multi_cell(0, 6, line)
    if rows:
        pdf.ln(2)
        col_w = 40

        def _header() -> None:
            pdf.set_font("Helvetica", "B", 8)
            for header in headers:
                pdf.cell(col_w, 8, header, border=1)
            pdf.ln()
            pdf.set_font("Helvetica", "", 8)

        _header()
        for row in rows:
            if pdf.get_y() > 170:
                pdf.add_page()
                _header()
            for cell in row:
                pdf.cell(col_w, 6, (cell or "")[:32], border=1)
            pdf.ln()
    raw = pdf.output()
    return bytes(raw) if isinstance(raw, (bytes, bytearray)) else raw.encode("latin-1")


def _to_pdf_weasyprint(
    title: str, lines: list[str], headers: tuple[str, ...], rows: list[list[str]]
) -> bytes:
    """PDF real con `weasyprint` (tabla HTML, `thead` repetido por pagina)."""
    from weasyprint import HTML

    summary = "".join(f"<p>{_escape(line)}</p>" for line in lines)
    table_html = ""
    if rows:
        cells = "".join(f"<th>{_escape(header)}</th>" for header in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{_escape(cell or '')}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        table_html = f"<table><thead><tr>{cells}</tr></thead><tbody>{body}</tbody></table>"
    html = (
        "<html><head><meta charset='utf-8'>"
        "<style>@page{size:A4 landscape;@bottom-center{content:counter(page)}}"
        "table{border-collapse:collapse;width:100%;font-size:9px}"
        "th,td{border:1px solid #888;padding:2px}thead{display:table-header-group}"
        "</style></head><body>"
        f"<h2>{_escape(title)}</h2>"
        f"{summary}"
        f"{table_html}"
        "</body></html>"
    )
    return HTML(string=html).write_pdf()


def to_view(row: Contract) -> dict[str, Any]:
    """Vista de un documento del juego (metadatos, sin contenido)."""
    return {
        "contract_id": str(row.id),
        "doc_type": row.doc_type,
        "doc": PUBLIC_DOC_BY_TYPE[row.doc_type],
        "doc_ref": row.doc_ref,
        "doc_hash": row.doc_hash,
        "status": row.status,
    }


def _set_status(rows: list[Contract]) -> str:
    """Estado del juego derivado de las filas (sin hardcodear).

    `SIGNED` solo si las tres estan firmadas; `PARTIAL` si hay mezcla;
    `GENERATED` en otro caso. El estado por-documento (`to_view`) y el
    sello (E3-T13) no cambian.
    """
    statuses = {row.status for row in rows}
    if statuses == {"SIGNED"}:
        return "SIGNED"
    if "SIGNED" in statuses:
        return "PARTIAL"
    return "GENERATED"


def to_payload(application_id: object, rows: list[Contract]) -> dict[str, Any]:
    """Juego completo para respuestas API (`data`, sin PII)."""
    return {
        "application_id": str(application_id),
        "status": _set_status(rows),
        "documents": [to_view(row) for row in rows],
    }


def _record_event(session: Session, owner: uuid.UUID, rows: list[Contract]) -> None:
    """Registra `credit.contract.generated` en el outbox (sin PII)."""
    from app.modules.shared.repository.outbox import insert_event

    insert_event(
        session,
        aggregate_type="credit_contract",
        aggregate_id=rows[0].id,
        event_type=credit_events.CREDIT_CONTRACT_GENERATED,
        payload={
            "contract_ids": [str(row.id) for row in rows],
            "application_id": str(rows[0].application_id),
            "user_id": str(owner),
            "status": "GENERATED",
        },
    )


def _audit_generation(session: Session, owner: uuid.UUID, rows: list[Contract]) -> None:
    """Auditoria best-effort de la generacion (sin PII ni contenido)."""
    try:
        from app.modules.audit.service import record as audit_record

        audit_record(
            session,
            actor=owner,
            action="credit.contract.generated",
            entity="credit_contract",
            entity_id=rows[0].application_id,
            metadata={
                "application_id": str(rows[0].application_id),
                "status": "GENERATED",
                "doc_types": [row.doc_type for row in rows],
            },
        )
    except Exception as exc:  # noqa: BLE001 - la auditoria no bloquea la generacion
        logger.warning("credit contract audit best-effort omitida err=%s", type(exc).__name__)


def generate_contracts(
    session: Session,
    owner_user_id: uuid.UUID | str,
    application_id: object,
) -> tuple[dict[str, Any], bool]:
    """Genera (o reusa) el juego contractual del expediente.

    Expediente `APPROVED` del titular; otro estado -> `CONFLICT`;
    ajeno/inexistente -> `NOT_FOUND` neutro. Si el juego ya existe lo
    devuelve sin duplicar filas ni emitir eventos (`reused=True`).
    Retorna `(payload, reused)`. Sin `commit` (decide el llamante).
    """
    owner = _coerce_owner(owner_user_id)
    row = _load_own_approved(session, owner, application_id)
    existing = contracts_repo.list_by_application(session, row.id)
    if existing:
        logger.info("credit contract replay doc_count=%s", len(existing))
        return to_payload(row.id, existing), True
    ctx = build_context(session, row)
    created: list[Contract] = []
    try:
        with session.begin_nested():
            for doc_type in CONTRACT_DOC_TYPES:
                canonical = canonical_for(doc_type, ctx)
                created.append(
                    contracts_repo.create_contract(
                        session,
                        application_id=row.id,
                        doc_type=doc_type,
                        doc_ref=doc_ref_for(row.id, doc_type),
                        doc_hash=doc_hash_for(canonical),
                    )
                )
            session.flush()
            _record_event(session, owner, created)
            _audit_generation(session, owner, created)
    except (IntegrityError, ContractConflictError):
        # Carrera: otro worker genero el juego primero (UQ por
        # `(application_id, doc_type)`); el savepoint ya reverso lo
        # parcial y la transaccion externa sigue intacta: se reusa.
        logger.info("credit contract carrera UQ: se reusa el juego existente")
        existing = contracts_repo.list_by_application(session, row.id)
        if not existing:  # pragma: no cover - defensa en profundidad
            raise ContractConflictError("el expediente ya tiene documentos en curso")
        return to_payload(row.id, existing), True
    logger.info("credit contract generated doc_count=%s", len(created))
    return to_payload(row.id, created), False


def _minimal_snapshot(*, application_id: object) -> dict[str, Any]:
    """Snapshot de idempotencia SIN PII: solo referencia + estado."""
    return {"application_id": str(application_id), "status": "GENERATED"}


def _replay_view(session: Session, owner: uuid.UUID, stored: Any) -> dict[str, Any]:
    """Reconstruye el juego desde la BD (sin regenerar ni re-emitir)."""
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict) or snapshot.get("application_id") is None:
        raise ContractConflictError("operacion en curso para esta Idempotency-Key")
    app_id = snapshot["application_id"]
    from app.modules.credits.repository import applications as apps_repo

    app_row = apps_repo.get_application(session, app_id)
    if app_row is None or app_row.user_id != owner:
        raise ContractConflictError("expediente asociado a la clave ya no disponible")
    rows = contracts_repo.list_by_application(session, app_row.id)
    if not rows:
        raise ContractConflictError("expediente asociado a la clave ya no disponible")
    return to_payload(app_row.id, rows)


def generate_contracts_idempotent(
    session: Session,
    owner_user_id: uuid.UUID | str,
    application_id: object,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool, int]:
    """Generacion con `Idempotency-Key` obligatoria (crea recurso).

    El juego es idempotente por naturaleza (UQ por expediente): si ya
    existe se devuelve (`replayed=True`, `200`) sin consumir la clave.
    Sin juego previo se exige la clave (`422` si falta; `409` si se
    reusa con otro cuerpo). Retorna `(payload, replayed, http_status)`:
    `201` en la primera generacion, `200` en el replay.
    """
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        store_response,
        try_claim,
    )

    owner = _coerce_owner(owner_user_id)
    from app.modules.credits.repository import applications as apps_repo

    app_row = apps_repo.get_application(session, application_id)  # type: ignore[arg-type]
    if app_row is None or app_row.user_id != owner:
        raise ContractNotFoundError("expediente inexistente")
    if contracts_repo.count_by_application(session, app_row.id):
        payload, _ = generate_contracts(session, owner, app_row.id)
        return payload, True, 200
    key = str(idempotency_key).strip() if isinstance(idempotency_key, str) else ""
    if not key:
        raise ContractValidationError("cabecera Idempotency-Key obligatoria")
    if len(key) > 80:
        raise ContractValidationError("Idempotency-Key supera 80 caracteres")
    digest = compute_request_hash({"owner": str(owner), "application_id": str(app_row.id)})

    row, created = try_claim(
        session,
        key=key,
        user_id=owner,
        endpoint=IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )
    if created:
        payload, reused = generate_contracts(session, owner, app_row.id)
        store_response(
            session,
            row,
            response_snapshot=_minimal_snapshot(application_id=app_row.id),
            transaction_id=payload["documents"][0]["contract_id"],
        )
        # `reused` solo si un worker concurrente gano la carrera UQ.
        return (payload, True, 200) if reused else (payload, False, 201)
    stored = find_key(session, key=key, user_id=owner)
    if stored is None:  # pragma: no cover - carrera extrema
        raise ContractConflictError("clave de idempotencia en conflicto")
    if is_expired(stored):
        renew_expired(
            session,
            stored,
            request_hash=digest,
            endpoint=IDEMPOTENCY_ENDPOINT,
            method="POST",
        )
        payload, reused = generate_contracts(session, owner, app_row.id)
        if not reused:
            store_response(
                session,
                stored,
                response_snapshot=_minimal_snapshot(application_id=app_row.id),
                transaction_id=payload["documents"][0]["contract_id"],
            )
            return payload, False, 201
        return payload, True, 200
    if stored.request_hash != digest:
        raise ContractConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    view = _replay_view(session, owner, stored)
    return view, True, 200


def get_contract_set(
    session: Session, owner_user_id: uuid.UUID | str, application_id: object
) -> dict[str, Any]:
    """Metadatos del juego (`doc_ref`/`doc_hash`/`status`, solo titular)."""
    from app.modules.credits.repository import applications as apps_repo

    owner = _coerce_owner(owner_user_id)
    app_row = apps_repo.get_application(session, application_id)  # type: ignore[arg-type]
    if app_row is None or app_row.user_id != owner:
        raise ContractNotFoundError("expediente inexistente")
    rows = contracts_repo.list_by_application(session, app_row.id)
    if not rows:
        raise ContractNotFoundError("contrato inexistente")
    return to_payload(app_row.id, rows)


def download_contract_pdf(
    session: Session, owner_user_id: uuid.UUID | str, application_id: object, doc: str
) -> tuple[bytes, str]:
    """Un documento del juego en PDF (re-render determinista, titular).

    `doc` publico: `contrato` | `pagare` | `resumen`. Sin backend PDF
    -> `ContractPdfUnavailableError` (501). Retorna `(pdf, filename)`.
    """
    if not isinstance(doc, str) or doc not in TYPE_BY_PUBLIC_DOC:
        raise ContractValidationError("doc debe ser contrato, pagare o resumen")
    doc_type = TYPE_BY_PUBLIC_DOC[doc]
    owner = _coerce_owner(owner_user_id)
    row = _load_own_approved(session, owner, application_id)
    existing = contracts_repo.get_by_application_and_type(session, row.id, doc_type)
    if existing is None:
        raise ContractNotFoundError("contrato inexistente")
    ctx = build_context(session, row)
    pdf = render_doc_pdf(doc_type, ctx)
    short = str(row.id)[:8]
    filename = f"{PUBLIC_DOC_BY_TYPE[doc_type]}-{short}.pdf"
    return pdf, filename


__all__ = [
    "CANONICAL_VERSION",
    "IDEMPOTENCY_ENDPOINT",
    "ContractConflictError",
    "ContractError",
    "ContractNotFoundError",
    "ContractPdfUnavailableError",
    "ContractValidationError",
    "build_context",
    "canonical_for",
    "doc_hash_for",
    "doc_ref_for",
    "download_contract_pdf",
    "generate_contracts",
    "generate_contracts_idempotent",
    "get_contract_set",
    "render_doc_pdf",
    "to_payload",
    "to_view",
]
