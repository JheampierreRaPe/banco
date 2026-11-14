"""Spread dinamico por volumen (E7-T10, HU25).

Cubre: dominio puro (`spread_bps_for` por tramos con borde inferior
inclusivo, `apply_spread` multiplicativo con `Decimal` y redondeo a
8 decimales, `bps = 0` valido, tramos desordenados/solapados/mal
tipados rechazados, nunca `float`); configuracion via
`shared/config` (`fx.spread_bps_default = 100`,
`fx.spread_tiers_json` con semilla `< 1,000 PEN` 150 bps,
`>= 1,000` 100 bps, `>= 10,000` 50 bps; JSON invalido, desordenado o
solapado -> fallback a la semilla; nunca lanza al cotizar);
integracion en `create_quote` (tasa efectiva congelada en `rate`,
porcentaje en `spread`, `target_amount_minor` con la tasa con
spread); cambio de tramos que rige solo para cotizaciones nuevas
(congelado); endpoint `GET /fx/spread` que coincide con la
cotizacion del mismo monto; sin mover dinero; y frontera (`fx` no
importa modelos de otros modulos).

SQLite en memoria con schemas ATTACH (`fx`, `shared`, `config`,
`audit`, `ledger`); `get_db` y `get_current_user_id` de `fx.api`
se inyectan via `app.dependency_overrides` (auth falsa).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.db import Base, get_db
from app.main import app
from app.modules.fx import service as fx_service
from app.modules.fx.api import get_current_user_id
from app.modules.fx.domain import spread as spread_domain
from app.modules.shared.config import parameters as fx_params

USER_A = uuid.uuid4()

CURRENT = {"user": USER_A}

TABLES = [
    "fx.fx_rates",
    "fx.fx_quotes",
    "fx.fx_operations",
    "shared.outbox",
    "shared.idempotency_keys",
    "ledger.journal_entries",
    "ledger.postings",
    "audit.audit_log",
]

SCHEMAS = ("fx", "shared", "config", "audit", "ledger")


def _seed_param(session: Session, key: str, value: str, module: str = "fx") -> None:
    session.execute(
        sa.text(
            "INSERT INTO config.parameters (id, key, value_json, module) "
            "VALUES (:id, :key, :value, :module)"
        ),
        {"id": uuid.uuid4().hex, "key": key, "value": value, "module": module},
    )
    session.flush()


def _set_param(session: Session, key: str, value: str) -> None:
    session.execute(
        sa.text("UPDATE config.parameters SET value_json = :value WHERE key = :key"),
        {"value": value, "key": key},
    )
    session.commit()


@pytest.fixture()
def fx_spread_client():
    """TestClient con BD SQLite aislada y auth falsa (titular unico)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    import app.modules.audit.models as _au
    import app.modules.fx.models as _fx
    import app.modules.ledger.models as _l
    import app.modules.shared.models as _s

    assert _au is not None and _fx is not None and _l is not None and _s is not None

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
    CURRENT["user"] = USER_A
    try:
        _seed_param(session, "fx.quote_ttl_seconds", "30")
        session.commit()

        def _override_db():
            yield session

        def _override_user():
            return CURRENT["user"]

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_id] = _override_user
        with TestClient(app) as client:
            yield {"client": client, "session": session}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_id, None)
        session.close()
        engine.dispose()


def _count(session: Session, table: str) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[table])))


def _quote(client: TestClient, body: dict):
    return client.post("/api/v1/fx/quotes", json=body)


def _spread(client: TestClient, base: str, quote: str, amount_minor: int):
    return client.get(
        "/api/v1/fx/spread", params={"base": base, "quote": quote, "amount_minor": amount_minor}
    )


# ------------------------------------------------- dominio: tramos (CA-01)
def test_spread_bps_for_tramos_y_bordes_inclusivos():
    tiers = spread_domain.SEED_TIERS
    assert spread_domain.spread_bps_for(0, tiers) == 150
    assert spread_domain.spread_bps_for(99_999, tiers) == 150
    assert spread_domain.spread_bps_for(100_000, tiers) == 100  # borde exacto
    assert spread_domain.spread_bps_for(999_999, tiers) == 100
    assert spread_domain.spread_bps_for(1_000_000, tiers) == 50  # borde exacto
    assert spread_domain.spread_bps_for(10**12, tiers) == 50  # ultimo tramo sin maximo
    assert not isinstance(spread_domain.spread_bps_for(100, tiers), float)


def test_spread_bps_for_acepta_dicts_y_pares():
    as_dicts = [
        {"min_amount_minor": 0, "bps": 150},
        {"min_amount_minor": 100_000, "bps": 100},
    ]
    as_pairs = [(0, 150), (100_000, 100)]
    assert spread_domain.spread_bps_for(50, as_dicts) == 150
    assert spread_domain.spread_bps_for(50, as_pairs) == 150
    assert spread_domain.spread_bps_for(200_000, as_dicts) == 100


def test_spread_bps_cero_es_valido():
    assert spread_domain.spread_bps_for(10, [(0, 0)]) == 0
    assert spread_domain.apply_spread(Decimal("3.80"), 0) == Decimal("3.80")
    assert spread_domain.spread_percent_for(0) == Decimal(0)


@pytest.mark.parametrize(
    "tiers",
    [
        [],  # sin tramos
        "no-es-lista",
        [{"min_amount_minor": 0}],  # sin clave bps
        [{"bps": 150}],  # sin clave min
        [{"min_amount_minor": 100, "bps": 100}, {"min_amount_minor": 0, "bps": 150}],  # desorden
        [{"min_amount_minor": 0, "bps": 150}, {"min_amount_minor": 0, "bps": 100}],  # solape
        [{"min_amount_minor": -1, "bps": 100}],  # min negativo
        [{"min_amount_minor": 0, "bps": -1}],  # bps negativo
        [{"min_amount_minor": True, "bps": 100}],  # bool no es entero
        [{"min_amount_minor": 0, "bps": 1.5}],  # float prohibido
        [{"min_amount_minor": "0", "bps": 100}],  # texto no es entero
        [(100, 200, 300)],  # par mal formado
    ],
)
def test_tramos_invalidos_rechazados(tiers):
    with pytest.raises(spread_domain.SpreadTierError):
        spread_domain.normalize_tiers(tiers)


def test_monto_bajo_primer_tramo_y_monto_invalido():
    with pytest.raises(spread_domain.SpreadTierError):
        spread_domain.spread_bps_for(50, [(100, 10)])
    for bad in (-1, True, 1.5, "100", None):
        with pytest.raises(ValueError):
            spread_domain.spread_bps_for(bad, spread_domain.SEED_TIERS)


def test_apply_spread_multiplica_con_decimal_y_redondea():
    assert spread_domain.apply_spread(Decimal("3.80"), 150) == Decimal("3.857")
    assert spread_domain.apply_spread(Decimal("3.80"), 100) == Decimal("3.838")
    assert spread_domain.apply_spread(Decimal("3.80"), 50) == Decimal("3.819")
    # Redondeo documentado a 8 decimales (`ROUND_HALF_UP`):
    # 0.26315789 * 1.015 = 0.26710525835 -> 0.26710526.
    assert spread_domain.apply_spread(Decimal("0.26315789"), 150) == Decimal("0.26710526")
    effective = spread_domain.apply_spread(Decimal("3.80"), 150)
    assert isinstance(effective, Decimal) and not isinstance(effective, float)


@pytest.mark.parametrize("rate", [0, -1, "abc", float("nan"), 3.8, True, None])
def test_apply_spread_rechaza_tasa_invalida(rate):
    with pytest.raises(spread_domain.SpreadTierError):
        spread_domain.apply_spread(rate, 100)


@pytest.mark.parametrize("bps", [-1, True, 1.5, "150", None])
def test_apply_spread_rechaza_bps_invalido(bps):
    with pytest.raises(spread_domain.SpreadTierError):
        spread_domain.apply_spread(Decimal("3.80"), bps)


def test_spread_percent_for_convierte_bps_a_porcentaje():
    assert spread_domain.spread_percent_for(150) == Decimal("1.5")
    assert spread_domain.spread_percent_for(100) == Decimal(1)
    assert spread_domain.spread_percent_for(1) == Decimal("0.01")
    assert not isinstance(spread_domain.spread_percent_for(150), float)


# ------------------------------------------------- config con fallback (CA-04)
def test_config_defaults_sin_claves_sembradas(fx_spread_client):
    session = fx_spread_client["session"]
    assert fx_params.get_fx_spread_bps_default(session) == 100
    assert fx_params.get_fx_spread_tiers(session) == spread_domain.SEED_TIERS


def test_config_default_invalido_cae_a_100(fx_spread_client):
    session = fx_spread_client["session"]
    _seed_param(session, "fx.spread_bps_default", '"abc"')
    session.commit()
    assert fx_params.get_fx_spread_bps_default(session) == 100
    _set_param(session, "fx.spread_bps_default", "-5")
    assert fx_params.get_fx_spread_bps_default(session) == 100
    _set_param(session, "fx.spread_bps_default", "25")
    assert fx_params.get_fx_spread_bps_default(session) == 25


@pytest.mark.parametrize(
    "raw",
    [
        "no-es-json{{{",
        "[1, 2]",  # items no mapeos
        "[]",  # vacio
        '[{"min_amount_minor": 100, "bps": 10}, {"min_amount_minor": 0, "bps": 5}]',  # desorden
        '[{"min_amount_minor": 0, "bps": 10}, {"min_amount_minor": 0, "bps": 5}]',  # solape
        '[{"min_amount_minor": 0, "bps": -5}]',  # bps negativo
        '[{"min_amount_minor": 0, "bps": 1.5}]',  # float prohibido
    ],
)
def test_tiers_invalidos_caen_a_la_semilla(fx_spread_client, raw: str):
    session = fx_spread_client["session"]
    _seed_param(session, "fx.spread_tiers_json", raw)
    session.commit()
    assert fx_params.get_fx_spread_tiers(session) == spread_domain.SEED_TIERS


def test_tiers_validos_se_respetan(fx_spread_client):
    session = fx_spread_client["session"]
    _seed_param(session, "fx.spread_tiers_json", '[{"min_amount_minor": 0, "bps": 7}]')
    session.commit()
    assert fx_params.get_fx_spread_tiers(session) == ((0, 7),)


# ------------------------------------------------- integracion: cotizacion con spread (CA-01)
@pytest.mark.parametrize(
    "amount, bps, rate, spread, target",
    [
        (38_000, 150, Decimal("3.857"), Decimal("1.5"), 9_852),
        (100_000, 100, Decimal("3.838"), Decimal(1), 26_055),
        (1_000_000, 50, Decimal("3.819"), Decimal("0.5"), 261_849),
    ],
)
def test_create_quote_aplica_spread_por_tramo(
    fx_spread_client, amount: int, bps: int, rate: Decimal, spread: Decimal, target: int
):
    data = _quote(
        fx_spread_client["client"],
        {"base": "PEN", "quote": "USD", "source_amount_minor": amount},
    ).json()["data"]
    assert Decimal(str(data["rate"])) == rate
    assert Decimal(str(data["spread"])) == spread
    assert data["source_amount_minor"] == amount
    assert data["target_amount_minor"] == target
    assert isinstance(data["target_amount_minor"], int)
    # Tasa efectiva coherente: `rate = 3.80 * (1 + bps/10000)`.
    assert Decimal(str(data["rate"])) == spread_domain.apply_spread(Decimal("3.80"), bps)


def test_cambiar_tramos_cambia_siguiente_cotizacion_sin_reiniciar(fx_spread_client):
    client, session = fx_spread_client["client"], fx_spread_client["session"]
    body = {"base": "PEN", "quote": "USD", "source_amount_minor": 38_000}
    first = _quote(client, body).json()["data"]
    assert Decimal(str(first["spread"])) == Decimal("1.5")

    _seed_param(session, "fx.spread_tiers_json", '[{"min_amount_minor": 0, "bps": 5}]')
    session.commit()
    second = _quote(client, body).json()["data"]
    assert Decimal(str(second["spread"])) == Decimal("0.05")
    assert Decimal(str(second["rate"])) == spread_domain.apply_spread(Decimal("3.80"), 5)
    assert second["target_amount_minor"] == 9_995  # 38000 / 3.8019
    assert second["quote_id"] != first["quote_id"]


def test_cotizacion_congela_spread_ante_cambio_de_tramos(fx_spread_client):
    client, session = fx_spread_client["client"], fx_spread_client["session"]
    quote_id = _quote(
        client, {"base": "PEN", "quote": "USD", "source_amount_minor": 38_000}
    ).json()["data"]["quote_id"]

    _seed_param(session, "fx.spread_tiers_json", '[{"min_amount_minor": 0, "bps": 0}]')
    session.commit()

    frozen = client.get(f"/api/v1/fx/quotes/{quote_id}").json()["data"]
    assert Decimal(str(frozen["rate"])) == Decimal("3.857")
    assert Decimal(str(frozen["spread"])) == Decimal("1.5")
    assert frozen["target_amount_minor"] == 9_852


def test_json_invalido_cae_a_la_semilla_en_la_cotizacion(fx_spread_client):
    client, session = fx_spread_client["client"], fx_spread_client["session"]
    _seed_param(session, "fx.spread_tiers_json", "roto{{{")
    _seed_param(session, "fx.spread_bps_default", "25")
    session.commit()
    data = _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 38_000}).json()[
        "data"
    ]
    # Nunca lanza: el JSON roto cae a la semilla (`bps = 150`), no al
    # default (el default solo rige si ningun tramo aplica).
    assert Decimal(str(data["spread"])) == Decimal("1.5")
    assert Decimal(str(data["rate"])) == Decimal("3.857")
    assert data["target_amount_minor"] == 9_852


def test_sin_tramo_aplicable_cae_al_default_en_la_cotizacion(fx_spread_client):
    client, session = fx_spread_client["client"], fx_spread_client["session"]
    _seed_param(session, "fx.spread_tiers_json", '[{"min_amount_minor": 500, "bps": 10}]')
    _seed_param(session, "fx.spread_bps_default", "25")
    session.commit()
    data = _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 100}).json()[
        "data"
    ]
    # Nunca lanza: 100 minor < primer tramo 500 -> `bps = 25` del
    # default, tasa efectiva `3.80 * 1.0025 = 3.8095`.
    assert Decimal(str(data["spread"])) == Decimal("0.25")
    assert Decimal(str(data["rate"])) == spread_domain.apply_spread(Decimal("3.80"), 25)
    assert data["target_amount_minor"] == 26  # 100 / 3.8095


def test_spread_explicito_legado_sigue_siendo_incremento_absoluto(fx_spread_client):
    session = fx_spread_client["session"]
    view = fx_service.create_quote(
        session,
        USER_A,
        base="PEN",
        quote="USD",
        source_amount_minor=800,
        spread=Decimal("2.00"),
    )
    assert view["rate"] == Decimal("3.80")
    assert view["spread"] == Decimal("2.00")
    assert view["target_amount_minor"] == 138  # 800 / (3.80 + 2.00)


# ------------------------------------------------- endpoint de previa (CA-05)
def test_spread_endpoint_coincide_con_la_cotizacion(fx_spread_client):
    client, session = fx_spread_client["client"], fx_spread_client["session"]
    assert _count(session, "fx.fx_rates") == 0
    preview = _spread(client, "PEN", "USD", 38_000)
    assert preview.status_code == 200, preview.text
    data = preview.json()["data"]
    assert data == {
        "base": "PEN",
        "quote": "USD",
        "amount_minor": 38_000,
        "spread_bps": 150,
        "rate": str(Decimal("3.80")),
        "effective_rate": str(spread_domain.apply_spread(Decimal("3.80"), 150)),
    }
    # Solo lectura: la previa no persiste tasa ni cotizacion.
    assert _count(session, "fx.fx_rates") == 0
    assert _count(session, "fx.fx_quotes") == 0

    quoted = _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 38_000}).json()[
        "data"
    ]
    assert Decimal(str(quoted["rate"])) == Decimal(data["effective_rate"])
    assert Decimal(str(quoted["spread"])) == Decimal(data["spread_bps"]) / 100


@pytest.mark.parametrize(
    "base,quote,amount",
    [
        ("PEN", "USD", 0),
        ("PEN", "USD", -5),
        ("PEN", "PEN", 100),
        ("PEN", "GBP", 100),
        ("USD", "USD", 100),
    ],
)
def test_spread_endpoint_rechaza_monto_y_moneda(
    fx_spread_client, base: str, quote: str, amount: int
):
    resp = _spread(fx_spread_client["client"], base, quote, amount)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_spread_endpoint_exige_auth(fx_spread_client):
    app.dependency_overrides.pop(get_current_user_id, None)
    try:
        assert _spread(fx_spread_client["client"], "PEN", "USD", 100).status_code == 401
    finally:
        app.dependency_overrides[get_current_user_id] = lambda: CURRENT["user"]


# ------------------------------------------------- dinero y frontera
def test_spread_no_mueve_dinero(fx_spread_client):
    client, session = fx_spread_client["client"], fx_spread_client["session"]
    assert client.get("/api/v1/fx/rates", params={"base": "PEN", "quote": "USD"}).status_code == 200
    assert _spread(client, "PEN", "USD", 38_000).status_code == 200
    assert (
        _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 38_000}).status_code
        == 201
    )
    assert _count(session, "ledger.journal_entries") == 0
    assert _count(session, "ledger.postings") == 0


def test_frontera_spread_no_importa_modelos_ajenos():
    """`fx/domain/spread.py` es puro: sin modelos/tablas de otros modulos."""
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[1]
    text = (backend_dir / "app/modules/fx/domain/spread.py").read_text(encoding="utf-8")
    for forbidden in (
        "app.modules.accounts.models",
        "app.modules.accounts.repository",
        "app.modules.transactions",
        "app.modules.ledger",
        "post_entry",
    ):
        assert forbidden not in text, f"frontera rota en domain/spread.py: {forbidden}"


def test_expiracion_y_estado_con_spread_dinamico(fx_spread_client):
    """La vigencia on-read sigue intacta con spread dinamico (CA-03)."""
    client, session = fx_spread_client["client"], fx_spread_client["session"]
    quote_id = _quote(client, {"base": "PEN", "quote": "USD", "source_amount_minor": 380}).json()[
        "data"
    ]["quote_id"]

    from app.modules.fx.models import FxQuote

    row = session.get(FxQuote, uuid.UUID(quote_id))
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=60)
    row.created_at = past
    row.expires_at = past + timedelta(seconds=30)
    session.commit()

    resp = client.get(f"/api/v1/fx/quotes/{quote_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "EXPIRED"
