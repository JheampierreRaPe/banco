"""Fijado inicial del PIN con OTP de activacion (addendum E1-T14/E1-T28, HU02/HU03).

- Sin Postgres: `TestClient(app)` + override de `get_db` a SQLite en memoria
  con schemas ATTACH (`identity`/`shared`), patron de `test_pin_login.py`
  (TestClient) + `test_activation.py` (SQLite ATTACH).
- Hueco que cierra: `onboard_customer` crea la credencial SIN `pin_hash` y
  no existia endpoint para fijarlo -> `POST /auth/login/pin` jamas funcionaba
  para usuarios de la app. `POST /auth/pin/setup {user_ref, code, pin}`
  consume un OTP `ACTIVATION` valido, fija `pin_hash` y deja la cuenta
  `ACTIVE` una sola vez (un solo OTP, E1-T28).
- Casos: setup OK + activacion + login con PIN funciona despues; codigo
  invalido -> `INVALID_SETUP_CODE` sin cambios de estado; codigo reutilizado
  -> `INVALID_SETUP_CODE`; credencial ya con PIN -> `PIN_ALREADY_SET` (409);
  PIN debil -> 422; usuario inexistente == codigo invalido (mismo cuerpo, no
  filtra); `user.activated` enlistado UNA sola vez; atomicidad ante fallo
  posterior; OpenAPI expone la ruta; reglas estaticas (servicio sin
  `commit`/`float`, reutiliza `validate_otp`+`hash_pin`, sin PIN en logs).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app

SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "modules"
    / "identity"
    / "service"
    / "pin_setup.py"
)
API_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "modules" / "identity" / "api" / "pin_setup.py"
)

PIN = "4829"


@pytest.fixture()
def setup_session():
    """Sesion SQLite aislada (`identity`/`shared`/`notifications` via ATTACH)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.identity.models as _i  # noqa: F401 (registro)
    import app.modules.notifications.models as _n  # noqa: F401 (registro)
    import app.modules.shared.models as _s  # noqa: F401 (registro)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    def _attach(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        for schema in ("identity", "shared", "notifications"):
            cur.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
        cur.close()

    event.listen(engine, "connect", _attach)
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["identity.users"],
            Base.metadata.tables["identity.credentials"],
            Base.metadata.tables["identity.otp_codes"],
            Base.metadata.tables["identity.sessions"],
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


@pytest.fixture()
def setup_client(setup_session: Session):
    """TestClient con `get_db` a SQLite."""

    def _override():
        yield setup_session

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _make_setup_user(
    setup_session: Session, *, with_pin: bool = False, status: str = "PENDING_ACTIVATION"
):
    """Usuario + credencial sin PIN (+ OTP vigente).

    Retorna `(user, plain)`. Con `with_pin=True` la credencial ya trae PIN
    (caso 409); `status` permite probar el rechazo de estados no activables
    (`BLOCKED`/`CLOSED`/otro) introducido por E1-T28.
    """
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.service import otp_service
    from app.modules.identity.service import pin_login as pin_login_service

    suffix = uuid.uuid4().hex[:8]
    user = identity_repo.create_user(
        setup_session,
        doc_type="DNI",
        doc_number_hash="hash-" + uuid.uuid4().hex,
        first_name="Ada",
        last_name="Lovelace",
        email=f"ada.{suffix}@example.com",
        phone="+51999888777",
        status=status,
    )
    pin_hash = pin_login_service.hash_pin("9999") if with_pin else None
    identity_repo.create_credential(setup_session, user.id, pin_hash=pin_hash)
    _, plain = otp_service.generate_otp(
        setup_session, user_id=user.id, purpose="ACTIVATION", destination="+51999888777"
    )
    setup_session.commit()
    return user, plain


def _pin_hash(setup_session: Session, user_id: uuid.UUID):
    from app.modules.identity import repository as identity_repo

    setup_session.expire_all()
    row = identity_repo.get_credential(setup_session, user_id)
    assert row is not None
    return row.pin_hash


def _user_status(setup_session: Session, user_id: uuid.UUID) -> str:
    from app.modules.identity import repository as identity_repo

    setup_session.expire_all()
    return identity_repo.get_user(setup_session, user_id).status


def _outbox_activated(setup_session: Session, user_id: uuid.UUID) -> list:
    from app.modules.shared.models import OutboxEntry

    stmt = sa.select(OutboxEntry).where(
        OutboxEntry.event_type == "user.activated",
        OutboxEntry.aggregate_id == user_id,
    )
    return list(setup_session.scalars(stmt).all())


def _otp_status(setup_session: Session, user_id: uuid.UUID) -> str:
    from app.modules.identity.models import OtpCode

    stmt = sa.select(OtpCode.status).where(OtpCode.user_id == user_id)
    return setup_session.scalars(stmt).first()


# ---------------------------------------------------------------- Reglas estaticas
def test_static_rules_setup_consumes_otp_hashes_pin_no_commit():
    service = SERVICE_PATH.read_text(encoding="utf-8")
    assert "validate_otp" in service, "el setup exige OTP via validate_otp (lo consume)"
    assert "ACTIVATION" in service, "proposito ACTIVATION"
    assert "hash_pin" in service, "reutiliza hash_pin (sin cripto nueva)"
    assert "PIN_ALREADY_SET" in service or "PinAlreadySetError" in service
    assert 'user.status = "ACTIVE"' in service, "un solo OTP: activa en la misma transaccion"
    assert ".commit(" not in service, "el servicio hace flush; el endpoint confirma"
    assert "float(" not in service, "sin float"
    top_imports = "\n".join(
        line for line in service.splitlines() if line.startswith(("from app.", "import app."))
    )
    assert "onboard_customer" not in top_imports
    assert "outbox" not in top_imports, "el evento lo enlista validate_otp; no se re-emite"
    assert "def login_with_pin" not in service, "no redefine el login"
    assert ".login_with_pin(" not in service, "no llama ni reimplementa el login"
    log_lines = [line for line in service.splitlines() if "logger." in line]
    assert log_lines, "el servicio debe loguear sin PII"
    for line in log_lines:
        lowered = line.lower().replace("pin_setup", "").replace("pin_login", "")
        assert "pin" not in lowered, f"el PIN podria salir en logs: {line.strip()}"

    api = API_PATH.read_text(encoding="utf-8")
    assert "INVALID_SETUP_CODE" in api and "PIN_ALREADY_SET" in api
    assert ".commit(" in api, "el endpoint confirma exito y 409 (el consumo del OTP persiste)"
    assert "INVALID_PIN_FORMAT" in api, "PIN debil a nivel servicio tambien es 422"


# ---------------------------------------------------------------- Camino feliz
def test_setup_ok_then_pin_login_works(setup_client: TestClient, setup_session: Session):
    user, plain = _make_setup_user(setup_session)
    assert _pin_hash(setup_session, user.id) is None

    resp = setup_client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": str(user.id), "code": plain, "pin": PIN},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"}
    assert body["data"] == {"user_id": str(user.id), "status": "ACTIVE"}
    assert body["meta"]["request_id"]
    assert PIN not in resp.text and plain not in resp.text, "ni PIN ni OTP se reflejan"

    assert _user_status(setup_session, user.id) == "ACTIVE", "un solo OTP activa la cuenta"
    assert _otp_status(setup_session, user.id) == "USED", "el OTP quedo consumido"

    events = _outbox_activated(setup_session, user.id)
    assert len(events) == 1, "validate_otp enlista 1x user.activated; setup no duplica"
    assert events[0].payload["user_id"] == str(user.id)

    stored = _pin_hash(setup_session, user.id)
    assert stored is not None and stored != PIN and PIN not in stored

    login = setup_client.post("/api/v1/auth/login/pin", json={"user_ref": str(user.id), "pin": PIN})
    assert login.status_code == 200, login.text
    assert set(login.json()["data"]) == {
        "access_token",
        "refresh_token",
        "token_type",
        "session_id",
        "expires_in",
    }


# ---------------------------------------------------------------- Errores
def test_setup_invalid_code_401(setup_client: TestClient, setup_session: Session):
    user, _plain = _make_setup_user(setup_session)

    resp = setup_client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": str(user.id), "code": "000000", "pin": PIN},
    )
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["error"]["code"] == "INVALID_SETUP_CODE"
    assert _pin_hash(setup_session, user.id) is None, "fallo no fija PIN"
    assert _user_status(setup_session, user.id) == "PENDING_ACTIVATION", "fallo no activa"
    assert _otp_status(setup_session, user.id) == "PENDING", "fallo no consume el OTP"


def test_setup_reused_code_401(setup_client: TestClient, setup_session: Session):
    user, plain = _make_setup_user(setup_session)

    first = setup_client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": str(user.id), "code": plain, "pin": PIN},
    )
    assert first.status_code == 200, first.text

    second = setup_client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": str(user.id), "code": plain, "pin": "1234"},
    )
    assert second.status_code == 401, second.text
    assert second.json()["error"]["code"] == "INVALID_SETUP_CODE"


@pytest.mark.parametrize("blocked_status", ["BLOCKED", "CLOSED"])
def test_setup_rejects_non_activatable_status(
    setup_client: TestClient, setup_session: Session, blocked_status: str
):
    """BLOCKED/CLOSED -> 401 generico sin fijar PIN ni activar ni consumir OTP."""
    user, plain = _make_setup_user(setup_session, status=blocked_status)

    resp = setup_client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": str(user.id), "code": plain, "pin": PIN},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "INVALID_SETUP_CODE"
    assert _pin_hash(setup_session, user.id) is None, "no se fija PIN en estado no activable"
    assert _user_status(setup_session, user.id) == blocked_status, "no cambia de estado"
    assert _otp_status(setup_session, user.id) == "PENDING", "el OTP no queda a medias"


def test_setup_pin_already_set_409(setup_client: TestClient, setup_session: Session):
    user, plain = _make_setup_user(setup_session, with_pin=True)

    resp = setup_client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": str(user.id), "code": plain, "pin": PIN},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "PIN_ALREADY_SET"


@pytest.mark.parametrize("weak", ["12", "123", "1234567", "abcd", "12a4", "", "  ", "12 34"])
def test_setup_weak_pin_422(setup_client: TestClient, setup_session: Session, weak: str):
    user, plain = _make_setup_user(setup_session)

    resp = setup_client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": str(user.id), "code": plain, "pin": weak},
    )
    assert resp.status_code == 422, resp.text
    assert _pin_hash(setup_session, user.id) is None, "PIN debil no se fija"


def test_setup_atomic_on_late_failure(
    setup_client: TestClient, setup_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """Un fallo posterior no deja ACTIVE sin PIN ni el OTP consumido a medias."""
    from app.modules.identity.service import pin_login as pin_login_service

    user, plain = _make_setup_user(setup_session)

    def _boom(_pin: str) -> str:
        raise ValueError("fallo simulado al hashear")

    monkeypatch.setattr(pin_login_service, "hash_pin", _boom)

    resp = setup_client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": str(user.id), "code": plain, "pin": PIN},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "INVALID_PIN_FORMAT"

    assert _pin_hash(setup_session, user.id) is None
    assert _user_status(setup_session, user.id) == "PENDING_ACTIVATION", "no activa a medias"
    assert _otp_status(setup_session, user.id) == "PENDING", "el OTP no queda a medias"
    assert _outbox_activated(setup_session, user.id) == [], "el evento se revierte con todo"


def test_setup_unknown_user_same_body_as_invalid_code(
    setup_client: TestClient, setup_session: Session
):
    user, _plain = _make_setup_user(setup_session)
    bad_code = setup_client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": str(user.id), "code": "000000", "pin": PIN},
    )
    ghost = setup_client.post(
        "/api/v1/auth/pin/setup",
        json={"user_ref": str(uuid.uuid4()), "code": "000000", "pin": PIN},
    )
    assert ghost.status_code == 401, ghost.text
    assert ghost.json()["error"]["code"] == "INVALID_SETUP_CODE"
    assert ghost.json()["error"]["code"] == bad_code.json()["error"]["code"]
    assert ghost.json()["error"]["message"] == bad_code.json()["error"]["message"]


def test_openapi_exposes_pin_setup(setup_client: TestClient):
    spec = setup_client.get("/openapi.json")
    assert spec.status_code == 200
    assert "/api/v1/auth/pin/setup" in spec.json()["paths"]
