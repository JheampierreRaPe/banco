"""QA de observabilidad Q-T07 (transversal; sin logica de negocio).

Cubre CA-01..CA-04 (feliz + error):

- CA-01: `request_id` correlacionado (`X-Request-Id` <-> log JSON <->
  `meta.request_id` <-> header de respuesta). Regresion del gap real:
  `request_id_var` existia en `app.core.logging` pero nadie lo fijaba y el
  log salia siempre con `request_id: "-"`; el middleware de `app.main` lo
  fija por request (estas pruebas fallan sin el fix).
- CA-02: `422` saneado sin PII/secretos (ni `input`/`ctx` ni valores crudos
  en log JSON ni en respuesta; `detail` solo `loc`/`msg`/`type`).
- CA-03: healthchecks `/health` (`{"status": "ok"}`) y `/health/ready`
  (`ok` con BD, `degraded` sin BD, sin lanzar).
- CA-04: senal de alerta de outbox (conteo por estado; `FAILED` terminal) y
  de descuadre del ledger (validador de cuadre + verificador de cadena).

Senales para alerta (consumibles por monitoreo / panel de Q-T09):

- Outbox: `SELECT status, count(*) FROM shared.outbox GROUP BY status`
  (`PENDING` acumulado = atasco del worker; `FAILED > 0` = alerta).
- Ledger: `validate_balanced` rechaza el asiento descuadrado antes de
  crearlo; `verify_ledger_chain_detailed(session)` retorna
  `{"valid", "reason", "failed_entry_id", "checked"}`
  (`valid is False` = alerta critica, `reason` como codigo).
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import date

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.core.logging import JsonFormatter
from app.main import app
from app.modules.transactions.api import get_current_user_id

EMAIL_CHECK_PATH = "/api/v1/auth/kyc/email/check"
THIRD_PARTY_PATH = "/api/v1/transfers/third-party"


@pytest.fixture()
def auth_client() -> TestClient:
    """Cliente con auth falsa (la validacion del body llega a 422 de esquema)."""

    def _override_user() -> uuid.UUID:
        return uuid.uuid4()

    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)


@pytest.fixture()
def json_log_stream() -> io.StringIO:
    """Captura el log JSON real (`JsonFormatter`) del logger del 422 (`app.main`).

    El `ContextVar` del `request_id` solo es visible en el hilo que atiende el
    request: por eso se captura con el formatter productivo en vez de `caplog`
    (cuyo formateo ocurriria en el hilo del test con `request_id: "-"`).
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app.main")
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)


def _validation_json_logs(stream: io.StringIO) -> list[dict]:
    records = []
    for line in stream.getvalue().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return [p for p in records if "validation_error" in str(p.get("message", ""))]


# ------------------------------------------------------------------ CA-01: request_id
def test_request_id_header_correlates_response_and_meta(db_client: TestClient) -> None:
    """Con `X-Request-Id` fijo: respuesta, `meta` y header conservan el mismo id."""
    request_id = "qt07-fixed-request-id-001"
    response = db_client.post(
        EMAIL_CHECK_PATH,
        json={"email": "qt07-libre@example.com"},
        headers={"X-Request-Id": request_id, "X-Device-Id": "qt07-ca01-a"},
    )
    assert response.status_code == 200
    assert response.headers.get("x-request-id") == request_id
    assert response.json()["meta"]["request_id"] == request_id


def test_request_id_generated_when_header_missing(db_client: TestClient) -> None:
    """Sin cabecera: el middleware genera un id no vacio y lo devuelve en la respuesta.

    Nota de frontera (Q-T07): cada router genera su propio `meta.request_id`
    cuando no recibe `X-Request-Id` (sus `_request_id` son de cada modulo y no
    se tocan); la convergencia total log <-> `meta` rige con cabecera explicita
    (flujo 05#3, test anterior). Aqui se verifica que el id generado existe y
    viaja en el header de respuesta.
    """
    response = db_client.post(
        EMAIL_CHECK_PATH,
        json={"email": "qt07-otro-libre@example.com"},
        headers={"X-Device-Id": "qt07-ca01-b"},
    )
    assert response.status_code == 200
    generated = response.headers.get("x-request-id")
    assert generated and generated != "-"
    assert response.json()["meta"]["request_id"], "el envelope trae su propio id"


def test_json_log_carries_request_id_regression(
    auth_client: TestClient, json_log_stream: io.StringIO
) -> None:
    """Regresion del gap Q-T07: el log JSON del request lleva su `request_id`.

    Falla sin el fix (log con `request_id: "-"` y sin header en la respuesta);
    pasa con el fix (log, header y respuesta con el mismo id).
    """
    request_id = "qt07-log-correlation-001"
    response = auth_client.post(THIRD_PARTY_PATH, json={}, headers={"X-Request-Id": request_id})
    assert response.status_code == 422
    assert response.headers.get("x-request-id") == request_id
    logs = _validation_json_logs(json_log_stream)
    assert logs, "se esperaba el log JSON del 422"
    assert all(entry.get("request_id") == request_id for entry in logs)


def test_generated_id_matches_log_and_response_header(
    auth_client: TestClient, json_log_stream: io.StringIO
) -> None:
    """Sin cabecera: el id generado coincide en el log JSON y en la respuesta."""
    response = auth_client.post(THIRD_PARTY_PATH, json={})
    assert response.status_code == 422
    generated = response.headers.get("x-request-id")
    assert generated and generated != "-"
    logs = _validation_json_logs(json_log_stream)
    assert logs, "se esperaba el log JSON del 422"
    assert all(entry.get("request_id") == generated for entry in logs)


# ------------------------------------------------------------------ CA-02: 422 sin PII
def test_422_sanitized_without_pii(auth_client: TestClient, json_log_stream: io.StringIO) -> None:
    """Un `422` nunca expone valores crudos (ni `input`/`ctx`) en log ni respuesta."""
    sentinel_doc = "Q-T07-SENTINEL-DOC-998877"
    sentinel_target = "Q-T07-SENTINEL-TGT-112233"
    body = {
        "source_account_id": sentinel_doc,
        "target_account_id": sentinel_target,
        "amount_minor": -5,
        "currency": "pen",
    }
    response = auth_client.post(THIRD_PARTY_PATH, json=body)
    assert response.status_code == 422
    assert sentinel_doc not in response.text
    assert sentinel_target not in response.text
    payload = response.json()
    assert isinstance(payload.get("detail"), list) and payload["detail"]
    for item in payload["detail"]:
        assert "input" not in item
        assert "ctx" not in item
        assert set(item.keys()) <= {"loc", "msg", "type"}
    logged = json_log_stream.getvalue()
    assert sentinel_doc not in logged
    assert sentinel_target not in logged


# ------------------------------------------------------------------ CA-03: healthchecks
def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_ok_with_db(db_client: TestClient) -> None:
    response = db_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": True}


def test_ready_degraded_without_db() -> None:
    """Sin BD responde `degraded` con `database: false` (no lanza)."""

    class _BrokenDB:
        def execute(self, *args, **kwargs):
            raise RuntimeError("bd caida (simulada Q-T07)")

    def _override_broken():
        yield _BrokenDB()

    app.dependency_overrides[get_db] = _override_broken
    try:
        response = TestClient(app).get("/health/ready")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "database": False}


# ------------------------------------------------------------------ CA-04: outbox
@pytest.fixture()
def sqlite_shared_session() -> Session:
    """Sesion SQLite aislada con schema `shared` (ATTACH; patron outbox)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.shared.models as m  # noqa: F401 (registro)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS shared")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["shared.processed_events"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def outbox_status_counts(session: Session) -> dict[str, int]:
    """Senal de alerta del outbox: conteo por estado (`PENDING`/`PUBLISHED`/`FAILED`).

    `PENDING` acumulado = atasco del worker; `FAILED > 0` = alerta de entrega.
    """
    table = Base.metadata.tables["shared.outbox"]
    rows = session.execute(
        sa.select(table.c.status, sa.func.count()).group_by(table.c.status)
    ).all()
    return {status: int(count) for status, count in rows}


def test_outbox_pending_failed_signal(sqlite_shared_session: Session) -> None:
    """Forzar `FAILED` tras N intentos deja la senal/conteo verificable."""
    from app.core.outbox import fetch_pending, mark_failed, record

    for seq in range(2):
        record(
            sqlite_shared_session,
            aggregate_type="transfer",
            aggregate_id=uuid.uuid4(),
            event_type="transfer.settled",
            payload={"n": seq},
        )
    assert outbox_status_counts(sqlite_shared_session) == {"PENDING": 2}
    pending = fetch_pending(sqlite_shared_session)
    assert len(pending) == 2
    mark_failed(sqlite_shared_session, pending[0].id, "boom (simulado Q-T07)", max_attempts=1)
    counts = outbox_status_counts(sqlite_shared_session)
    assert counts == {"PENDING": 1, "FAILED": 1}
    assert counts.get("FAILED", 0) > 0, "FAILED > 0 es la senal de alerta"
    remaining = fetch_pending(sqlite_shared_session)
    assert len(remaining) == 1 and remaining[0].status == "PENDING"


# ------------------------------------------------------------------ CA-04: cuadre del ledger
def test_ledger_unbalanced_entry_rejected() -> None:
    """Descuadre detectado por el validador: el asiento no se crea (`ValueError`)."""
    from app.modules.ledger.domain.entries import PostingInput, validate_balanced

    debit_account, credit_account = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ValueError, match="descuadrado"):
        validate_balanced(
            [
                PostingInput(
                    ledger_account_id=debit_account,
                    direction="DEBIT",
                    amount_minor=1000,
                    currency="PEN",
                ),
                PostingInput(
                    ledger_account_id=credit_account,
                    direction="CREDIT",
                    amount_minor=999,
                    currency="PEN",
                ),
            ]
        )


def _balanced_pair(amount_minor: int = 1000, currency: str = "PEN"):
    from app.modules.ledger.domain.entries import PostingInput

    return (
        PostingInput(
            ledger_account_id=uuid.uuid4(),
            direction="DEBIT",
            amount_minor=amount_minor,
            currency=currency,
        ),
        PostingInput(
            ledger_account_id=uuid.uuid4(),
            direction="CREDIT",
            amount_minor=amount_minor,
            currency=currency,
        ),
    )


def test_ledger_chain_tamper_is_detectable_alert_signal() -> None:
    """Cadena integra -> `valid`; manipulacion -> senal `hash_mismatch` con el id fallido."""
    from app.modules.ledger.domain.entries import PostingInput, compute_entry_hash
    from app.modules.ledger.models import JournalEntry
    from app.modules.ledger.service import verify_chain_detailed

    day = date(2026, 10, 1)
    debit_1, credit_1 = _balanced_pair(1000)
    debit_2, credit_2 = _balanced_pair(2500)
    tx_1, tx_2 = uuid.uuid4(), uuid.uuid4()
    hash_1 = compute_entry_hash(
        entry_type="OWN_TRANSFER",
        description="asiento 1",
        value_date=day,
        transaction_id=tx_1,
        postings=[debit_1, credit_1],
        prev_hash=None,
    )
    entry_1 = JournalEntry(
        id=uuid.uuid4(),
        transaction_id=tx_1,
        entry_type="OWN_TRANSFER",
        description="asiento 1",
        value_date=day,
        status="POSTED",
        prev_hash=None,
        hash=hash_1,
    )
    hash_2 = compute_entry_hash(
        entry_type="OWN_TRANSFER",
        description="asiento 2",
        value_date=day,
        transaction_id=tx_2,
        postings=[debit_2, credit_2],
        prev_hash=hash_1,
    )
    entry_2 = JournalEntry(
        id=uuid.uuid4(),
        transaction_id=tx_2,
        entry_type="OWN_TRANSFER",
        description="asiento 2",
        value_date=day,
        status="POSTED",
        prev_hash=hash_1,
        hash=hash_2,
    )
    ok = verify_chain_detailed(
        [entry_1, entry_2],
        {entry_1.id: [debit_1, credit_1], entry_2.id: [debit_2, credit_2]},
    )
    assert ok == {"valid": True, "reason": None, "failed_entry_id": None, "checked": 2}
    # Manipulacion: el posting almacenado ya no sostiene el hash del asiento.
    tampered_debit_2 = PostingInput(
        ledger_account_id=debit_2.ledger_account_id,
        direction="DEBIT",
        amount_minor=2501,
        currency="PEN",
    )
    alert = verify_chain_detailed(
        [entry_1, entry_2],
        {entry_1.id: [debit_1, credit_1], entry_2.id: [tampered_debit_2, credit_2]},
    )
    assert alert["valid"] is False
    assert alert["reason"] == "hash_mismatch"
    assert alert["failed_entry_id"] == str(entry_2.id)
