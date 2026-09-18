"""Endpoints de movimientos y export (E2-T03, HU05 CA-03/CA-04).

- Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron de
  `tests/test_accounts_endpoints.py`); `get_db` y `get_current_user_id`
  via `app.dependency_overrides` (TestClient + contexto de auth falso).
- Casos: paginacion (page 1/2 sin solaparse, total correcto), filtro por
  fecha valor y por direccion, export CSV valido (parseable, cabeceras
  `03b#5.4`, solo filas del rango), 403 cuenta ajena en ambos endpoints,
  404 inexistente, 401 sin auth, rango obligatorio/invertido y xlsx -> 501
  sin `openpyxl` (decision documentada en `accounts/service`).
"""

from __future__ import annotations

import csv
import io
import time
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.main import app
from app.modules.accounts.api import get_current_user_id
from app.modules.accounts.service import EXPORT_CSV_HEADERS

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
FULL_A1 = "00310001111"
FULL_B1 = "00310002222"

SEED_MOVEMENTS = (
    # (sufijo cuenta, direction, amount, value_date, description)
    ("m1", "CREDIT", 10_000, date(2026, 1, 10), "Abono enero"),
    ("m2", "DEBIT", 2_500, date(2026, 2, 15), "Pago febrero"),
    ("m3", "CREDIT", 7_000, date(2026, 2, 20), "Abono febrero"),
    ("m4", "DEBIT", 1_000, date(2026, 3, 5), "Pago marzo"),
    ("m5", "CREDIT", 3_000, date(2026, 3, 25), "Abono marzo"),
)


@pytest.fixture()
def movements_client():
    """TestClient con 5 movimientos en la cuenta de USER_A + 1 en la de B."""
    import app.modules.accounts.models as _a  # noqa: F401 (registro de tablas)
    import app.modules.accounts.repository.movements as _m  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    import app.modules.transactions.models as _t  # noqa: F401
    from app.modules.accounts import repository as repo

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "ledger", "shared", "transactions"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["accounts.daily_balance_snapshots"],
            Base.metadata.tables["accounts.movements_view"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["ledger.journal_entries"],
            Base.metadata.tables["ledger.postings"],
            Base.metadata.tables["ledger.ledger_balances"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.processed_events"],
            Base.metadata.tables["transactions.transactions"],
            Base.metadata.tables["transactions.transaction_status_history"],
            Base.metadata.tables["transactions.holds"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = repo.create_account(
            session, user_id=USER_A, account_number=FULL_A1, type="AHORRO", currency="PEN"
        )
        b1 = repo.create_account(
            session, user_id=USER_B, account_number=FULL_B1, type="AHORRO", currency="PEN"
        )
        for suffix, direction, amount, value_date, description in SEED_MOVEMENTS:
            assert (
                repo.handle_movement_event(
                    session,
                    event_id=uuid.uuid4(),
                    entry_id=uuid.uuid5(uuid.NAMESPACE_URL, f"e2t03-{suffix}"),
                    postings=[
                        {
                            "ledger_account_id": a1.ledger_account_id,
                            "direction": direction,
                            "amount_minor": amount,
                            "currency": "PEN",
                            "account_ref": a1.id,
                        }
                    ],
                    transaction_id=uuid.uuid5(uuid.NAMESPACE_URL, f"e2t03-tx-{suffix}"),
                    description=description,
                    value_date=value_date,
                )
                is True
            )
        # 1 movimiento en la cuenta ajena (para probar que no se filtra).
        assert (
            repo.handle_movement_event(
                session,
                event_id=uuid.uuid4(),
                entry_id=uuid.uuid4(),
                postings=[
                    {
                        "ledger_account_id": b1.ledger_account_id,
                        "direction": "CREDIT",
                        "amount_minor": 99_999,
                        "currency": "PEN",
                        "account_ref": b1.id,
                    }
                ],
                description="Abono ajeno",
                value_date=date(2026, 2, 15),
            )
            is True
        )
        session.commit()

        def _override_db():
            yield session

        def _override_user():
            return USER_A

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        with TestClient(app) as client:
            yield {"client": client, "a1": a1.id, "b1": b1.id, "session": session}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        session.close()
        engine.dispose()


def test_list_envelope_and_total(movements_client):
    resp = movements_client["client"].get(f"/api/v1/accounts/{movements_client['a1']}/movements")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"data", "meta"}
    assert body["meta"]["page"] == 1 and body["meta"]["page_size"] == 20
    assert body["meta"]["total"] == 5
    assert len(body["data"]) == 5
    assert FULL_A1 not in resp.text  # sin numeros en claro (regla de oro 9)


def test_pagination_pages_do_not_overlap(movements_client):
    client: TestClient = movements_client["client"]
    a1 = movements_client["a1"]
    p1 = client.get(f"/api/v1/accounts/{a1}/movements?page=1&page_size=2").json()
    p2 = client.get(f"/api/v1/accounts/{a1}/movements?page=2&page_size=2").json()
    p3 = client.get(f"/api/v1/accounts/{a1}/movements?page=3&page_size=2").json()
    assert (p1["meta"]["total"], p2["meta"]["total"]) == (5, 5)
    ids1 = {m["journal_entry_id"] + m["direction"] for m in p1["data"]}
    ids2 = {m["journal_entry_id"] + m["direction"] for m in p2["data"]}
    ids3 = {m["journal_entry_id"] + m["direction"] for m in p3["data"]}
    assert len(p1["data"]) == 2 and len(p2["data"]) == 2 and len(p3["data"]) == 1
    assert not ids1 & ids2 and not ids1 & ids3 and not ids2 & ids3
    assert len(ids1 | ids2 | ids3) == 5


def test_filter_by_date_range(movements_client):
    client: TestClient = movements_client["client"]
    a1 = movements_client["a1"]
    body = client.get(
        f"/api/v1/accounts/{a1}/movements?date_from=2026-02-01&date_to=2026-02-28"
    ).json()
    assert body["meta"]["total"] == 2
    assert {m["description"] for m in body["data"]} == {"Pago febrero", "Abono febrero"}
    for m in body["data"]:
        assert "2026-02-01" <= m["value_date"] <= "2026-02-28"


def test_filter_by_direction(movements_client):
    client: TestClient = movements_client["client"]
    a1 = movements_client["a1"]
    body = client.get(f"/api/v1/accounts/{a1}/movements?direction=DEBIT").json()
    assert body["meta"]["total"] == 2
    assert {m["direction"] for m in body["data"]} == {"DEBIT"}
    body = client.get(
        f"/api/v1/accounts/{a1}/movements"
        "?date_from=2026-03-01&date_to=2026-03-31&direction=CREDIT"
    ).json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["description"] == "Abono marzo"


def test_list_under_two_seconds(movements_client):
    client: TestClient = movements_client["client"]
    started = time.perf_counter()
    resp = client.get(f"/api/v1/accounts/{movements_client['a1']}/movements")
    assert resp.status_code == 200
    assert time.perf_counter() - started < 2.0


def _parse_csv(resp) -> list[dict]:
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="movimientos_' in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_export_csv_valid_with_headers_and_range(movements_client):
    client: TestClient = movements_client["client"]
    a1 = movements_client["a1"]
    rows = _parse_csv(
        client.get(
            f"/api/v1/accounts/{a1}/movements/export"
            "?format=csv&date_from=2026-02-01&date_to=2026-03-31"
        )
    )
    assert len(rows) == 4  # m2..m5 (enero queda fuera del rango)
    assert list(rows[0].keys()) == list(EXPORT_CSV_HEADERS)
    assert {r["description"] for r in rows} == {
        "Pago febrero",
        "Abono febrero",
        "Pago marzo",
        "Abono marzo",
    }
    for r in rows:
        assert "2026-02-01" <= r["value_date"] <= "2026-03-31"
        assert int(r["amount_minor"]) > 0 and r["direction"] in ("DEBIT", "CREDIT")
    assert FULL_A1 not in io.StringIO(resp_text(client, a1)).getvalue()


def resp_text(client: TestClient, a1) -> str:
    return client.get(
        f"/api/v1/accounts/{a1}/movements/export"
        "?format=csv&date_from=2026-01-01&date_to=2026-12-31"
    ).text


def test_export_csv_empty_range_has_only_header(movements_client):
    client: TestClient = movements_client["client"]
    rows = _parse_csv(
        client.get(
            f"/api/v1/accounts/{movements_client['a1']}/movements/export"
            "?format=csv&date_from=2025-01-01&date_to=2025-12-31"
        )
    )
    assert rows == []


def test_export_requires_date_range(movements_client):
    client: TestClient = movements_client["client"]
    a1 = movements_client["a1"]
    assert client.get(f"/api/v1/accounts/{a1}/movements/export?format=csv").status_code == 422
    resp = client.get(
        f"/api/v1/accounts/{a1}/movements/export"
        "?format=csv&date_from=2026-03-01&date_to=2026-01-01"
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_export_xlsx_unsupported_without_openpyxl(movements_client):
    pytest.importorskip("pytest")  # marcador: siempre corre; verifica el 501 real
    import importlib.util

    if importlib.util.find_spec("openpyxl") is not None:
        pytest.skip("openpyxl instalado: el xlsx real se ejerce en ese entorno")
    client: TestClient = movements_client["client"]
    resp = client.get(
        f"/api/v1/accounts/{movements_client['a1']}/movements/export"
        "?format=xlsx&date_from=2026-01-01&date_to=2026-12-31"
    )
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "EXPORT_FORMAT_NOT_SUPPORTED"


def test_foreign_account_forbidden_on_both_endpoints(movements_client):
    client: TestClient = movements_client["client"]
    b1 = movements_client["b1"]
    resp = client.get(f"/api/v1/accounts/{b1}/movements")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"
    resp = client.get(
        f"/api/v1/accounts/{b1}/movements/export"
        "?format=csv&date_from=2026-01-01&date_to=2026-12-31"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "NOT_AUTHORIZED"


def test_missing_account_not_found(movements_client):
    client: TestClient = movements_client["client"]
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/accounts/{missing}/movements").status_code == 404
    assert (
        client.get(
            f"/api/v1/accounts/{missing}/movements/export"
            "?format=csv&date_from=2026-01-01&date_to=2026-12-31"
        ).status_code
        == 404
    )


def test_missing_auth_unauthorized(movements_client):
    client: TestClient = movements_client["client"]
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        a1 = movements_client["a1"]
        assert client.get(f"/api/v1/accounts/{a1}/movements").status_code == 401
        assert (
            client.get(
                f"/api/v1/accounts/{a1}/movements/export"
                "?format=csv&date_from=2026-01-01&date_to=2026-12-31"
            ).status_code
            == 401
        )
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: USER_A


# ------------------------------------------------- Export PDF (fase 4/H1)


def test_export_pdf_requires_date_range(movements_client):
    """`format=pdf` exige rango igual que csv (422 si falta)."""
    client: TestClient = movements_client["client"]
    a1 = movements_client["a1"]
    assert client.get(f"/api/v1/accounts/{a1}/movements/export?format=pdf").status_code == 422
    resp = client.get(
        f"/api/v1/accounts/{a1}/movements/export"
        "?format=pdf&date_from=2026-03-01&date_to=2026-01-01"
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_export_pdf_unsupported_without_pdf_lib(movements_client):
    """Sin backend PDF -> 501 `EXPORT_FORMAT_NOT_SUPPORTED` (patron xlsx)."""
    import importlib.util

    if any(
        importlib.util.find_spec(name) is not None for name in ("reportlab", "fpdf", "weasyprint")
    ):
        pytest.skip("backend PDF instalado: el pdf real se ejerce en ese entorno")
    client: TestClient = movements_client["client"]
    resp = client.get(
        f"/api/v1/accounts/{movements_client['a1']}/movements/export"
        "?format=pdf&date_from=2026-01-01&date_to=2026-12-31"
    )
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "EXPORT_FORMAT_NOT_SUPPORTED"


def test_export_pdf_real_when_backend_available(movements_client):
    """Con backend PDF -> `application/pdf` valido (cabecera `%PDF`)."""
    import importlib.util

    if all(importlib.util.find_spec(name) is None for name in ("reportlab", "fpdf", "weasyprint")):
        pytest.skip("sin backend PDF: rige el 501 documentado en el service")
    client: TestClient = movements_client["client"]
    resp = client.get(
        f"/api/v1/accounts/{movements_client['a1']}/movements/export"
        "?format=pdf&date_from=2026-01-01&date_to=2026-12-31"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert 'attachment; filename="movimientos_' in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF")
