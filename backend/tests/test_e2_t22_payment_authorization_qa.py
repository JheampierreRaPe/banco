"""QA de autorizacion biometrica de pagos sensibles (E2-T22, HU08).

Verifica de punta a punta (HTTP + fachadas reales, sin red, sin PII, dinero
de juguete en centimos) que el gate biometrico respeta el umbral configurable,
los limites diarios por perfil y el bloqueo preventivo, sin mover dinero en
los rechazos.

Cobertura NUEVA respecto a `test_e2_t17*`, `test_e2_t18*`, `test_e2_t19*` y
`test_e2_t20*` (no se duplica: esos suites cubren la mecanica del servicio,
la fachada de parametros en aislamiento, el endpoint `authorize` caso por
caso y el consumidor `risk` con payloads sinteticos). Aqui solo integracion
QA no cubierta antes:

- bajo umbral liquida sin reto ni rastro de seguridad (HTTP) .. `test_under_*`
- sobre umbral queda pendiente sin liquidar (HTTP + detalle) .. `test_over_*`
- cambio de umbral VIA FACHADA cambia el gate sin reinicio ... `test_hot_*`
- limite diario ACUMULADO por perfil != STANDARD via fachada . `test_daily_*`
- bloqueo preventivo integrado: hold + saldos + evento + `risk`
  + notificacion en un solo flujo .......................... `test_block_*`
- `nonce` expirado conserva el hold y alimenta a `risk` ..... `test_expired_*`
- parametro cambiado a mitad de operacion: sin inconsistencia `test_mid_*`
- doble alta con misma clave: una sola operacion ............. `test_dup_*`

Evidencia ya existente (no repetida aqui): `EXPIRED_NONCE` -> challenge ->
reintento con clave nueva que autoriza (`test_e2_t19`::
`test_expired_nonce_retry_with_fresh_challenge_and_new_key_settles`), tope
pre-aprobado (`test_e2_t14*`), fachada de parametros en aislamiento
(`test_e2_t18*`).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
DEVICE_A = "dev-e2t22-1"

THRESHOLD_KEY = "transfer.biometric_threshold_minor"
DAILY_KEY = "transfer.daily_limit_minor"
DAILY_STANDARD_KEY = "transfer.daily_limit_STANDARD_minor"
DAILY_PREMIUM_KEY = "transfer.daily_limit_PREMIUM_minor"
MAX_ATTEMPTS_KEY = "auth.max_failed_attempts"

TABLES = [
    "accounts.accounts",
    "accounts.account_balances",
    "accounts.daily_balance_snapshots",
    "ledger.ledger_accounts",
    "ledger.journal_entries",
    "ledger.postings",
    "ledger.ledger_balances",
    "shared.outbox",
    "shared.processed_events",
    "shared.idempotency_keys",
    "transactions.transactions",
    "transactions.transaction_status_history",
    "transactions.holds",
    "identity.users",
    "risk.risk_profiles",
    "notifications.notifications",
    "notifications.notification_templates",
    "audit.audit_log",
]

SCHEMAS = (
    "accounts",
    "ledger",
    "shared",
    "transactions",
    "config",
    "identity",
    "risk",
    "notifications",
    "audit",
)


def _make_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401 (registro de tablas)
    import app.modules.audit.models as _au  # noqa: F401
    import app.modules.identity.models as _i  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.notifications.models as _n  # noqa: F401
    import app.modules.risk.models as _r  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401
    import app.modules.transactions.models as _t  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in SCHEMAS:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    return engine


def _seed_param(session: Session, key: str, value: str, module: str = "transactions") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


@pytest.fixture()
def qa_session():
    """Sesion SQLite aislada con cuentas fondeadas y parametros E2-T18."""
    from app.modules.accounts import repository as accounts_repo

    engine = _make_engine()
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        a1 = accounts_repo.create_account(
            session, user_id=USER_A, account_number="00990001111", type="AHORRO", currency="PEN"
        )
        b1 = accounts_repo.create_account(
            session, user_id=USER_B, account_number="00880003333", type="AHORRO", currency="PEN"
        )
        accounts_repo.apply_delta(
            session, a1.id, available_delta=100_000, held_delta=0, currency="PEN"
        )
        accounts_repo.apply_delta(
            session, b1.id, available_delta=50_000, held_delta=0, currency="PEN"
        )
        _seed_param(session, THRESHOLD_KEY, "2000")
        _seed_param(session, DAILY_KEY, "500000")
        _seed_param(session, DAILY_STANDARD_KEY, "500000")
        _seed_param(session, MAX_ATTEMPTS_KEY, "5", module="identity")
        session.commit()
        yield {"session": session, "a1": a1.id, "b1": b1.id}
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def hmac_identity(monkeypatch):
    """Fachada `identity` simulada: binding en memoria + firma HMAC real."""
    from app.modules.identity.service import device_login as device_login_service

    secret = bytes.fromhex("ab" * 16)
    bindings: dict[tuple[str, str], dict] = {}

    def _fake_binding(session, user_id, device_id):
        found = bindings.get((str(user_id), device_id))
        return dict(found) if found is not None else None

    monkeypatch.setattr(device_login_service, "get_device_binding", _fake_binding)
    return {"secret": secret, "bindings": bindings}


def _add_binding(hmac_identity, user_id, device_id=DEVICE_A, status="ACTIVE"):
    hmac_identity["bindings"][(str(user_id), device_id)] = {
        "device_id": device_id,
        "public_key": "hmac:" + "ab" * 16,
        "status": status,
    }


def _sign(secret: bytes, nonce: str) -> str:
    return hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _http(session, user_id, monkeypatch):
    """TestClient con BD y usuario inyectados (patron E2-T19)."""
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import app
    from app.modules.transactions.api import get_current_user_id

    @contextmanager
    def _open():
        monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")

        def _override_db():
            yield session

        def _override_user():
            return user_id

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        try:
            with TestClient(app) as client:
                yield client
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_current_user_id, None)

    return _open()


def _key() -> str:
    return f"e2t22-{uuid.uuid4()}"


def _transfer(client, src, dst, amount, fee=0, key=None):
    return client.post(
        "/api/v1/transfers/third-party",
        json={
            "source_account_id": str(src),
            "target_account_id": str(dst),
            "amount_minor": amount,
            "currency": "PEN",
            "fee_minor": fee,
        },
        headers={"Idempotency-Key": key or _key()},
    )


def _authorize(client, tx_id, body, key=None):
    return client.post(
        f"/api/v1/transactions/{tx_id}/authorize",
        json=body,
        headers={"Idempotency-Key": key or _key()},
    )


def _detail(client, tx_id):
    return client.get(f"/api/v1/transactions/{tx_id}")


def _uid(tx_id) -> uuid.UUID:
    return tx_id if isinstance(tx_id, uuid.UUID) else uuid.UUID(str(tx_id))


def _avail(session, account_id) -> int:
    from app.modules.accounts import repository as accounts_repo

    return int(accounts_repo.get_balance(session, account_id).available_minor)


def _holds(session, tx_id):
    from app.modules.transactions import repository as tx_repo

    return list(tx_repo.list_holds_by_transaction(session, _uid(tx_id)))


def _entry_types(session, tx_id) -> list[str]:
    from app.modules.ledger.models import JournalEntry

    rows = session.scalars(
        sa.select(JournalEntry).where(JournalEntry.transaction_id == _uid(tx_id))
    ).all()
    return sorted(e.entry_type for e in rows)


def _outbox_rows(session, tx_id):
    from app.modules.shared.models import OutboxEntry

    return list(
        session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == _uid(tx_id))).all()
    )


def _outbox_types(session, tx_id) -> set[str]:
    return {r.event_type for r in _outbox_rows(session, tx_id)}


def _security_events(session, tx_id) -> list[dict]:
    return [
        r.payload for r in _outbox_rows(session, tx_id) if r.event_type == "auth.failed_attempt"
    ]


def _history_reasons(session, tx_id) -> list[str]:
    from app.modules.transactions import repository as tx_repo

    return [h.reason or "" for h in tx_repo.list_history(session, _uid(tx_id))]


def _nonce_of(session, tx_id) -> str:
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    tx = tx_repo.get_transaction(session, _uid(tx_id))
    return (bio_svc.get_block(tx) or {}).get("nonce") or ""


# ------------------------------------------------- Umbral (CA-01) punta a punta
def test_under_threshold_settles_without_challenge_nor_security_trace(
    qa_session, hmac_identity, monkeypatch
):
    """Bajo el umbral: liquida sin reto biometrico ni eventos de seguridad."""
    session, a1, b1 = qa_session["session"], qa_session["a1"], qa_session["b1"]
    with _http(session, USER_A, monkeypatch) as client:
        resp = _transfer(client, a1, b1, 1_000)
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["status"] == "SETTLED"
        assert resp.json()["data"]["failure_code"] is None
        tx_id = resp.json()["data"]["transaction_id"]
        detail = _detail(client, tx_id)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["status"] == "SETTLED"
    assert detail.json()["data"]["failure_code"] is None
    session.expire_all()
    assert "biometria requerida" not in _history_reasons(session, tx_id)
    assert _security_events(session, tx_id) == []
    assert _entry_types(session, tx_id) == ["TRANSFER_HOLD", "TRANSFER_SETTLE"]
    assert _avail(session, a1) == 100_000 - 1_000
    assert _avail(session, b1) == 50_000 + 1_000


def test_over_threshold_pends_with_hold_and_no_settlement(qa_session, hmac_identity, monkeypatch):
    """Sobre el umbral: `PENDING_AUTHORIZATION` con hold, sin liquidar."""
    session, a1, b1 = qa_session["session"], qa_session["a1"], qa_session["b1"]
    with _http(session, USER_A, monkeypatch) as client:
        resp = _transfer(client, a1, b1, 5_000)
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["status"] == "PENDING_AUTHORIZATION"
        tx_id = resp.json()["data"]["transaction_id"]
        detail = _detail(client, tx_id)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["status"] == "PENDING_AUTHORIZATION"
    assert any(
        "biometria requerida" in (h.get("reason") or "") for h in detail.json()["data"]["history"]
    )
    session.expire_all()
    holds = _holds(session, tx_id)
    assert len(holds) == 1 and holds[0].status == "ACTIVE"
    assert int(holds[0].amount_minor) == 5_000
    assert _entry_types(session, tx_id) == ["TRANSFER_HOLD"]
    assert _avail(session, a1) == 100_000 - 5_000
    assert _avail(session, b1) == 50_000  # destino intacto hasta autorizar
    types = _outbox_types(session, tx_id)
    assert {"transaction.initiated", "funds.held"} <= types
    assert "transfer.settled" not in types
    assert _nonce_of(session, tx_id) != ""


# ---------------------------------- Umbral configurable sin despliegue (regla 6)
def test_threshold_hot_change_via_facade_alters_gate_without_restart(
    qa_session, hmac_identity, monkeypatch
):
    """La fachada `shared.config` cambia el gate en caliente (sin reinicio)."""
    from app.modules.shared.config import parameters as params

    session, a1, b1 = qa_session["session"], qa_session["a1"], qa_session["b1"]
    assert params.get_biometric_threshold_minor(session) == 2_000
    params.set_parameter(session, key=THRESHOLD_KEY, value=1_000_000)
    session.commit()
    assert params.get_biometric_threshold_minor(session) == 1_000_000
    with _http(session, USER_A, monkeypatch) as client:
        settled = _transfer(client, a1, b1, 5_000)
        assert settled.status_code == 201, settled.text
        assert settled.json()["data"]["status"] == "SETTLED"
    params.set_parameter(session, key=THRESHOLD_KEY, value=2_000)
    session.commit()
    assert params.get_biometric_threshold_minor(session) == 2_000
    with _http(session, USER_A, monkeypatch) as client:
        pending = _transfer(client, a1, b1, 5_000)
        assert pending.status_code == 201, pending.text
        assert pending.json()["data"]["status"] == "PENDING_AUTHORIZATION"


# ----------------- Limite diario por perfil != STANDARD, acumulado (CA-03)
def test_daily_limit_per_profile_accumulates_end_to_end(qa_session, hmac_identity, monkeypatch):
    """Perfil `PREMIUM` con limite propio fijado por fachada: el acumulado
    rechaza `DAILY_LIMIT_EXCEEDED` sin hold; `STANDARD` liquida igual monto."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.identity import repository as identity_repo
    from app.modules.shared.config import parameters as params

    session = qa_session["session"]
    _seed_param(session, DAILY_PREMIUM_KEY, "8000")
    session.commit()
    assert params.set_parameter(session, key=DAILY_PREMIUM_KEY, value=8_000) == 8_000
    params.set_parameter(session, key=THRESHOLD_KEY, value=1_000_000)  # aisla el limite
    session.commit()

    premium = identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash="h-premium-e2t22",
        first_name="Ana",
        last_name="Premium",
        email="premium-e2t22@test.xx",
        risk_profile="PREMIUM",
    )
    standard = identity_repo.create_user(
        session,
        doc_type="DNI",
        doc_number_hash="h-standard-e2t22",
        first_name="Ben",
        last_name="Standard",
        email="standard-e2t22@test.xx",
    )
    session.commit()
    p_src = accounts_repo.create_account(
        session, user_id=premium.id, account_number="00990009111", type="AHORRO", currency="PEN"
    )
    p_dst = accounts_repo.create_account(
        session, user_id=USER_B, account_number="00990009112", type="AHORRO", currency="PEN"
    )
    s_src = accounts_repo.create_account(
        session, user_id=standard.id, account_number="00990009113", type="AHORRO", currency="PEN"
    )
    s_dst = accounts_repo.create_account(
        session, user_id=USER_B, account_number="00990009114", type="AHORRO", currency="PEN"
    )
    accounts_repo.apply_delta(
        session, p_src.id, available_delta=100_000, held_delta=0, currency="PEN"
    )
    accounts_repo.apply_delta(
        session, s_src.id, available_delta=100_000, held_delta=0, currency="PEN"
    )
    session.commit()
    premium_id, standard_id = premium.id, standard.id

    with _http(session, premium_id, monkeypatch) as client:
        first = _transfer(client, p_src.id, p_dst.id, 5_000)
        assert first.status_code == 201, first.text
        assert first.json()["data"]["status"] == "SETTLED"
        second = _transfer(client, p_src.id, p_dst.id, 5_000)  # 5k + 5k > 8k
        assert second.status_code == 201, second.text
        assert second.json()["data"]["status"] == "REJECTED"
        assert second.json()["data"]["failure_code"] == "DAILY_LIMIT_EXCEEDED"
        rejected_id = second.json()["data"]["transaction_id"]
    session.expire_all()
    assert _holds(session, rejected_id) == []  # sin hold
    assert _entry_types(session, rejected_id) == []  # sin asientos
    assert _avail(session, p_src.id) == 100_000 - 5_000  # solo la primera
    assert _avail(session, p_dst.id) == 5_000
    assert "limite diario excedido" in _history_reasons(session, rejected_id)

    with _http(session, standard_id, monkeypatch) as client:
        ok = _transfer(client, s_src.id, s_dst.id, 5_000)
        assert ok.status_code == 201, ok.text
        assert ok.json()["data"]["status"] == "SETTLED"
        assert ok.json()["data"]["failure_code"] is None


# ------------------------- Bloqueo preventivo integrado (CA-02 + CA-04)
def test_preventive_block_releases_hold_restores_and_feeds_risk(
    qa_session, hmac_identity, monkeypatch
):
    """N fallos HTTP -> `REJECTED BIOMETRIC_PREVENTIVE_BLOCK`, hold liberado,
    saldos restaurados, evento `auth.failed_attempt(blocked)` que `risk`
    consume (perfil + push `auth_blocked` + auditoria)."""
    from app.adapters.notification_sender import MockNotificationSender
    from app.modules.risk import repository as risk_repo
    from app.modules.risk import service as risk_service
    from app.modules.shared.config import parameters as params

    session, a1, b1 = qa_session["session"], qa_session["a1"], qa_session["b1"]
    params.set_parameter(session, key=MAX_ATTEMPTS_KEY, value=3)
    session.commit()
    _add_binding(hmac_identity, USER_A)
    with _http(session, USER_A, monkeypatch) as client:
        created = _transfer(client, a1, b1, 5_000)
        assert created.json()["data"]["status"] == "PENDING_AUTHORIZATION"
        tx_id = created.json()["data"]["transaction_id"]
        nonce = _nonce_of(session, tx_id)
        bad = {"device_id": DEVICE_A, "signature": "00" * 32}
        first = _authorize(client, tx_id, bad, key=_key())
        second = _authorize(client, tx_id, bad, key=_key())
        third = _authorize(client, tx_id, bad, key=_key())
    assert first.status_code == 403, first.text
    assert second.status_code == 403, second.text
    assert third.status_code == 200, third.text
    assert third.json()["data"]["status"] == "REJECTED"
    assert third.json()["data"]["failure_code"] == "BIOMETRIC_PREVENTIVE_BLOCK"
    session.expire_all()
    holds = _holds(session, tx_id)
    assert len(holds) == 1 and holds[0].status == "RELEASED"
    assert _avail(session, a1) == 100_000  # retenido devuelto
    assert _avail(session, b1) == 50_000  # destino nunca tocado
    assert [e for e in _entry_types(session, tx_id) if e == "TRANSFER_SETTLE"] == []
    assert "bloqueo preventivo tras N intentos fallidos" in _history_reasons(session, tx_id)

    rows = _outbox_rows(session, tx_id)
    blocked_rows = [
        r for r in rows if r.event_type == "auth.failed_attempt" and r.payload.get("blocked")
    ]
    assert len([r for r in rows if r.event_type == "auth.failed_attempt"]) == 3
    assert len(blocked_rows) == 1
    assert blocked_rows[0].payload["attempt"] == 3
    assert blocked_rows[0].payload["reason"] == "blocked"
    raw = str(blocked_rows[0].payload)
    assert "signature" not in raw and nonce not in raw  # sin secretos

    outcome = risk_service.handle_auth_failed_attempt(
        session,
        event_id=blocked_rows[0].id,
        payload=dict(blocked_rows[0].payload),
        sender=MockNotificationSender(),
    )
    session.commit()
    assert outcome["consumed"] is True and outcome["blocked"] is True
    profile = risk_repo.get_by_user(session, USER_A)
    assert profile is not None and profile.score == 5  # 1 + 4 extra por bloqueo

    import sqlalchemy as _sa

    from app.modules.audit.models import AuditLog
    from app.modules.notifications.models import Notification

    notes = list(session.scalars(_sa.select(Notification)).all())
    assert len(notes) == 1
    assert notes[0].template_code == "auth_blocked" and notes[0].channel == "push"
    assert notes[0].status == "SENT" and notes[0].user_id == USER_A
    audits = list(session.scalars(_sa.select(AuditLog)).all())
    assert len(audits) == 1 and audits[0].action == "risk.auth_preventive_block"


# ---------------------- Nonce expirado: conserva hold y alimenta a risk
def test_expired_nonce_counts_attempt_keeps_hold_and_feeds_risk(
    qa_session, hmac_identity, monkeypatch
):
    """`EXPIRED_NONCE` HTTP: cuenta el intento sin liberar el hold; el evento
    (`reason=expired`) lo consume `risk` (perfil + push intento fallido)."""
    from app.adapters.notification_sender import MockNotificationSender
    from app.modules.risk import repository as risk_repo
    from app.modules.risk import service as risk_service
    from app.modules.transactions import repository as tx_repo
    from app.modules.transactions.service import biometric as bio_svc

    session, a1, b1 = qa_session["session"], qa_session["a1"], qa_session["b1"]
    _add_binding(hmac_identity, USER_A)
    with _http(session, USER_A, monkeypatch) as client:
        created = _transfer(client, a1, b1, 5_000)
        tx_id = created.json()["data"]["transaction_id"]
    session.expire_all()
    stored = tx_repo.get_transaction(session, _uid(tx_id))
    block = bio_svc.get_block(stored)
    assert block is not None
    block["expires_at"] = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    bio_svc.save_block(session, tx_repo.get_transaction(session, _uid(tx_id)), block)
    session.commit()
    body = {
        "device_id": DEVICE_A,
        "signature": _sign(hmac_identity["secret"], block["nonce"]),
    }
    with _http(session, USER_A, monkeypatch) as client:
        resp = _authorize(client, tx_id, body, key=_key())
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "EXPIRED_NONCE"
    session.expire_all()
    fresh = tx_repo.get_transaction(session, _uid(tx_id))
    assert fresh.status == "PENDING_AUTHORIZATION"
    assert (bio_svc.get_block(fresh) or {})["failed_attempts"] == 1
    holds = _holds(session, tx_id)
    assert len(holds) == 1 and holds[0].status == "ACTIVE"  # hold intacto
    assert _entry_types(session, tx_id) == ["TRANSFER_HOLD"]  # sin liquidar
    assert _avail(session, b1) == 50_000
    rows = [r for r in _outbox_rows(session, tx_id) if r.event_type == "auth.failed_attempt"]
    assert len(rows) == 1
    assert rows[0].payload["reason"] == "expired" and rows[0].payload["blocked"] is False

    outcome = risk_service.handle_auth_failed_attempt(
        session,
        event_id=rows[0].id,
        payload=dict(rows[0].payload),
        sender=MockNotificationSender(),
    )
    session.commit()
    assert outcome["consumed"] is True and outcome["blocked"] is False
    assert risk_repo.get_by_user(session, USER_A).score == 1

    import sqlalchemy as _sa

    from app.modules.notifications.models import Notification

    notes = list(session.scalars(_sa.select(Notification)).all())
    assert len(notes) == 1 and notes[0].template_code == "auth_failed_attempt"


# ------------------ Parametro cambiado a mitad de operacion (consistencia)
def test_param_change_mid_operation_leaves_consistent_state(qa_session, hmac_identity, monkeypatch):
    """E2-T36 (adapta la semantica derogada "sin recalculo"): si el limite
    diario baja en caliente por debajo de la pendiente, el `authorize`
    re-chequea (`acumulado + total > limite`) y la rechaza
    (`REJECTED`/`DAILY_LIMIT_EXCEEDED`, hold liberado, sin liquidar);
    las NUEVAS operaciones usan lo vigente (rechazo en la iniciacion)."""
    from app.modules.shared.config import parameters as params

    session, a1, b1 = qa_session["session"], qa_session["a1"], qa_session["b1"]
    _add_binding(hmac_identity, USER_A)
    with _http(session, USER_A, monkeypatch) as client:
        created = _transfer(client, a1, b1, 5_000)
        assert created.json()["data"]["status"] == "PENDING_AUTHORIZATION"
        tx_id = created.json()["data"]["transaction_id"]
    params.set_parameter(session, key=THRESHOLD_KEY, value=1_000_000)
    params.set_parameter(session, key=DAILY_KEY, value=1)
    session.commit()
    nonce = _nonce_of(session, tx_id)
    with _http(session, USER_A, monkeypatch) as client:
        done = _authorize(
            client,
            tx_id,
            {"device_id": DEVICE_A, "signature": _sign(hmac_identity["secret"], nonce)},
            key=_key(),
        )
        assert done.status_code == 200, done.text
        assert done.json()["data"]["status"] == "REJECTED"
        assert done.json()["data"]["failure_code"] == "DAILY_LIMIT_EXCEEDED"
        fresh_op = _transfer(client, a1, b1, 5_000)  # rige el limite nuevo
        assert fresh_op.status_code == 201, fresh_op.text
        assert fresh_op.json()["data"]["status"] == "REJECTED"
        assert fresh_op.json()["data"]["failure_code"] == "DAILY_LIMIT_EXCEEDED"
    session.expire_all()
    assert _entry_types(session, tx_id) == ["HOLD_RELEASE", "TRANSFER_HOLD"]
    holds = _holds(session, tx_id)
    assert len(holds) == 1 and holds[0].status == "RELEASED"
    assert _avail(session, a1) == 100_000  # hold devuelto, sin liquidar
    assert _avail(session, b1) == 50_000  # destino nunca tocado


# ---------------------------- Idempotencia del alta (doble POST, una op)
def test_duplicate_transfer_creation_yields_single_pending_operation(
    qa_session, hmac_identity, monkeypatch
):
    """Doble `POST /transfers/third-party` con misma clave: misma operacion,
    un solo hold y un solo asiento de retencion (trazabilidad)."""
    session, a1, b1 = qa_session["session"], qa_session["a1"], qa_session["b1"]
    key = _key()
    with _http(session, USER_A, monkeypatch) as client:
        first = _transfer(client, a1, b1, 5_000, key=key)
        second = _transfer(client, a1, b1, 5_000, key=key)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["data"]["transaction_id"] == second.json()["data"]["transaction_id"]
    assert second.json()["data"]["status"] == "PENDING_AUTHORIZATION"
    tx_id = first.json()["data"]["transaction_id"]
    session.expire_all()
    assert len(_holds(session, tx_id)) == 1
    assert _entry_types(session, tx_id) == ["TRANSFER_HOLD"]
    assert _avail(session, a1) == 100_000 - 5_000  # una sola retencion
