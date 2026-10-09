"""Numero de cuenta de 14 digitos con digito verificador + backfill (E2-T28, HU05/HU06).

- Parte A (sin BD): DV modulo 11 (vectores de referencia verificados a mano),
  `verify_account_number` (validos e invalidos) y generacion (`_new_account_number`).
- Parte B (SQLite en memoria + schemas ATTACH): unicidad y reintento acotado
  ante colision (`_new_unique_account_number` con colision forzada por
  monkeypatch); alta `onboard_customer` genera numero valido y unico.
- Parte C (sin BD): `normalize_account_or_cci` acepta 14 digitos con DV valido
  o CCI de 20 digitos y rechaza el resto (se retira el rango laxo `8..20`).
- Parte D (migracion `0024`, sin BD salvo SQLite aislado): `plan_backfill` puro
  (determinista, unico, idempotente), `backfill_data` sobre SQLite (up),
  `downgrade` no-op documentado, cadena de revisiones y `head` unico.
- Parte E (Postgres `banca_test`): backfill real up/down y alta con numero
  valido; se omite sin BD.
- Parte F (regresion): enmascarado `****dddd` con numeros nuevos.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_DIR / "migrations" / "versions" / "0024_accounts_numeric_backfill.py"

FULL_RE = re.compile(r"^001[0-9]{11}$")

#: Vectores de referencia (calculados a mano con el algoritmo del brief):
#: cuerpo `0000000001` -> suma 8 -> DV 3; cuerpo `0000000003` -> suma 12 ->
#: resto 1 -> DV 10 -> 0; cuerpo `0000000008` -> suma 22 -> resto 0 ->
#: DV 11 -> 0.
REF_BODY_1 = "00100000000013"
REF_BODY_3 = "00100000000030"
REF_BODY_8 = "00100000000080"
HEX_LEGACY = "001-01a7124e8ebe"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0024_backfill", str(MIGRATION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- Parte A: DV puro


def test_check_digit_reference_vectors():
    from app.modules.identity.service import compute_check_digit

    assert compute_check_digit("0010000000001") == 3
    assert compute_check_digit("0010000000003") == 0  # resto 1 -> dv 10 -> 0
    assert compute_check_digit("0010000000008") == 0  # resto 0 -> dv 11 -> 0


def test_check_digit_rejects_bad_input():
    from app.modules.identity.service import compute_check_digit

    for bad in ("", "001000000001", "00100000000013", "001000000000a", None, 13 * "1" + " "):
        with pytest.raises(ValueError):
            compute_check_digit(bad)


def test_verify_accepts_valid_numbers():
    from app.modules.identity.service import verify_account_number

    assert verify_account_number(REF_BODY_1) is True
    assert verify_account_number(REF_BODY_3) is True
    assert verify_account_number(REF_BODY_8) is True


def test_verify_rejects_invalid_numbers():
    from app.modules.identity.service import verify_account_number

    assert verify_account_number("00100000000014") is False  # DV incorrecto
    assert verify_account_number("00200000000013") is False  # sin prefijo 001
    assert verify_account_number("001000000001") is False  # corto (12)
    assert verify_account_number("001000000000133") is False  # largo (15)
    assert verify_account_number(HEX_LEGACY) is False  # hex heredado
    assert verify_account_number("001-000000000013") is False  # con guion
    assert verify_account_number("") is False
    assert verify_account_number(None) is False
    assert verify_account_number(123) is False
    assert verify_account_number(" 00100000000013 ") is False  # sin normalizar


def test_new_account_number_format_and_check_digit():
    from app.modules.identity.service import _new_account_number, verify_account_number

    for _ in range(500):
        number = _new_account_number()
        assert FULL_RE.fullmatch(number) is not None, number
        assert verify_account_number(number) is True


def test_new_account_number_zero_padded_body(monkeypatch):
    import app.modules.identity.service as svc

    monkeypatch.setattr(svc.secrets, "randbelow", lambda _n: 1)
    assert svc._new_account_number() == REF_BODY_1


def test_new_account_number_uniqueness():
    from app.modules.identity.service import _new_account_number

    batch = {_new_account_number() for _ in range(500)}
    assert len(batch) == 500


# ------------------------------------------------- Parte B: unicidad y alta (SQLite)


@pytest.fixture()
def accounts_session():
    """Sesion SQLite aislada con solo `accounts.accounts` (colision/UQ)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401 (registro de tablas)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS accounts")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def onboard_session():
    """Sesion SQLite aislada (patron `test_identity_onboarding`, Parte B)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401
    import app.modules.identity.models as _i  # noqa: F401
    import app.modules.ledger.models as _l  # noqa: F401
    import app.modules.notifications.models as _n  # noqa: F401
    import app.modules.shared.models as _s  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "accounts", "ledger", "shared", "notifications"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["identity.otp_codes"],
            Base.metadata.tables["accounts.accounts"],
            Base.metadata.tables["accounts.account_balances"],
            Base.metadata.tables["ledger.ledger_accounts"],
            Base.metadata.tables["shared.outbox"],
            Base.metadata.tables["notifications.notifications"],
            Base.metadata.tables["notifications.notification_templates"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _new_account_row(session: Session, number: str):
    from app.modules.accounts.models import Account

    row = Account(user_id=uuid.uuid4(), account_number=number, type="AHORRO", currency="PEN")
    session.add(row)
    session.flush()
    return row


def test_unique_generation_retries_on_collision(accounts_session: Session, monkeypatch):
    import app.modules.identity.service as svc

    taken = _new_account_row(accounts_session, REF_BODY_1).account_number
    calls = iter([taken, REF_BODY_3])
    monkeypatch.setattr(svc, "_new_account_number", lambda: next(calls))
    assert svc._new_unique_account_number(accounts_session) == REF_BODY_3


def test_unique_generation_exhaustion_fails_controlled(accounts_session: Session, monkeypatch):
    import app.modules.identity.service as svc

    taken = _new_account_row(accounts_session, REF_BODY_1).account_number
    monkeypatch.setattr(svc, "_new_account_number", lambda: taken)
    monkeypatch.setattr(svc, "ACCOUNT_NUMBER_MAX_ATTEMPTS", 3)
    with pytest.raises(RuntimeError, match="numero de cuenta unico"):
        svc._new_unique_account_number(accounts_session)


def _customer_kwargs(doc_hash: str, **overrides) -> dict:
    params = {
        "kyc_result": {"overall_result": True, "doc_number_hash": doc_hash},
        "first_name": "Ana",
        "last_name": "Quispe",
        "doc_type": "DNI",
        "doc_number_masked": "****5678",
        "email": f"ana.{uuid.uuid4().hex[:8]}@example.com",
        "phone": "999888777",
    }
    params.update(overrides)
    return params


def test_onboard_generates_valid_unique_numbers(onboard_session: Session):
    from app.modules.accounts import repository as accounts_repo
    from app.modules.identity.service import onboard_customer, verify_account_number

    first = onboard_customer(onboard_session, **_customer_kwargs(f"hash-{uuid.uuid4().hex}"))
    second = onboard_customer(onboard_session, **_customer_kwargs(f"hash-{uuid.uuid4().hex}"))
    numbers = set()
    for result in (first, second):
        account = accounts_repo.get_account(onboard_session, result["account_id"])
        assert account is not None
        assert FULL_RE.fullmatch(account.account_number) is not None
        assert verify_account_number(account.account_number) is True
        numbers.add(account.account_number)
    assert len(numbers) == 2


def test_onboard_retries_when_generated_number_exists(onboard_session: Session, monkeypatch):
    import app.modules.identity.service as svc
    from app.modules.accounts import repository as accounts_repo

    taken = _new_account_row(onboard_session, REF_BODY_1).account_number
    calls = iter([taken, REF_BODY_3])
    monkeypatch.setattr(svc, "_new_account_number", lambda: next(calls))
    result = svc.onboard_customer(onboard_session, **_customer_kwargs(f"hash-{uuid.uuid4().hex}"))
    account = accounts_repo.get_account(onboard_session, result["account_id"])
    assert account is not None and account.account_number == REF_BODY_3


def test_onboard_exhaustion_creates_nothing(onboard_session: Session, monkeypatch):
    import app.modules.identity.service as svc
    from app.modules.identity import repository as identity_repo

    taken = _new_account_row(onboard_session, REF_BODY_1).account_number
    monkeypatch.setattr(svc, "_new_account_number", lambda: taken)
    monkeypatch.setattr(svc, "ACCOUNT_NUMBER_MAX_ATTEMPTS", 2)
    doc_hash = f"hash-{uuid.uuid4().hex}"
    with pytest.raises(RuntimeError, match="numero de cuenta unico"):
        svc.onboard_customer(onboard_session, **_customer_kwargs(doc_hash))
    assert identity_repo.get_by_doc_hash(onboard_session, doc_hash) is None


# ------------------------------------------------- Parte C: normalize alineado


def test_normalize_accepts_valid_14_and_20():
    from app.modules.accounts.service import beneficiaries as beneficiaries_service
    from app.modules.identity.service import _new_account_number

    fresh = _new_account_number()
    assert beneficiaries_service.normalize_account_or_cci(fresh) == fresh
    assert beneficiaries_service.normalize_account_or_cci(REF_BODY_1) == REF_BODY_1
    assert (
        beneficiaries_service.normalize_account_or_cci("00110001111111111111")
        == "00110001111111111111"
    )
    # Con espacios/guiones se normaliza igual que antes.
    assert (
        beneficiaries_service.normalize_account_or_cci(" 0011-0001 1111-1111 1111 ")
        == "00110001111111111111"
    )


def test_normalize_rejects_lax_range_and_bad_dv():
    from app.modules.accounts.service import beneficiaries as beneficiaries_service

    for bad in (
        "00100000000014",  # 14 digitos con DV incorrecto
        "00200000000013",  # 14 digitos sin prefijo 001
        "12345678",  # 8 digitos (rango laxo retirado)
        "00110001111",  # 11 digitos (rango laxo retirado)
        "1234567890123456789",  # 19 digitos (rango laxo retirado)
        "1234567",  # 7 digitos
        "1" * 21,  # 21 digitos
        "ABC123",
        "",
    ):
        with pytest.raises(ValueError):
            beneficiaries_service.normalize_account_or_cci(bad)
    with pytest.raises(TypeError):
        beneficiaries_service.normalize_account_or_cci(123)


def test_beneficiary_points_to_14_digit_account(accounts_session: Session):
    """Un beneficiario puede apuntar a una cuenta CuyCash de 14 digitos (CA-05)."""

    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401 (registro de tablas)
    import app.modules.shared.models as _s  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("accounts", "shared"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["accounts.beneficiaries"],
            Base.metadata.tables["accounts.beneficiary_search_tokens"],
            Base.metadata.tables["shared.outbox"],
        ],
    )
    session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        from app.modules.accounts.service import beneficiaries as beneficiaries_service

        view = beneficiaries_service.create_beneficiary(
            session, uuid.uuid4(), alias="Cuenta Cuy", account_or_cci=REF_BODY_1
        )
        assert view["account_masked"] == f"****{REF_BODY_1[-4:]}"
    finally:
        session.close()
        engine.dispose()


# ------------------------------------------------- Parte D: migracion 0024


def test_migration_file_chain():
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0024_accounts_numeric_backfill"' in content
    assert 'down_revision = "0023_risk_profiles_params"' in content
    assert "def upgrade" in content and "def downgrade" in content
    assert len("0024_accounts_numeric_backfill") <= 32  # limite de alembic_version


def test_single_head_is_0028():
    # E2-T37: la cabeza avanza con la migracion 0028_backfill_entry_type
    # (backfill de accounts.movements_view.entry_type; sin reescribir 0027).
    from tests.db_utils import get_head_revisions

    assert get_head_revisions() == ("0028_backfill_entry_type",)


def test_plan_backfill_deterministic_unique_and_idempotent():
    mig = _load_migration()

    existing = [
        ("id-hex", HEX_LEGACY),
        ("id-ok", REF_BODY_3),
        ("id-short", "12345678"),
    ]
    first = mig.plan_backfill(existing)
    second = mig.plan_backfill(existing)
    assert first == second  # determinista
    assert set(first) == {"id-hex", "id-short"}  # la conforme se conserva
    assert len(set(first.values())) == 2  # unicos
    for new in first.values():
        assert FULL_RE.fullmatch(new) is not None
        assert mig.is_conforming(new)
    # Secuencial desde el cuerpo 1 (los cuerpos 1 y 2 estaban libres).
    assert first["id-hex"] == "00100000000013"
    assert first["id-short"] == "00100000000021"
    # Idempotente: tras aplicar, ya no hay nada que reasignar.
    applied = [(row_id, first.get(row_id, number)) for row_id, number in existing]
    assert mig.plan_backfill(applied) == {}


def test_plan_backfill_skips_occupied_bodies():
    mig = _load_migration()

    existing = [("id-ok", "00100000000013"), ("id-hex", HEX_LEGACY)]
    plan = mig.plan_backfill(existing)
    assert set(plan) == {"id-hex"}
    assert plan["id-hex"] != "00100000000013"
    assert mig.is_conforming(plan["id-hex"])


def test_backfill_data_on_sqlite_and_downgrade_noop():
    mig = _load_migration()
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.accounts.models as _a  # noqa: F401 (registro de tablas)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("ATTACH DATABASE ':memory:' AS accounts")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["accounts.accounts"]])
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO accounts.accounts "
                    "(id, user_id, account_number, type, currency, status) "
                    "VALUES (:id, :uid, :num, 'AHORRO', 'PEN', 'ACTIVE')"
                ),
                [
                    {"id": uuid.uuid4().hex, "uid": uuid.uuid4().hex, "num": HEX_LEGACY},
                    {"id": uuid.uuid4().hex, "uid": uuid.uuid4().hex, "num": REF_BODY_1},
                    {"id": uuid.uuid4().hex, "uid": uuid.uuid4().hex, "num": "12345678"},
                ],
            )
            assert mig.backfill_data(conn) == 2
            numbers = [
                row[0]
                for row in conn.execute(sa.text("SELECT account_number FROM accounts.accounts"))
            ]
            assert len(numbers) == 3 and len(set(numbers)) == 3
            assert all(mig.is_conforming(number) for number in numbers)
            assert HEX_LEGACY not in numbers and "12345678" not in numbers
            assert REF_BODY_1 in numbers  # la conforme se conserva
            assert mig.backfill_data(conn) == 0  # re-ejecucion sin cambios
        assert mig.downgrade() is None  # no-op documentado, no rompe
    finally:
        engine.dispose()


# ------------------------------------------------- Parte E: Postgres


def test_pg_backfill_up_down_on_test_database(test_engine):
    """Hex real -> upgrade head -> 14 digitos con DV; downgrade no rompe (CA-03)."""
    from sqlalchemy.orm import Session as SASession

    from tests.db_utils import (
        downgrade_in_process,
        resolve_test_database_url,
        run_migrations_in_process,
    )

    url = resolve_test_database_url()
    from app.modules.accounts.models import Account

    marker = f"hex-{uuid.uuid4().hex[:8]}"
    account_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session = SASession(bind=test_engine, autoflush=False, expire_on_commit=False)
    try:
        session.add(
            Account(
                id=account_id,
                user_id=user_id,
                account_number=f"001-{marker}",
                type="AHORRO",
                currency="PEN",
            )
        )
        session.commit()
        try:
            downgrade_in_process(url, "0023_risk_profiles_params")
            row = session.get(Account, account_id)
            assert row is not None and row.account_number == f"001-{marker}"
            run_migrations_in_process(url, "head")
            session.expire_all()
            row = session.get(Account, account_id)
            assert row is not None
            assert FULL_RE.fullmatch(row.account_number) is not None
            from app.modules.identity.service import verify_account_number

            assert verify_account_number(row.account_number) is True
            assert (row.user_id, row.type, row.currency) == (user_id, "AHORRO", "PEN")
        finally:
            run_migrations_in_process(url, "head")
    finally:
        session.delete(session.get(Account, account_id))
        session.commit()
        session.close()


def test_pg_onboard_generates_valid_number(db_session: Session):
    """Alta real genera numero de 14 digitos con DV valido (CA-01)."""
    from app.modules.accounts import repository as accounts_repo
    from app.modules.identity.service import onboard_customer, verify_account_number

    result = onboard_customer(db_session, **_customer_kwargs(f"hash-{uuid.uuid4().hex}"))
    account = accounts_repo.get_account(db_session, result["account_id"])
    assert account is not None
    assert FULL_RE.fullmatch(account.account_number) is not None
    assert verify_account_number(account.account_number) is True


# ------------------------------------------------- Parte F: regresion de enmascarado


def test_masked_view_with_new_format():
    """`account_number_masked` (`****dddd`) intacto con numeros nuevos (CA-05)."""
    from app.modules.accounts.service import to_summary

    summary = to_summary(
        uuid.uuid4(),
        account_number=REF_BODY_1,
        type="AHORRO",
        currency="PEN",
        available_minor=100,
        held_minor=0,
    )
    assert summary.account_number_masked == f"****{REF_BODY_1[-4:]}"
    assert summary.account_number_masked[4:].isdigit()
