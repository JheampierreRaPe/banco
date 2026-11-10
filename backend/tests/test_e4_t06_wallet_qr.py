"""QR firmado + `qr_charges` (E4-T06, HU14 CA-01/CA-03 + caducidad).

- Parte A (sin BD): dominio puro `wallet.domain.qr` (payload
  canonico determinista, `payload_hash` estable, firma HMAC valida,
  rechazo de firma alterada, caducidad con `now` inyectable,
  interop `BO-QR1` (de)codificable por el adaptador de `E4-T03`,
  sin `float`, sin PII).
- Parte B (SQLite en memoria + schemas ATTACH, sin red): persistencia
  (`create_qr_charge` deja fila `ACTIVE` con `payload_hash` /
  `signature` / `expires_at = now + wallet.qr_ttl_seconds`), evento
  `qr.charge.created` por `outbox` (sin PII), comercios demo (G69),
  TTL configurable (G70), transiciones (`PAID`/`EXPIRED`/`CANCELLED`)
  y errores (comercio inexistente/bloqueado, monto/moneda/modo).
- Parte C (migraciones): `0039_wallet_qr` encadena tras
  `0038_wallet_express_params` y deja un solo head.

Sin endpoint publico (el router es `E4-T08`); no mueve dinero.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.modules.audit.models as _audit
import app.modules.shared.models as _shared
import app.modules.wallet.models as _wallet
from app.adapters import wallet_interop as interop
from app.core.db import Base
from app.modules.shared.config import parameters as params
from app.modules.wallet.domain import qr as qr_domain
from app.modules.wallet.repository import qr as qr_repo
from app.modules.wallet.service import qr as qr_service

assert _audit is not None and _shared is not None and _wallet is not None

SECRET = "test-qr-signing-secret"
OTHER_SECRET = "otro-secreto"
RUC_PLAIN = "20123456789"

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
MERCHANT_ID = uuid.uuid4()

TABLES = [
    "wallet.merchant_accounts",
    "wallet.qr_charges",
    "shared.outbox",
    "audit.audit_log",
]

SCHEMAS = ("wallet", "shared", "config", "audit")


def _payload(**overrides):
    base = {
        "charge_id": uuid.uuid4(),
        "merchant_id": MERCHANT_ID,
        "amount_minor": 5_000,
        "currency": "PEN",
        "concept": "Ceviche + chicha",
        "mode": "DYNAMIC",
        "expires_at": NOW + timedelta(seconds=300),
    }
    base.update(overrides)
    return qr_domain.build_payload(**base)


# ------------------------------------------------------- Parte A: dominio puro


def test_build_payload_canonical_and_deterministic():
    first = _payload()
    second = _payload(
        charge_id=first["charge_id"],
        merchant_id=first["merchant_id"],
        amount_minor=5_000,
        currency="pen",
        concept="Ceviche + chicha",
        mode="DYNAMIC",
        expires_at=NOW + timedelta(seconds=300),
    )
    assert first == second
    assert first["v"] == qr_domain.QR_FORMAT_VERSION == "BO-QR1"
    assert first["currency"] == "PEN"
    assert first["exp"] == int((NOW + timedelta(seconds=300)).timestamp())
    assert qr_domain.canonical_bytes(first) == qr_domain.canonical_bytes(second)


def test_payload_hash_stable_and_64_hex():
    payload = _payload()
    first = qr_domain.payload_hash_for(payload)
    second = qr_domain.payload_hash_for(dict(payload))
    assert first == second
    assert len(first) == 64
    assert all(ch in "0123456789abcdef" for ch in first)


def test_sign_verify_roundtrip_deterministic():
    payload = _payload()
    first = qr_domain.sign_payload(payload, secret=SECRET)
    second = qr_domain.sign_payload(payload, secret=SECRET)
    assert first == second
    assert len(first) == 64
    assert qr_domain.verify_signature(payload, first, secret=SECRET) is True


def test_verify_rejects_tampered_payload():
    payload = _payload()
    signature = qr_domain.sign_payload(payload, secret=SECRET)
    for field, evil in (
        ("amount_minor", 5_001),
        ("merchant_id", str(uuid.uuid4())),
        ("charge_id", str(uuid.uuid4())),
        ("currency", "USD"),
        ("concept", "otro concepto"),
        ("mode", "STATIC"),
        ("exp", payload["exp"] + 60),
    ):
        tampered = dict(payload)
        tampered[field] = evil
        assert qr_domain.verify_signature(tampered, signature, secret=SECRET) is False
    assert qr_domain.verify_signature(payload, "", secret=SECRET) is False
    assert qr_domain.verify_signature(payload, None, secret=SECRET) is False
    assert qr_domain.verify_signature(payload, signature, secret=OTHER_SECRET) is False


def test_signer_hides_secret_and_uses_constant_time_compare():
    signer = qr_domain.HmacQrSigner(secret=SECRET)
    assert SECRET not in repr(signer)
    payload = _payload()
    assert signer.verify(payload, signer.sign(payload)) is True


def test_amounts_are_integers_without_float():
    for bad in (True, 5_000.0, "5000", None, 0, -1):
        with pytest.raises((TypeError, ValueError)):
            _payload(amount_minor=bad)


def test_rejects_bad_currency_mode_concept():
    with pytest.raises(ValueError):
        _payload(currency="XX")
    with pytest.raises(ValueError):
        _payload(mode="REUSABLE")
    with pytest.raises(ValueError):
        _payload(concept="x" * 121)


def test_is_expired_with_injected_now():
    charge = {"expires_at": NOW + timedelta(seconds=300)}
    assert qr_domain.is_expired(charge, NOW) is False
    assert qr_domain.is_expired(charge, NOW + timedelta(seconds=299)) is False
    assert qr_domain.is_expired(charge, NOW + timedelta(seconds=300)) is True
    assert qr_domain.is_expired(charge, NOW + timedelta(seconds=3_600)) is True
    static = {"expires_at": None}
    assert qr_domain.is_expired(static, NOW + timedelta(days=30)) is False


def test_interop_envelope_roundtrip_via_adapter():
    payload = _payload()
    signature = qr_domain.sign_payload(payload, secret=SECRET)
    token = qr_domain.to_interop_token(payload, signature)
    decoded = interop.decode_payload(token)
    assert decoded.ref == str(payload["charge_id"])
    assert decoded.amount_minor == payload["amount_minor"]
    assert decoded.currency == payload["currency"]
    assert decoded.expires_at == payload["exp"]
    assert decoded.sig == signature
    fields = qr_domain.envelope_fields(token)
    assert fields["ref"] == str(payload["charge_id"])
    assert fields["sig"] == signature


def test_hash_tax_id_deterministic_without_plain():
    first = qr_domain.hash_tax_id(RUC_PLAIN)
    second = qr_domain.hash_tax_id(RUC_PLAIN)
    assert first == second
    assert len(first) == 64
    assert RUC_PLAIN not in first
    with pytest.raises(ValueError):
        qr_domain.hash_tax_id("123")


# ------------------------------------------------------- Parte B: persistencia


def _seed_param(session: Session, key: str, value: str, module: str = "wallet") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


@pytest.fixture()
def env(monkeypatch):
    """QR firmado en SQLite: comercios demo + parametro TTL."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    monkeypatch.setenv("OUTBOX_WORKER_ENABLED", "0")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in SCHEMAS:
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE config.parameters (id TEXT PRIMARY KEY, "
                "key TEXT NOT NULL UNIQUE, value_json TEXT NOT NULL, "
                "module TEXT NOT NULL, description TEXT, updated_by TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        _seed_param(session, params.WALLET_QR_TTL_SECONDS_KEY, "300")
        merchants = qr_repo.ensure_demo_merchants(session)
        session.commit()
        yield {"session": session, "merchants": merchants}
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def test_create_qr_charge_happy_path(env):
    session = env["session"]
    merchant = env["merchants"][0]
    before = NOW
    view = qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=5_000,
        currency="PEN",
        concept="Menu del dia",
        mode="DYNAMIC",
        now=before,
        secret=SECRET,
    )
    assert view["status"] == "ACTIVE"
    assert view["amount_minor"] == 5_000
    assert view["currency"] == "PEN"
    assert view["expires_at"] == before + timedelta(seconds=300)
    assert len(view["payload_hash"]) == 64
    assert qr_domain.verify_signature(view["payload"], view["signature"], secret=SECRET)
    assert view["payload"]["exp"] == int(view["expires_at"].timestamp())

    row = qr_repo.get_qr_charge(session, view["charge_id"])
    assert row is not None
    assert row.status == "ACTIVE"
    assert row.payload_hash == view["payload_hash"]
    assert row.signature == view["signature"]
    # SQLite devuelve naive: comparar por instante (el servicio usa aware UTC).
    stored = row.expires_at
    if stored is not None and stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    assert stored == view["expires_at"]

    from app.modules.shared.models import OutboxEntry

    events = session.scalars(sa.select(OutboxEntry).where(OutboxEntry.aggregate_id == row.id)).all()
    assert len(events) == 1
    assert events[0].event_type == "qr.charge.created"
    payload = events[0].payload
    assert payload["charge_id"] == str(row.id)
    assert payload["merchant_id"] == str(merchant.id)
    assert payload["amount_minor"] == 5_000
    assert payload["currency"] == "PEN"
    assert payload["mode"] == "DYNAMIC"
    assert payload["expires_at"] == view["expires_at"].isoformat()
    blob = str(payload)
    assert SECRET not in blob
    assert merchant.business_name not in blob
    assert RUC_PLAIN not in blob


def test_create_qr_charge_respects_configured_ttl_and_fallback(env):
    session = env["session"]
    merchant = env["merchants"][0]
    params.set_parameter(session, key=params.WALLET_QR_TTL_SECONDS_KEY, value=600)
    session.flush()
    assert qr_service.get_qr_ttl_seconds(session) == 600
    view = qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=1_000,
        currency="PEN",
        now=NOW,
        secret=SECRET,
    )
    assert view["expires_at"] == NOW + timedelta(seconds=600)
    session.rollback()
    with pytest.raises(params.ParameterValidationError):
        params.set_parameter(session, key=params.WALLET_QR_TTL_SECONDS_KEY, value=1.5)
    session.rollback()
    session.execute(sa.text("DELETE FROM config.parameters"))
    session.flush()
    assert qr_service.get_qr_ttl_seconds(session) == params.DEFAULT_WALLET_QR_TTL_SECONDS
    assert qr_service.get_qr_ttl_seconds(session) == 300


def test_create_qr_charge_static_without_strict_expiry(env):
    session = env["session"]
    merchant = env["merchants"][1]
    view = qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=2_500,
        currency="PEN",
        concept="Caja chica",
        mode="STATIC",
        now=NOW,
        secret=SECRET,
    )
    assert view["status"] == "ACTIVE"
    assert view["expires_at"] is None
    assert view["payload"]["exp"] == 0
    assert qr_domain.is_expired({"expires_at": None}, NOW + timedelta(days=30)) is False


def test_create_qr_charge_rejects_unknown_or_blocked_merchant(env):
    session = env["session"]
    with pytest.raises(qr_service.QrMerchantNotFoundError):
        qr_service.create_qr_charge(
            session,
            merchant_id=uuid.uuid4(),
            amount_minor=1_000,
            currency="PEN",
            now=NOW,
            secret=SECRET,
        )
    blocked = qr_repo.create_merchant(session, user_id=uuid.uuid4(), business_name="Bloqueado")
    blocked.status = "BLOCKED"
    session.flush()
    with pytest.raises(qr_service.QrMerchantNotFoundError):
        qr_service.create_qr_charge(
            session,
            merchant_id=blocked.id,
            amount_minor=1_000,
            currency="PEN",
            now=NOW,
            secret=SECRET,
        )
    session.rollback()


def test_create_qr_charge_rejects_bad_input(env):
    session = env["session"]
    merchant = env["merchants"][0]
    for kwargs in (
        {"amount_minor": 0},
        {"amount_minor": -5},
        {"amount_minor": 10.5},
        {"currency": "XX"},
        {"mode": "REUSABLE"},
        {"concept": "x" * 121},
        {"ttl_seconds": 0},
    ):
        with pytest.raises(qr_service.QrValidationError):
            qr_service.create_qr_charge(
                session,
                merchant_id=merchant.id,
                amount_minor=kwargs.pop("amount_minor", 1_000),
                currency=kwargs.pop("currency", "PEN"),
                now=NOW,
                secret=SECRET,
                **kwargs,
            )
    with pytest.raises(qr_service.QrValidationError):
        qr_service.create_qr_charge(
            session,
            merchant_id="no-es-uuid",
            amount_minor=1_000,
            currency="PEN",
            now=NOW,
            secret=SECRET,
        )
    session.rollback()


def test_demo_merchants_idempotent_and_crud(env):
    session = env["session"]
    first = qr_repo.ensure_demo_merchants(session)
    second = qr_repo.ensure_demo_merchants(session)
    assert [str(m.id) for m in first] == [str(m.id) for m in second]
    assert len(first) == 2
    assert all(m.status == "ACTIVE" for m in first)
    assert first[0].business_name == "Bodega Demo San Juan"

    hashed = qr_domain.hash_tax_id(RUC_PLAIN)
    row = qr_repo.create_merchant(
        session,
        user_id=uuid.uuid4(),
        business_name="Tienda Nueva",
        tax_id_hash=hashed,
    )
    assert row.tax_id_hash == hashed
    assert RUC_PLAIN not in (row.tax_id_hash or "")
    assert qr_repo.get_merchant_by_user(session, row.user_id).id == row.id
    with pytest.raises(qr_repo.DuplicateMerchantError):
        qr_repo.create_merchant(session, user_id=row.user_id, business_name="Duplicado")
    session.rollback()


def test_mark_transitions(env):
    session = env["session"]
    merchant = env["merchants"][0]
    view = qr_service.create_qr_charge(
        session,
        merchant_id=merchant.id,
        amount_minor=1_000,
        currency="PEN",
        now=NOW,
        secret=SECRET,
    )
    row = qr_repo.get_qr_charge(session, view["charge_id"])
    assert row is not None
    paid_at = NOW + timedelta(seconds=60)
    qr_repo.mark_paid(session, row, paid_at=paid_at)
    assert row.status == "PAID"
    assert row.paid_at == paid_at
    listed = qr_repo.list_charges_by_merchant(session, merchant.id, status="PAID")
    assert [str(r.id) for r in listed] == [view["charge_id"]]
    qr_repo.mark_expired(session, row)
    assert row.status == "EXPIRED"
    qr_repo.mark_cancelled(session, row)
    assert row.status == "CANCELLED"
    session.rollback()


def test_qr_logs_without_secrets(env, caplog):
    session = env["session"]
    hashed = qr_domain.hash_tax_id(RUC_PLAIN)
    other = qr_repo.create_merchant(
        session, user_id=uuid.uuid4(), business_name="Tienda X", tax_id_hash=hashed
    )
    with caplog.at_level(logging.DEBUG, logger="app.modules.wallet"):
        view = qr_service.create_qr_charge(
            session,
            merchant_id=other.id,
            amount_minor=1_000,
            currency="PEN",
            now=NOW,
            secret=SECRET,
        )
        assert view["status"] == "ACTIVE"
    session.rollback()
    text = caplog.text
    assert SECRET not in text
    assert RUC_PLAIN not in text
    assert hashed not in text


# ------------------------------------------------------- Parte C: migraciones


def test_pg_0039_downgrade_reversible_with_charge(test_engine):
    """MEDIO-1: `upgrade -> cargo -> downgrade -> upgrade` sin error de FK.

    Sin el fix el `downgrade()` de `0039_wallet_qr` borraba los
    comercios demo antes que `qr_charges` y Postgres violaba
    `fk_qr_charges_merchant` al existir cargos. Se omite sin Postgres
    de prueba (el `test_engine` hace `skip`).
    """
    from sqlalchemy.orm import Session as SASession

    from tests.db_utils import (
        downgrade_in_process,
        resolve_test_database_url,
        run_migrations_in_process,
    )

    url = resolve_test_database_url()
    session = SASession(bind=test_engine, autoflush=False, expire_on_commit=False)
    charge_ids: list[str] = []
    try:
        # Cargo sobre un comercio demo: el `downgrade()` los borra y, sin
        # el fix, ese `DELETE` viola `fk_qr_charges_merchant`.
        demo_id = "a1111111-1111-4111-8111-111111111111"
        demo = qr_repo.get_merchant(session, demo_id)
        assert demo is not None and str(demo.status) == "ACTIVE"
        view = qr_service.create_qr_charge(
            session,
            merchant_id=demo.id,
            amount_minor=1_000,
            currency="PEN",
            secret=SECRET,
        )
        session.commit()
        assert view["status"] == "ACTIVE"
        charge_ids.append(view["charge_id"])
        try:
            # Con cargos existentes el downgrade no debe violar la FK.
            downgrade_in_process(url, "0038_wallet_express_params")
            with test_engine.connect() as conn:
                assert (
                    conn.execute(sa.text("SELECT to_regclass('wallet.qr_charges')")).scalar()
                    is None
                )
                assert (
                    conn.execute(sa.text("SELECT to_regclass('wallet.merchant_accounts')")).scalar()
                    is None
                )
            run_migrations_in_process(url, "head")
            with test_engine.connect() as conn:
                assert (
                    conn.execute(sa.text("SELECT to_regclass('wallet.qr_charges')")).scalar()
                    == "wallet.qr_charges"
                )
                assert (
                    conn.execute(sa.text("SELECT to_regclass('wallet.merchant_accounts')")).scalar()
                    == "wallet.merchant_accounts"
                )
            # Reversible funcional: comercios demo re-sembrados y alta nueva.
            fresh = SASession(bind=test_engine, autoflush=False, expire_on_commit=False)
            try:
                demo = qr_repo.get_merchant(fresh, "a1111111-1111-4111-8111-111111111111")
                assert demo is not None and str(demo.status) == "ACTIVE"
                retry = qr_service.create_qr_charge(
                    fresh,
                    merchant_id=demo.id,
                    amount_minor=2_000,
                    currency="PEN",
                    secret=SECRET,
                )
                assert retry["status"] == "ACTIVE"
                fresh.commit()
                charge_ids.append(retry["charge_id"])
            finally:
                fresh.close()
        finally:
            run_migrations_in_process(url, "head")
            # Limpieza: los eventos/auditoria viven en tablas compartidas
            # (`shared.outbox`, `audit.audit_log`) que el ciclo no recrea;
            # sin borrarlos se contamina a `test_shared_outbox`.
            with test_engine.begin() as conn:
                for charge_id in charge_ids:
                    conn.execute(
                        sa.text(
                            "DELETE FROM shared.outbox " "WHERE aggregate_id = CAST(:id AS uuid)"
                        ),
                        {"id": charge_id},
                    )
                    conn.execute(
                        sa.text(
                            "DELETE FROM audit.audit_log "
                            "WHERE entity_type = 'qr_charge' "
                            "AND entity_id = CAST(:id AS uuid)"
                        ),
                        {"id": charge_id},
                    )
    finally:
        session.close()


def test_migration_0039_chained_after_0038_with_single_head():
    import pathlib
    import re

    versions_dir = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"
    revisions: dict[str, str | None] = {}
    for path in versions_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision\s*=\s*"([^"]+)"', text, re.MULTILINE)
        down = re.search(r'^down_revision\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if rev is None:
            continue
        assert len(rev.group(1)) <= 32, f"revision > 32 chars: {rev.group(1)}"
        revisions[rev.group(1)] = down.group(1) if down else None
    # Fix guarda (2026-10-13): la cabeza avanza a
    # 0041_wallet_qr_payment_guard (indice unico parcial anti doble
    # cobro, tras 0040). E4-T16 (2026-10-13): la cabeza avanza a
    # 0042_wallet_services (catalogo `billers` + servicios, tras 0041). E6-T12
    # (2026-10-13): la cabeza avanza a 0044_notifications_preferences
    # (preferencias por canal + claves de seguridad, tras 0043). Pulido de
    # correos (2026-10-15): la cabeza avanza a
    # 0045_notification_email_content (textos de semillas, tras 0044).
    # E4-T21 (2026-10-15): la cabeza avanza a 0047_wallet_personal_qr
    # (QR personal estatico, tras 0046).
    assert revisions.get("0047_wallet_personal_qr") == "0046_services_settlement"
    assert revisions.get("0046_services_settlement") == "0045_notification_email_content"
    assert revisions.get("0045_notification_email_content") == "0044_notifications_preferences"
    assert revisions.get("0042_wallet_services") == "0041_wallet_qr_payment_guard"
    assert revisions.get("0041_wallet_qr_payment_guard") == "0040_wallet_qr_payments"
    assert revisions.get("0040_wallet_qr_payments") == "0039_wallet_qr"
    assert revisions.get("0039_wallet_qr") == "0038_wallet_express_params"
    assert revisions.get("0038_wallet_express_params") == "0037_wallet"
    assert revisions.get("0037_wallet") == "0036_loan_reminder_param"
    referenced = {down for down in revisions.values() if down is not None}
    heads = [rev for rev in revisions if rev not in referenced]
    assert heads == ["0047_wallet_personal_qr"], f"heads inesperados: {heads}"
    content = (versions_dir / "0039_wallet_qr.py").read_text(encoding="utf-8")
    assert "merchant_accounts" in content
    assert "qr_charges" in content
    assert "wallet.qr_ttl_seconds" in content
    assert "Bodega Demo San Juan" in content
    assert "Cafe Demo Miraflores" in content
    assert "def upgrade" in content and "def downgrade" in content
