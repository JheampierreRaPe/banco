"""Servicio de firma del contrato (E3-T13, HU11 CA-02/CA-03).

- Camino feliz dispositivo: juego `GENERATED` + reto (`nonce` real de
  `identity.domain.nonce`) + firma HMAC valida (verificador REAL
  `device_login.verify_signature`, sin tablas de `identity`) ->
  3 filas `SIGNED` con `signed_at` + `signature_token_hash`, sellos
  verificables, evento `credit.contract.signed` + auditoria.
- Camino feliz PIN (fallback, patron E2-T31): `sign_contract_with_pin`
  con verificador inyectado -> mismo sellado; el PIN jamas se persiste
  ni se loguea.
- Errores: firma invalida / binding ausente o revocado / dispositivo
  distinto / PIN erroneo -> MISMO generico `NOT_AUTHORIZED` (sin
  oraculo), contrato intacto; `nonce` vencido -> `EXPIRED_NONCE`;
  `nonce` reutilizado -> generico; ya `SIGNED` -> `CONFLICT`;
  ajeno/inexistente/sin juego -> `NOT_FOUND` neutro.
- No reutilizacion: token repetido (funcion hash forzada) -> `CONFLICT`.
- Frontera D-03: la verificacion usa las fachadas de `identity`
  (`get_device_binding` + `verify_signature` + `verify_holder_pin`,
  delegacion con `monkeypatch`); el codigo de `credits` no importa
  modelos/repositorios ajenos (barrido estatico).
- Sello puro: determinista, sensible a cualquier cambio, verificable.
- Sin PII/secretos en logs; aislamiento (sin `loans`, sin dinero).

Humo sin Postgres: SQLite en memoria + schemas ATTACH (patron del
repo); `identity.domain.nonce` es en memoria (se resetea por test).
Sin red. Las llamadas al servicio son directas (sin router propio;
los endpoints van en E3-T14).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.modules.audit.models as _audit
import app.modules.credits.models as _credits
import app.modules.notifications.models as _notifications
import app.modules.shared.models as _shared
from app.adapters.credit_bureau import BureauScore, MockCreditBureau
from app.core.db import Base
from app.modules.credits import repository as repo
from app.modules.credits import service as catalog
from app.modules.credits.domain import signing as signing_domain
from app.modules.credits.repository import contracts as contracts_repo
from app.modules.credits.service import applications as apps_service
from app.modules.credits.service import documents as docs_service
from app.modules.credits.service import signing as signing_service
from app.modules.identity.domain import nonce as nonce_domain

USER = uuid.uuid4()
OTHER = uuid.uuid4()
DEVICE = "dev-e3t13-1"
SECRET_HEX = "ab" * 20  # 20 bytes (>= 16 exigidos por el formato hmac:)
PUBLIC_KEY = f"hmac:{SECRET_HEX}"
PIN = "4829"

TABLES = [
    "credits.loan_products",
    "credits.loan_applications",
    "credits.contracts",
    "shared.outbox",
    "shared.processed_events",
    "notifications.notifications",
    "notifications.notification_templates",
    "audit.audit_log",
]

assert _audit is not None and _credits is not None
assert _notifications is not None and _shared is not None


def _bureau_for(user_id: uuid.UUID, score: int = 780):
    return MockCreditBureau(
        profiles={str(user_id): BureauScore(score=score, deudas_minor=0, morosidad=False)}
    )


def _make_engine():
    from sqlalchemy import create_engine

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("credits", "shared", "notifications", "audit"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[k] for k in TABLES])
    return engine


@pytest.fixture()
def session():
    """Sesion con catalogo demo y nonces limpios."""
    nonce_domain.reset_nonces()
    engine = _make_engine()
    current = Session(bind=engine, autoflush=False, expire_on_commit=False)
    repo.seed_demo_products(current)
    current.commit()
    try:
        yield current
    finally:
        nonce_domain.reset_nonces()
        current.close()
        engine.dispose()


def _product_id(current: Session) -> str:
    return str(catalog.get_product(current, code="PERSONAL_LIBRE")["id"])


def _approved_app_id(current: Session, user: uuid.UUID = USER) -> str:
    view = apps_service.submit_application(
        current,
        user,
        product_id=_product_id(current),
        requested_amount_minor=1_000_000,
        term_months=12,
        total_income_minor=2_000_000,
        total_debt_minor=100_000,
        bureau_authorized=True,
        bureau_provider=_bureau_for(user),
    )
    assert view["status"] == "APPROVED"
    current.commit()
    return view["id"]


def _generated_app_id(current: Session, user: uuid.UUID = USER) -> str:
    app_id = _approved_app_id(current, user)
    payload, reused = docs_service.generate_contracts(current, user, app_id)
    assert not reused and len(payload["documents"]) == 3
    current.commit()
    return app_id


def _hmac_signature(nonce: str) -> str:
    return hmac.new(bytes.fromhex(SECRET_HEX), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _active_binding_reader(current: Session, user_id: uuid.UUID, device_id: str) -> dict | None:
    assert user_id == USER
    assert device_id == DEVICE
    return {"device_id": DEVICE, "public_key": PUBLIC_KEY, "status": "ACTIVE"}


def _real_verify(nonce: str, signature: str, public_key: str) -> bool:
    from app.modules.identity.service import device_login as device_login_service

    return device_login_service.verify_signature(nonce, signature, public_key)


def _outbox_by_type(current: Session, event_type: str) -> list[dict]:
    table = Base.metadata.tables["shared.outbox"]
    current.expire_all()
    return [
        row[0]
        for row in current.execute(
            sa.select(table.c.payload).where(table.c.event_type == event_type)
        ).all()
    ]


def _audit_actions(current: Session) -> list[str]:
    table = Base.metadata.tables["audit.audit_log"]
    current.expire_all()
    return [row[0] for row in current.execute(sa.select(table.c.action)).all()]


# ------------------------------------------------ camino feliz (dispositivo)


def test_device_happy_path_signs_seals_and_emits(session):
    app_id = _generated_app_id(session)
    challenge = signing_service.request_signature_challenge(session, USER, app_id, device_id=DEVICE)
    assert challenge["application_id"] == app_id
    assert challenge["status"] == "GENERATED"
    assert challenge["expires_in"] == 120

    payload = signing_service.sign_contract(
        session,
        USER,
        app_id,
        device_id=DEVICE,
        signature=_hmac_signature(challenge["nonce"]),
        nonce=challenge["nonce"],
        binding_reader=_active_binding_reader,
        signature_verifier=_real_verify,
    )
    session.commit()
    assert payload["application_id"] == app_id
    assert payload["status"] == "SIGNED"
    assert payload["via"] == "device"
    assert len(payload["documents"]) == 3

    rows = contracts_repo.list_by_application(session, app_id)
    assert len(rows) == 3
    token_hashes = set()
    for row, doc in zip(rows, payload["documents"], strict=True):
        assert row.status == "SIGNED"
        assert row.signed_at is not None
        assert re.fullmatch(r"[0-9a-f]{64}", row.signature_token_hash or "")
        token_hashes.add(row.signature_token_hash)
        # CA-03: sello derivado reproducible (no repudio).
        assert signing_domain.verify_seal(
            doc_hash=row.doc_hash,
            signed_at_iso=payload["signed_at"],
            signature_token_hash=row.signature_token_hash or "",
            seal=doc["seal"],
        )
    # Un solo token para el juego (las 3 filas comparten la firma).
    assert len(token_hashes) == 1

    events = _outbox_by_type(session, "credit.contract.signed")
    assert len(events) == 1
    assert events[0]["application_id"] == app_id
    assert events[0]["user_id"] == str(USER)
    assert events[0]["status"] == "SIGNED"
    assert len(events[0]["contract_ids"]) == 3
    assert "signed_at" in events[0]
    assert "credit.contract.signed" in _audit_actions(session)


def test_pin_happy_path_signs_without_nonce(session):
    app_id = _generated_app_id(session)
    payload = signing_service.sign_contract_with_pin(
        session, USER, app_id, pin=PIN, pin_verifier=lambda s, u, p: p == PIN
    )
    session.commit()
    assert payload["status"] == "SIGNED"
    assert payload["via"] == "pin"
    rows = contracts_repo.list_by_application(session, app_id)
    assert all(row.status == "SIGNED" for row in rows)
    assert all(re.fullmatch(r"[0-9a-f]{64}", row.signature_token_hash or "") for row in rows)
    # El PIN en claro jamas queda en la BD (el token lleva sal aleatoria).
    stored = {row.signature_token_hash for row in rows}
    assert PIN not in stored and len(stored) == 1
    assert len(_outbox_by_type(session, "credit.contract.signed")) == 1


# ------------------------------------------------ sello puro


def test_seal_deterministic_sensitive_and_verifiable():
    token = signing_domain.token_hash_for(
        signature="sig", nonce="n", device_id="d", application_id="a"
    )
    first = signing_domain.seal_for(
        doc_hash="ab" * 32,
        signed_at_iso="2026-01-01T00:00:00+00:00",
        signature_token_hash=token,
    )
    again = signing_domain.seal_for(
        doc_hash="ab" * 32,
        signed_at_iso="2026-01-01T00:00:00+00:00",
        signature_token_hash=token,
    )
    assert first == again
    assert signing_domain.verify_seal(
        doc_hash="ab" * 32,
        signed_at_iso="2026-01-01T00:00:00+00:00",
        signature_token_hash=token,
        seal=first,
    )
    tampered = signing_domain.seal_for(
        doc_hash="ac" * 32,
        signed_at_iso="2026-01-01T00:00:00+00:00",
        signature_token_hash=token,
    )
    assert tampered != first
    assert not signing_domain.verify_seal(
        doc_hash="ac" * 32,
        signed_at_iso="2026-01-01T00:00:00+00:00",
        signature_token_hash=token,
        seal=first,
    )
    # El token liga la firma al expediente (otro expediente -> otro hash).
    other = signing_domain.token_hash_for(
        signature="sig", nonce="n", device_id="d", application_id="b"
    )
    assert other != token
    # Via PIN: dos firmas del mismo expediente no colisionan (sal aleatoria).
    pin_a = signing_domain.token_hash_for_pin(
        application_id="a",
        signed_at_iso="2026-01-01T00:00:00+00:00",
        salt_hex="cd" * 16,
    )
    pin_b = signing_domain.token_hash_for_pin(
        application_id="a",
        signed_at_iso="2026-01-01T00:00:00+00:00",
        salt_hex="ce" * 16,
    )
    assert pin_a != pin_b


# ------------------------------------------------ errores genericos (sin oraculo)


def _fresh_challenge(session: Session, app_id: str) -> dict:
    return signing_service.request_signature_challenge(session, USER, app_id, device_id=DEVICE)


def test_bad_signature_revoked_unknown_reused_and_pin_share_generic(session):
    app_id = _generated_app_id(session)
    generics: set[str] = set()

    def _capture(exc: Exception) -> None:
        assert getattr(exc, "code", None) == "NOT_AUTHORIZED"
        generics.add(str(exc))

    # 1. Firma invalida.
    challenge = _fresh_challenge(session, app_id)
    with pytest.raises(signing_service.ContractNotAuthorizedError) as exc:
        signing_service.sign_contract(
            session,
            USER,
            app_id,
            device_id=DEVICE,
            signature="00" * 32,
            nonce=challenge["nonce"],
            binding_reader=_active_binding_reader,
            signature_verifier=_real_verify,
        )
    _capture(exc.value)

    # 2. Binding revocado.
    challenge = _fresh_challenge(session, app_id)

    def revoked(s, u, d):
        return {"device_id": d, "public_key": PUBLIC_KEY, "status": "REVOKED"}

    with pytest.raises(signing_service.ContractNotAuthorizedError) as exc:
        signing_service.sign_contract(
            session,
            USER,
            app_id,
            device_id=DEVICE,
            signature=_hmac_signature(challenge["nonce"]),
            nonce=challenge["nonce"],
            binding_reader=revoked,
            signature_verifier=_real_verify,
        )
    _capture(exc.value)

    # 3. Dispositivo desconocido (sin binding).
    challenge = _fresh_challenge(session, app_id)
    with pytest.raises(signing_service.ContractNotAuthorizedError) as exc:
        signing_service.sign_contract(
            session,
            USER,
            app_id,
            device_id="dev-desconocido",
            signature=_hmac_signature(challenge["nonce"]),
            nonce=challenge["nonce"],
            binding_reader=lambda s, u, d: None,
            signature_verifier=_real_verify,
        )
    _capture(exc.value)

    # 4. PIN erroneo: identico generico (sin oraculo entre vias).
    with pytest.raises(signing_service.ContractNotAuthorizedError) as exc:
        signing_service.sign_contract_with_pin(
            session, USER, app_id, pin="0000", pin_verifier=lambda s, u, p: False
        )
    _capture(exc.value)

    assert generics == {signing_service.NOT_AUTHORIZED_MESSAGE}
    # Nada cambio: juego intacto y sin evento de firma.
    rows = contracts_repo.list_by_application(session, app_id)
    assert all(row.status == "GENERATED" for row in rows)
    assert all(row.signature_token_hash is None for row in rows)
    assert _outbox_by_type(session, "credit.contract.signed") == []


def test_expired_nonce_maps_to_expired(session):
    app_id = _generated_app_id(session)
    moment = datetime.now(UTC)
    challenge = signing_service.request_signature_challenge(
        session, USER, app_id, device_id=DEVICE, now=moment
    )
    with pytest.raises(signing_service.ContractNonceExpiredError) as exc:
        signing_service.sign_contract(
            session,
            USER,
            app_id,
            device_id=DEVICE,
            signature=_hmac_signature(challenge["nonce"]),
            nonce=challenge["nonce"],
            now=moment + timedelta(seconds=300),
            binding_reader=_active_binding_reader,
            signature_verifier=_real_verify,
        )
    assert exc.value.code == "EXPIRED_NONCE"
    assert all(
        row.status == "GENERATED" for row in contracts_repo.list_by_application(session, app_id)
    )


def test_reused_nonce_rejected_and_second_set_untouched(session):
    app_a = _generated_app_id(session)
    challenge = _fresh_challenge(session, app_a)
    payload = signing_service.sign_contract(
        session,
        USER,
        app_a,
        device_id=DEVICE,
        signature=_hmac_signature(challenge["nonce"]),
        nonce=challenge["nonce"],
        binding_reader=_active_binding_reader,
        signature_verifier=_real_verify,
    )
    assert payload["status"] == "SIGNED"
    session.commit()

    app_b = _generated_app_id(session)
    with pytest.raises(signing_service.ContractNotAuthorizedError) as exc:
        signing_service.sign_contract(
            session,
            USER,
            app_b,
            device_id=DEVICE,
            signature=_hmac_signature(challenge["nonce"]),
            nonce=challenge["nonce"],
            binding_reader=_active_binding_reader,
            signature_verifier=_real_verify,
        )
    assert str(exc.value) == signing_service.NOT_AUTHORIZED_MESSAGE
    assert all(
        row.status == "GENERATED" for row in contracts_repo.list_by_application(session, app_b)
    )
    assert len(_outbox_by_type(session, "credit.contract.signed")) == 1


def test_token_reuse_rejected(session, monkeypatch):
    app_a = _generated_app_id(session)
    forced = "ff" * 32
    monkeypatch.setattr(signing_domain, "token_hash_for", lambda **kwargs: forced)
    challenge = _fresh_challenge(session, app_a)
    payload = signing_service.sign_contract(
        session,
        USER,
        app_a,
        device_id=DEVICE,
        signature="cualquiera",
        nonce=challenge["nonce"],
        binding_reader=_active_binding_reader,
        signature_verifier=lambda n, s, k: True,
    )
    assert payload["status"] == "SIGNED"
    session.commit()

    app_b = _generated_app_id(session)
    challenge_b = signing_service.request_signature_challenge(
        session, USER, app_b, device_id=DEVICE
    )
    with pytest.raises(contracts_repo.ContractConflictError):
        signing_service.sign_contract(
            session,
            USER,
            app_b,
            device_id=DEVICE,
            signature="otra",
            nonce=challenge_b["nonce"],
            binding_reader=_active_binding_reader,
            signature_verifier=lambda n, s, k: True,
        )
    assert all(
        row.status == "GENERATED" for row in contracts_repo.list_by_application(session, app_b)
    )


def test_already_signed_conflicts_both_paths(session):
    app_id = _generated_app_id(session)
    payload = signing_service.sign_contract_with_pin(
        session, USER, app_id, pin=PIN, pin_verifier=lambda s, u, p: True
    )
    assert payload["status"] == "SIGNED"
    session.commit()
    with pytest.raises(contracts_repo.ContractConflictError):
        signing_service.sign_contract_with_pin(
            session, USER, app_id, pin=PIN, pin_verifier=lambda s, u, p: True
        )
    with pytest.raises(contracts_repo.ContractConflictError):
        signing_service.sign_contract(
            session,
            USER,
            app_id,
            device_id=DEVICE,
            signature="x",
            nonce="y",
            binding_reader=_active_binding_reader,
            signature_verifier=_real_verify,
        )
    assert len(_outbox_by_type(session, "credit.contract.signed")) == 1


def test_challenge_on_signed_set_conflicts(session):
    app_id = _generated_app_id(session)
    signing_service.sign_contract_with_pin(
        session, USER, app_id, pin=PIN, pin_verifier=lambda s, u, p: True
    )
    session.commit()
    with pytest.raises(contracts_repo.ContractConflictError):
        signing_service.request_signature_challenge(session, USER, app_id, device_id=DEVICE)


def test_foreign_missing_or_empty_set_neutral_404(session):
    foreign_id = _generated_app_id(session, OTHER)
    for target in (foreign_id, str(uuid.uuid4())):
        with pytest.raises(contracts_repo.ContractNotFoundError):
            signing_service.sign_contract_with_pin(
                session, USER, target, pin=PIN, pin_verifier=lambda s, u, p: True
            )
        with pytest.raises(contracts_repo.ContractNotFoundError):
            signing_service.request_signature_challenge(session, USER, target)
    # Expediente aprobado pero sin juego generado: no firmable.
    bare_id = _approved_app_id(session)
    with pytest.raises(contracts_repo.ContractNotFoundError):
        signing_service.sign_contract_with_pin(
            session, USER, bare_id, pin=PIN, pin_verifier=lambda s, u, p: True
        )
    with pytest.raises(contracts_repo.ContractNotFoundError):
        signing_service.request_signature_challenge(session, USER, bare_id)


def test_challenge_bad_device_id_422(session):
    app_id = _generated_app_id(session)
    with pytest.raises(contracts_repo.ContractValidationError):
        signing_service.request_signature_challenge(session, USER, app_id, device_id="")


# ------------------------------------------------ frontera D-03 (fachadas reales)


def test_default_facades_delegate_to_identity(session, monkeypatch):
    from app.modules.identity.service import device_login as device_login_service
    from app.modules.identity.service import pin_authorization as pin_authorization_service

    app_id = _generated_app_id(session)
    challenge = signing_service.request_signature_challenge(session, USER, app_id, device_id=DEVICE)
    calls: dict[str, object] = {}

    def _fake_binding(s, user_id, device_id):
        calls["binding"] = (user_id, device_id)
        return {"device_id": device_id, "public_key": PUBLIC_KEY, "status": "ACTIVE"}

    def _fake_verify(nonce, signature, public_key):
        calls["verify"] = (nonce, public_key)
        return True

    monkeypatch.setattr(device_login_service, "get_device_binding", _fake_binding)
    monkeypatch.setattr(device_login_service, "verify_signature", _fake_verify)
    payload = signing_service.sign_contract(
        session,
        USER,
        app_id,
        device_id=DEVICE,
        signature=_hmac_signature(challenge["nonce"]),
        nonce=challenge["nonce"],
    )
    assert payload["status"] == "SIGNED"
    assert calls["binding"] == (USER, DEVICE)
    assert calls["verify"] == (challenge["nonce"], PUBLIC_KEY)
    session.commit()

    app_pin = _generated_app_id(session)

    def _fake_pin(s, user_id, pin):
        calls["pin"] = (user_id, pin)
        return True

    monkeypatch.setattr(pin_authorization_service, "verify_holder_pin", _fake_pin)
    payload = signing_service.sign_contract_with_pin(session, USER, app_pin, pin=PIN)
    assert payload["status"] == "SIGNED"
    assert calls["pin"] == (USER, PIN)


def test_default_verifiers_without_identity_tables(session):
    # Sin tablas de `identity`: la firma HMAC real verifica (pura) y el
    # PIN cae a la rama ciega (`False`, sin enumeracion ni excepcion).
    assert signing_service.default_signature_verifier("n", _hmac_signature("n"), PUBLIC_KEY)
    assert not signing_service.default_signature_verifier("n", "00" * 32, PUBLIC_KEY)
    assert signing_service.default_pin_verifier(session, USER, PIN) is False


def test_credits_signing_imports_no_foreign_models():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "modules" / "credits"
    checked = 0
    for rel in ("domain/signing.py", "service/signing.py", "repository/contracts.py"):
        imports = [
            line.strip()
            for line in (root / rel).read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        checked += len(imports)
        for line in imports:
            assert "identity.models" not in line, (rel, line)
            assert "identity.repository" not in line, (rel, line)
            assert "identity import repository" not in line, (rel, line)
            assert "transactions" not in line, (rel, line)
            assert "ledger" not in line, (rel, line)
            assert "kyc" not in line, (rel, line)
    assert checked > 0


# ------------------------------------------------ logs y aislamiento


def test_no_secrets_or_pii_in_logs(session, caplog):
    app_id = _generated_app_id(session)
    challenge = _fresh_challenge(session, app_id)
    signature = _hmac_signature(challenge["nonce"])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.modules.credits"):
        signing_service.sign_contract(
            session,
            USER,
            app_id,
            device_id=DEVICE,
            signature=signature,
            nonce=challenge["nonce"],
            binding_reader=_active_binding_reader,
            signature_verifier=_real_verify,
        )
        app_pin = _generated_app_id(session)
        signing_service.sign_contract_with_pin(
            session, USER, app_pin, pin=PIN, pin_verifier=lambda s, u, p: True
        )
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert signature not in blob
    assert challenge["nonce"] not in blob
    assert PIN not in blob
    assert SECRET_HEX not in blob


def test_signing_creates_no_loans_nor_money(session):
    app_id = _generated_app_id(session)
    signing_service.sign_contract_with_pin(
        session, USER, app_id, pin=PIN, pin_verifier=lambda s, u, p: True
    )
    session.commit()
    session.expire_all()
    names = {
        row[0]
        for row in session.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    }
    for forbidden in (
        "loans",
        "loan_schedules",
        "loan_payments",
        "transactions",
        "postings",
        "journal_entries",
        "holds",
    ):
        assert forbidden not in names
    outbox = Base.metadata.tables["shared.outbox"]
    types = {row[0] for row in session.execute(sa.select(outbox.c.event_type)).all()}
    assert types <= {
        "credit.application.submitted",
        "credit.approved",
        "credit.contract.generated",
        "credit.contract.signed",
    }
