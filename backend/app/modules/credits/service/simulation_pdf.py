"""Export PDF del simulador de creditos (E3-T03, HU09 CA-04).

Mismo patron que `accounts.service.pdf_available` /
`transactions` receipt (E2-T03/E2-T10): sondea backends PDF con
`importlib` (sin importarlos) y degrada a
`SimulationPdfUnavailableError` (el router lo traduce a `501
EXPORT_FORMAT_NOT_SUPPORTED`) si no hay libreria, sin romper
`POST /credits/simulate`.

Con `reportlab` (disponible en el `.venv`) genera el PDF real:
titulo + parametros + tabla del cronograma (`03b#84`) con cabecera
repetida por pagina. Montos solo como enteros `*_minor` (regla de oro 3;
el formateo a 2 decimales es solo presentacion). Sin PII: no incluye
titular, documento ni cuenta (la simulacion es anonima y efimera).
Puro (sin BD ni I/O).
"""

from __future__ import annotations

from decimal import Decimal
from html import escape as _escape
from typing import Any

__all__ = [
    "SimulationPdfUnavailableError",
    "pdf_available",
    "simulation_to_pdf",
]

#: Columnas del cronograma en el PDF.
PDF_HEADERS: tuple[str, ...] = (
    "N°",
    "Capital",
    "Interes",
    "Seguro",
    "Cuota",
    "Saldo",
)


class SimulationPdfUnavailableError(RuntimeError):
    """Sin backend PDF instalado (el router responde 501)."""


def pdf_available() -> str | None:
    """Backend PDF disponible (`reportlab` > `fpdf` > `weasyprint`) o `None`.

    Puro: solo sondea `importlib`, sin importar las librerias.
    """
    import importlib.util

    for name in ("reportlab", "fpdf", "weasyprint"):
        if importlib.util.find_spec(name) is not None:
            return name
    return None


def _minor_to_str(value: int) -> str:
    whole, cents = divmod(int(value), 100)
    return f"{whole:,}.{cents:02d}".replace(",", "_").replace(".", ",").replace("_", ".")


def _rate_to_pct(rate: Decimal | str) -> str:
    return f"{(Decimal(str(rate)) * 100):.4f} %".replace(".", ",")


def _table_rows(payload: dict[str, Any]) -> list[list[str]]:
    return [
        [
            str(row["installment_no"]),
            _minor_to_str(row["principal_minor"]),
            _minor_to_str(row["interest_minor"]),
            _minor_to_str(row["insurance_minor"]),
            _minor_to_str(row["total_minor"]),
            _minor_to_str(row["balance_minor"]),
        ]
        for row in payload["schedule"]
    ]


def _summary_lines(payload: dict[str, Any]) -> list[str]:
    product = payload["product"]
    return [
        (
            f"Producto: {product['code']} - {product['name']}"
            if product
            else "Producto: simulacion libre"
        ),
        f"Monto: {_minor_to_str(payload['amount_minor'])} | Plazo: {payload['term_months']} meses",
        f"TEA: {_rate_to_pct(payload['tea'])} | TCEA: {_rate_to_pct(payload['tcea'])}",
        (
            f"Cuota base: {_minor_to_str(payload['payment_minor'])} | "
            f"Total: {_minor_to_str(payload['total_paid_minor'])} "
            f"(capital {_minor_to_str(payload['total_principal_minor'])}, "
            f"interes {_minor_to_str(payload['total_interest_minor'])}, "
            f"seguro {_minor_to_str(payload['total_insurance_minor'])})"
        ),
        "Simulacion referencial: no genera ningun registro crediticio real.",
    ]


def simulation_to_pdf(payload: dict[str, Any]) -> bytes:
    """Cronograma simulado a PDF; 501 si no hay backend (`pdf_available`)."""
    backend = pdf_available()
    if backend is None:
        raise SimulationPdfUnavailableError(
            "export pdf no disponible: instale reportlab, fpdf2 o weasyprint"
        )
    rows = _table_rows(payload)
    lines = _summary_lines(payload)
    if backend == "reportlab":
        return _to_pdf_reportlab(rows, lines)
    if backend == "fpdf":
        return _to_pdf_fpdf(rows, lines)
    return _to_pdf_weasyprint(rows, lines)


def _to_pdf_reportlab(rows: list[list[str]], lines: list[str]) -> bytes:
    """PDF real con `reportlab` platypus (cabecera repetida por pagina)."""
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4))
    styles = getSampleStyleSheet()
    story: list[Any] = [Paragraph(_escape("Simulacion de credito"), styles["Heading2"])]
    for line in lines:
        story.append(Paragraph(_escape(line), styles["Normal"]))
    story.append(Spacer(1, 12))
    table = Table([list(PDF_HEADERS)] + rows, repeatRows=1)
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


def _to_pdf_fpdf(rows: list[list[str]], lines: list[str]) -> bytes:
    """PDF real con `fpdf2` (cabecera repetida, salto de pagina manual)."""
    from fpdf import FPDF

    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    col_w = 40
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, "Simulacion de credito", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for line in lines:
        pdf.multi_cell(0, 6, line)
    pdf.ln(2)

    def _header() -> None:
        pdf.set_font("Helvetica", "B", 8)
        for header in PDF_HEADERS:
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


def _to_pdf_weasyprint(rows: list[list[str]], lines: list[str]) -> bytes:
    """PDF real con `weasyprint` (tabla HTML, `thead` repetido por pagina)."""
    from weasyprint import HTML

    cells = "".join(f"<th>{_escape(header)}</th>" for header in PDF_HEADERS)
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(cell or '')}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    summary = "".join(f"<p>{_escape(line)}</p>" for line in lines)
    html = (
        "<html><head><meta charset='utf-8'>"
        "<style>@page{size:A4 landscape;@bottom-center{content:counter(page)}}"
        "table{border-collapse:collapse;width:100%;font-size:9px}"
        "th,td{border:1px solid #888;padding:2px}thead{display:table-header-group}"
        "</style></head><body>"
        "<h2>Simulacion de credito</h2>"
        f"{summary}"
        f"<table><thead><tr>{cells}</tr></thead><tbody>{body}</tbody></table>"
        "</body></html>"
    )
    return HTML(string=html).write_pdf()
