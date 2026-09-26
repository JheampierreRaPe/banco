"""QA de enmascaramiento y consistencia de saldos (E2-T06, HU05 CA-01/CA-02).

- Enmascaramiento (regla de oro 9, `15#3` PII): el numero de cuenta completo
  nunca aparece en ninguna respuesta (`list`, `detail`, `totals`,
  `movements`, export CSV); solo viaja `account_number_masked`
  (`****` + ultimos 4). RBAC entre usuarios (el 403/404 tampoco filtra).
- Consistencia (`03c#14`, invariante `03c#15.1`): `disponible + retenido =
  contable` en cada respuesta; `GET /totals` cuadra con `GET /accounts`
  (misma fuente: la proyeccion `account_balances`); la proyeccion cuadra
  con el ledger (`postings` manda: `check_projection_consistency == []`).
- Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron de
  `test_accounts_endpoints.py`); `get_db` y `get_current_user_id` se
  inyectan via `app.dependency_overrides` (contexto de auth falso).
- Sin PII en logs (regla de oro 7): los asserts usan solo sufijos
  enmascarados; este modulo no loguea numeros.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.main import app
from app.modules.accounts.api import get_current_user_id

BASE = Path(__file__).resolve().parents[1]
API_PATH = BASE / "app" / "modules" / "accounts" / "api" / "__init__.py"
SERVICE_PATH = BASE / "app" / "modules" / "accounts" / "service" / "__init__.py"
SCHEMAS_PATH = BASE / "app" / "modules" / "accounts" / "schemas" / "__init__.py"

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
FULL_A1 = "00110001234"
FULL_A2 = "00110005678"
FULL_B1 = "00220009999"
ALL_FULLS = (FULL_A1, FULL_A2, FULL_B1)

MASKED_RE = re.compile(r"^\*{4}.{4}$")


def _assert_no_full_numbers(raw: str) -> None:
    """Ningun numero completo de la semilla aparece en el texto dado."""
    for full in ALL_FULLS:
        assert full not in raw


def _assert_money_int(value: object, field: str) -> None:
    """Dinero entero en centimos (regla de oro 3): int, no bool, >= 0."""
    assert isinstance(value, int) and not isinstance(value, bool), field
    assert value >= 0, field


@pytest.fixture()
def qa_client():
    """TestClient con BD SQLite aislada y auth falsa como USER_A.

    a1 nace por la via del ledger (hold de 8_000 via
    `post_entry_and_update_balances` + proyeccion real): disponible 42_000,
    retenido 8_000, contable 50_000. a2/b1 por deltas directos.
    """
    import app.modules.accounts.models as _a  # noqa: F401 (registro de tablas)
    import app.modules.accounts.repository.movements as _m  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    import app.modules.transactions.models as _t  # noqa: F401
    from app.modules.accounts import repository as repo
    from app.modules.ledger import repository as ledger_repo

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
        a2 = repo.create_account(
            session, user_id=USER_A, account_number=FULL_A2, type="CORRIENTE", currency="PEN"
        )
        b1 = repo.create_account(
            session, user_id=USER_B, account_number=FULL_B1, type="AHORRO", currency="PEN"
        )
        # a1 por la via del ledger: fondeo + hold (el motor mueve
        # `available`; la proyeccion refleja `held`; sin doble conteo).
        repo.apply_delta(session, a1.id, available_delta=50_000, held_delta=0, currency="PEN")
        repo.apply_delta(session, a1.id, available_delta=-8_000, held_delta=0, currency="PEN")
        ledger_repo.post_entry_and_update_balances(
            session,
            entry_type="TRANSFER_HOLD",
            postings=[
                {
                    "ledger_account_id": a1.ledger_account_id,
                    "direction": "DEBIT",
                    "amount_minor": 8_000,
                    "currency": "PEN",
                    "account_ref": a1.id,
                },
                {
                    "ledger_account_id": a1.ledger_hold_account_id,
                    "direction": "CREDIT",
                    "amount_minor": 8_000,
                    "currency": "PEN",
                    "account_ref": a1.id,
                },
            ],
            account_projection=repo.ACCOUNTS_LEDGER_PROJECTION,
        )
        repo.apply_delta(session, a2.id, available_delta=120_000, held_delta=0, currency="PEN")
        repo.apply_delta(session, b1.id, available_delta=9_999, held_delta=0, currency="PEN")
        assert (
            repo.handle_movement_event(
                session,
                event_id=uuid.uuid4(),
                entry_id=uuid.uuid5(uuid.NAMESPACE_URL, "e2t06-m1"),
                postings=[
                    {
                        "ledger_account_id": a1.ledger_account_id,
                        "direction": "CREDIT",
                        "amount_minor": 5_000,
                        "currency": "PEN",
                        "account_ref": a1.id,
                    }
                ],
                transaction_id=uuid.uuid5(uuid.NAMESPACE_URL, "e2t06-tx-m1"),
                description="Abono QA",
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
            yield {
                "client": client,
                "a1": a1.id,
                "a2": a2.id,
                "b1": b1.id,
                "session": session,
            }
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        session.close()
        engine.dispose()


# ------------------------------------------------------- Enmascaramiento puro


def test_mask_account_number_format_pure():
    """`mask_account_number`: solo ultimos 4 visibles (`****1234`)."""
    from app.modules.accounts.service import mask_account_number

    assert mask_account_number(FULL_A1) == "****1234"
    assert mask_account_number(FULL_A2) == "****5678"
    assert mask_account_number("12") == "****12"
    assert MASKED_RE.match(mask_account_number("001-0999"))
    with pytest.raises(ValueError):
        mask_account_number("")
    with pytest.raises(ValueError):
        mask_account_number(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        mask_account_number(1234)  # type: ignore[arg-type]


def test_to_summary_masks_and_derives_balance_pure():
    """`to_summary`: enmascara y deriva `contable = disponible + retenido`."""
    from app.modules.accounts.service import to_summary

    summary = to_summary(
        uuid.uuid4(),
        account_number=FULL_A1,
        type="AHORRO",
        currency="PEN",
        available_minor=42_000,
        held_minor=8_000,
    )
    assert summary.account_number_masked == "****1234"
    assert summary.balance_minor == 50_000
    assert FULL_A1 not in summary.model_dump_json()
    with pytest.raises(ValueError):
        to_summary(
            uuid.uuid4(),
            account_number=FULL_A1,
            type="AHORRO",
            currency="PEN",
            available_minor=-1,
            held_minor=0,
        )


def test_static_no_plaintext_account_field_nor_pii_logs():
    """Estatico: sin campo en claro en schemas/API y sin PII en logs."""
    schemas = SCHEMAS_PATH.read_text(encoding="utf-8")
    assert "account_number_masked" in schemas
    assert (
        re.search(r"(?m)^\s*account_number\s*:", schemas) is None
    ), "el numero en claro no existe como campo del esquema"
    assert ": float" not in schemas and "float(" not in schemas, "dinero entero, nunca float"

    api = API_PATH.read_text(encoding="utf-8")
    assert '"account_number"' not in api, "el router nunca emite la clave en claro"

    for path in (SERVICE_PATH, API_PATH):
        content = path.read_text(encoding="utf-8")
        pii_logs = [
            line.strip()
            for line in content.splitlines()
            if ("logger" in line or "log." in line) and "account_number" in line
        ]
        assert pii_logs == [], f"numero en logs ({path.name}): {pii_logs[:2]}"


# --------------------------------------------------- Enmascaramiento en la API


def test_list_masks_all_and_hides_full(qa_client):
    client: TestClient = qa_client["client"]
    resp = client.get("/api/v1/accounts")
    assert resp.status_code == 200
    _assert_no_full_numbers(resp.text)
    body = resp.json()
    assert body["meta"]["total"] == 2
    by_id = {item["id"]: item for item in body["data"]}
    assert by_id[str(qa_client["a1"])]["account_number_masked"] == "****1234"
    assert by_id[str(qa_client["a2"])]["account_number_masked"] == "****5678"
    for item in body["data"]:
        assert "account_number" not in item, "solo viaja el enmascarado"
        assert MASKED_RE.match(item["account_number_masked"]), item["account_number_masked"]


def test_detail_masks_and_hides_full(qa_client):
    client: TestClient = qa_client["client"]
    resp = client.get(f"/api/v1/accounts/{qa_client['a1']}")
    assert resp.status_code == 200
    _assert_no_full_numbers(resp.text)
    item = resp.json()["data"]
    assert item["account_number_masked"] == "****1234"
    assert "account_number" not in item
    assert item["status"] == "ACTIVE"


def test_totals_carry_no_account_numbers(qa_client):
    client: TestClient = qa_client["client"]
    resp = client.get("/api/v1/accounts/totals")
    assert resp.status_code == 200
    _assert_no_full_numbers(resp.text)
    data = resp.json()["data"]
    assert "account" not in resp.text.replace("accounts", ""), "sin numeros ni mascaras"
    for entry in data["totals"]:
        assert set(entry) == {"currency", "available_minor", "held_minor", "total_minor"}


def test_movements_and_csv_export_hide_full_numbers(qa_client):
    from app.modules.accounts.service import EXPORT_CSV_HEADERS

    client: TestClient = qa_client["client"]
    a1 = qa_client["a1"]
    listed = client.get(f"/api/v1/accounts/{a1}/movements")
    assert listed.status_code == 200
    _assert_no_full_numbers(listed.text)
    assert listed.json()["meta"]["total"] == 1
    assert str(a1) in listed.text, "solo el UUID (identificador tecnico), sin numero"

    csv_resp = client.get(
        f"/api/v1/accounts/{a1}/movements/export"
        "?format=csv&date_from=2026-01-01&date_to=2026-12-31"
    )
    assert csv_resp.status_code == 200
    _assert_no_full_numbers(csv_resp.text)
    header = csv_resp.text.lstrip("\ufeff").splitlines()[0].split(",")
    assert header == list(EXPORT_CSV_HEADERS)
    assert "account_number" not in header


def test_rbac_error_bodies_hide_full_numbers(qa_client):
    client: TestClient = qa_client["client"]
    forbidden = client.get(f"/api/v1/accounts/{qa_client['b1']}")
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "NOT_AUTHORIZED"
    _assert_no_full_numbers(forbidden.text)

    missing = client.get(f"/api/v1/accounts/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    _assert_no_full_numbers(missing.text)

    ids = {item["id"] for item in client.get("/api/v1/accounts").json()["data"]}
    assert str(qa_client["b1"]) not in ids, "un usuario no lista cuentas ajenas"


# ------------------------------------------------- Consistencia de los saldos


def test_invariant_available_plus_held_equals_balance(qa_client):
    """`03c#15.1`: `balance_minor == available_minor + held_minor`."""
    client: TestClient = qa_client["client"]
    for item in client.get("/api/v1/accounts").json()["data"]:
        for field in ("available_minor", "held_minor", "balance_minor"):
            _assert_money_int(item[field], field)
        assert item["balance_minor"] == item["available_minor"] + item["held_minor"]

    detail = client.get(f"/api/v1/accounts/{qa_client['a1']}").json()["data"]
    assert (detail["available_minor"], detail["held_minor"], detail["balance_minor"]) == (
        42_000,
        8_000,
        50_000,
    )
    assert detail["balance_minor"] == detail["available_minor"] + detail["held_minor"]


def test_totals_match_list_same_source(qa_client):
    """`totals` cuadra con `list` (misma fuente: `account_balances`)."""
    client: TestClient = qa_client["client"]
    listed = client.get("/api/v1/accounts").json()["data"]
    totals = client.get("/api/v1/accounts/totals").json()["data"]
    by_ccy = {t["currency"]: t for t in totals["totals"]}
    assert by_ccy["PEN"]["total_minor"] == 170_000
    assert by_ccy["PEN"]["total_minor"] == sum(i["balance_minor"] for i in listed)
    assert by_ccy["PEN"]["available_minor"] == sum(i["available_minor"] for i in listed)
    assert by_ccy["PEN"]["held_minor"] == sum(i["held_minor"] for i in listed)
    assert totals["primary_total_minor"] == by_ccy["PEN"]["total_minor"]
    for entry in totals["totals"]:
        assert entry["total_minor"] == entry["available_minor"] + entry["held_minor"]


def test_totals_money_integers_no_float(qa_client):
    client: TestClient = qa_client["client"]
    data = client.get("/api/v1/accounts/totals").json()["data"]
    _assert_money_int(data["primary_total_minor"], "primary_total_minor")
    for entry in data["totals"]:
        for field in ("available_minor", "held_minor", "total_minor"):
            _assert_money_int(entry[field], field)


def test_ledger_hold_projects_and_matches_api(qa_client):
    """La proyeccion `held` refleja el ledger y la API la expone tal cual."""
    from app.modules.accounts import repository as repo
    from app.modules.ledger import repository as ledger_repo

    session: Session = qa_client["session"]
    client: TestClient = qa_client["client"]
    account = repo.create_account(
        session, user_id=USER_A, account_number="00330001111", type="AHORRO", currency="PEN"
    )
    repo.apply_delta(session, account.id, available_delta=20_000, held_delta=0, currency="PEN")
    repo.apply_delta(session, account.id, available_delta=-8_000, held_delta=0, currency="PEN")
    ledger_repo.post_entry_and_update_balances(
        session,
        entry_type="TRANSFER_HOLD",
        postings=[
            {
                "ledger_account_id": account.ledger_account_id,
                "direction": "DEBIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": account.id,
            },
            {
                "ledger_account_id": account.ledger_hold_account_id,
                "direction": "CREDIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": account.id,
            },
        ],
        account_projection=repo.ACCOUNTS_LEDGER_PROJECTION,
    )
    session.commit()

    assert ledger_repo.check_projection_consistency(session) == [], "postings mandan (03c#14)"
    row = repo.get_balance(session, account.id)
    assert (row.available_minor, row.held_minor) == (12_000, 8_000)
    assert row.available_minor + row.held_minor == 20_000

    detail = client.get(f"/api/v1/accounts/{account.id}").json()["data"]
    assert detail["account_number_masked"] == "****1111"
    assert (detail["available_minor"], detail["held_minor"], detail["balance_minor"]) == (
        12_000,
        8_000,
        20_000,
    )
    assert "00330001111" not in client.get("/api/v1/accounts").text


def test_settle_and_reverse_keep_ledger_consistency(qa_client):
    """Settle + reverso: `held` se mueve con el ledger y todo sigue cuadrado."""
    from app.modules.accounts import repository as repo
    from app.modules.ledger import repository as ledger_repo

    session: Session = qa_client["session"]
    src = repo.create_account(
        session, user_id=USER_A, account_number="00330002222", type="AHORRO", currency="PEN"
    )
    dst = repo.create_account(
        session, user_id=USER_A, account_number="00330003333", type="AHORRO", currency="PEN"
    )
    repo.apply_delta(session, src.id, available_delta=20_000, held_delta=0, currency="PEN")
    repo.apply_delta(session, src.id, available_delta=-8_000, held_delta=0, currency="PEN")
    ledger_repo.post_entry_and_update_balances(
        session,
        entry_type="TRANSFER_HOLD",
        postings=[
            {
                "ledger_account_id": src.ledger_account_id,
                "direction": "DEBIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": src.id,
            },
            {
                "ledger_account_id": src.ledger_hold_account_id,
                "direction": "CREDIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": src.id,
            },
        ],
        account_projection=repo.ACCOUNTS_LEDGER_PROJECTION,
    )
    repo.apply_delta(session, dst.id, available_delta=8_000, held_delta=0, currency="PEN")
    settle = ledger_repo.post_entry_and_update_balances(
        session,
        entry_type="TRANSFER_SETTLE",
        postings=[
            {
                "ledger_account_id": src.ledger_hold_account_id,
                "direction": "DEBIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": src.id,
            },
            {
                "ledger_account_id": dst.ledger_account_id,
                "direction": "CREDIT",
                "amount_minor": 8_000,
                "currency": "PEN",
                "account_ref": dst.id,
            },
        ],
        account_projection=repo.ACCOUNTS_LEDGER_PROJECTION,
    )
    session.commit()

    assert (repo.get_balance(session, src.id).held_minor) == 0
    assert ledger_repo.check_projection_consistency(session) == []

    ledger_repo.reverse_entry_and_update_balances(
        session, settle.id, account_projection=repo.ACCOUNTS_LEDGER_PROJECTION
    )
    session.commit()

    assert repo.get_balance(session, src.id).held_minor == 8_000, "el reverso restaura held"
    assert ledger_repo.check_projection_consistency(session) == [], "postings siguen mandando"
    for account_id in (src.id, dst.id):
        row = repo.get_balance(session, account_id)
        assert row.available_minor + row.held_minor >= 0
