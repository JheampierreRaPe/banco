"""Limites de billetera/QR (E4-T02, HU13; cierra `docs/18` G70 en billetera).

Revision ID: 0038_wallet_express_params
Revises: 0037_wallet
Create Date: 2026-10-13

Fuente: `docs/tasks/E4-T02.md` (parametros `wallet.express_limit_minor` /
`wallet.daily_limit_minor`; dueno del esquema `shared/config`, regla de
oro 6, patron `0030`/`0036`). No crea tablas; solo filas de
`config.parameters` (el TTL del QR, `wallet.qr_ttl_seconds`, lo siembra
`E4-T06`).

Siembra (idempotente, `ON CONFLICT (key) DO NOTHING`: respeta valores
ya personalizados y es re-ejecutable):

- `wallet.express_limit_minor = 20000` (S/ 200: bajo este total el pago
  express liquida sin reto del servidor; misma magnitud que el
  `qr.express_limit_minor` sembrado en `0001`).
- `wallet.daily_limit_minor = 500000` (S/ 5,000: `acumulado + total >`
  limite -> `REJECTED` + `DAILY_LIMIT_EXCEEDED`; mismo valor que el
  diario generico de transferencias).

Invariante (como OBS-2 `0025`): base < diario, para que el refuerzo
(`PENDING_AUTHORIZATION`) sea alcanzable en la banda
`(20000, 500000]`.

NOTA: el `revision` tiene maximo 32 caracteres (`alembic_version.
version_num VARCHAR(32)`): `0038_wallet_express_params` (26) lo respeta.
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_wallet_express_params"
down_revision = "0037_wallet"
branch_labels = None
depends_on = None

PARAMS = (
    (
        "wallet.express_limit_minor",
        "20000",
        "wallet",
        "Limite base del pago express sin reto del servidor (centimos, E4-T02)",
    ),
    (
        "wallet.daily_limit_minor",
        "500000",
        "wallet",
        "Limite diario de billetera: acumulado + total > limite -> REJECTED (centimos, E4-T02)",
    ),
)


def upgrade() -> None:
    for key, value, module, description in PARAMS:
        op.execute(
            sa.text(
                "INSERT INTO config.parameters (id, key, value_json, module, description) "
                "VALUES (gen_random_uuid(), :key, CAST(:value AS jsonb), :module, :description) "
                "ON CONFLICT (key) DO NOTHING"
            ).bindparams(
                key=key,
                value=value,
                module=module,
                description=description,
            )
        )


def downgrade() -> None:
    for key, _value, _module, _description in PARAMS:
        op.execute(sa.text("DELETE FROM config.parameters WHERE key = :key").bindparams(key=key))
