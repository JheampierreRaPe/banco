"""Factories y dataset semilla determinista (Q-T01).

Todo 100% simulado: correos `@example.com`, documentos ficticios y UUID v5
derivados del nombre (reproducibles en cualquier entorno). El dinero va en
centimos enteros (`*_minor`); nunca `float`.

Este dataset es la fuente de verdad para la semilla y para las suites de QA
(Q-T02 motor/ledger, Q-T03 seguridad, Q-T04 contratos). Cuando los modulos
creen sus tablas, los loaders insertaran estos mismos diccionarios.
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid

DATASET_VERSION = "seed-v1"
DEFAULT_SEED = 42
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "banca-online-qa")

CURRENCIES = ("PEN", "USD")


def det_uuid(name: str) -> str:
    """UUID determinista (v5) a partir de un nombre estable."""
    return str(uuid.uuid5(NAMESPACE, name))


def demo_users() -> list[dict]:
    """Usuarios demo simulados (sin PII real)."""
    return [
        {
            "id": det_uuid("user-ana-quintero"),
            "alias": "ana",
            "email": "ana.quintero@example.com",
            "doc_number": "00000001",
            "kyc_status": "approved",
            "role": "titular",
        },
        {
            "id": det_uuid("user-bruno-paredes"),
            "alias": "bruno",
            "email": "bruno.paredes@example.com",
            "doc_number": "00000002",
            "kyc_status": "approved",
            "role": "titular",
        },
        {
            "id": det_uuid("user-carmen-torres"),
            "alias": "carmen",
            "email": "carmen.torres@example.com",
            "doc_number": "00000003",
            "kyc_status": "approved",
            "role": "comercio",
        },
        {
            "id": det_uuid("user-diego-rios"),
            "alias": "diego",
            "email": "diego.rios@example.com",
            "doc_number": "00000004",
            "kyc_status": "rejected",
            "role": "titular",
        },
    ]


def demo_accounts(users: list[dict], seed: int = DEFAULT_SEED) -> list[dict]:
    """Cuentas PEN/USD con saldos iniciales (centimos). El jitter es seeded."""
    rng = random.Random(seed)
    by_alias = {u["alias"]: u for u in users}
    specs = [
        ("ana-pen", "ana", "PEN", 150_000),
        ("bruno-pen", "bruno", "PEN", 20_000),
        ("bruno-usd", "bruno", "USD", 50_000),
        ("carmen-pen", "carmen", "PEN", 300_000),
    ]
    accounts = []
    for alias, owner, currency, base_minor in specs:
        accounts.append(
            {
                "id": det_uuid(f"account-{alias}"),
                "alias": alias,
                "owner_id": by_alias[owner]["id"],
                "currency": currency,
                "balance_minor": base_minor + rng.randrange(0, 1000),
                "status": "active",
            }
        )
    return accounts


def chart_of_accounts() -> list[dict]:
    """Catalogo contable minimo para el ledger (partida doble)."""
    rows = [
        ("1011", "Caja PEN", "activo", "PEN"),
        ("1012", "Caja USD", "activo", "USD"),
        ("1013", "Cuenta puente interbancaria", "activo", "*"),
        ("2011", "Depositos a la vista PEN", "pasivo", "PEN"),
        ("2012", "Depositos a la vista USD", "pasivo", "USD"),
        ("3011", "Capital social", "patrimonio", "*"),
        ("4011", "Ingresos por comisiones", "ingreso", "*"),
        ("5011", "Egresos operativos", "egreso", "*"),
    ]
    return [
        {
            "id": det_uuid(f"ledger-account-{code}"),
            "code": code,
            "name": name,
            "type": kind,
            "currency_scope": scope,
        }
        for code, name, kind, scope in rows
    ]


def demo_merchants(users: list[dict]) -> list[dict]:
    """Comercios con cobro QR (propiedad de `carmen`, solo simulados)."""
    owner = next(u for u in users if u["alias"] == "carmen")["id"]
    return [
        {
            "id": det_uuid("merchant-cafe-central"),
            "alias": "cafe-central",
            "name": "Cafe Central (demo)",
            "owner_id": owner,
            "qr_code": "QR-DEMO-CAFE-0001",
            "status": "active",
        },
        {
            "id": det_uuid("merchant-libreria-norte"),
            "alias": "libreria-norte",
            "name": "Libreria Norte (demo)",
            "owner_id": owner,
            "qr_code": "QR-DEMO-LIB-0002",
            "status": "active",
        },
    ]


def demo_billers() -> list[dict]:
    """Empresas de servicios (billers) para pago de servicios demo."""
    rows = [
        ("biller-luz", "Luz del Sur (demo)", "electricidad"),
        ("biller-agua", "Sedapal Agua (demo)", "agua"),
        ("biller-movil", "Movil Peru (demo)", "telecom"),
    ]
    return [
        {"id": det_uuid(alias), "alias": alias, "name": name, "category": cat, "status": "active"}
        for alias, name, cat in rows
    ]


def balanced_entry(entry_alias: str, currency: str, lines: list[tuple[str, int, int]]) -> dict:
    """Arma un asiento: [(ledger_code, debito_minor, credito_minor), ...].

    Lanza ValueError si no cuadra (sum(debitos) == sum(creditos)).
    """
    postings = [
        {
            "ledger_code": code,
            "debit_minor": debit,
            "credit_minor": credit,
            "currency": currency,
        }
        for code, debit, credit in lines
    ]
    total_debit = sum(p["debit_minor"] for p in postings)
    total_credit = sum(p["credit_minor"] for p in postings)
    if total_debit != total_credit or total_debit <= 0:
        raise ValueError(
            f"Asiento {entry_alias!r} descuadrado: debitos={total_debit} "
            f"creditos={total_credit} ({currency})"
        )
    return {
        "id": det_uuid(f"journal-entry-{entry_alias}"),
        "alias": entry_alias,
        "currency": currency,
        "total_minor": total_debit,
        "postings": postings,
    }


def sample_entries() -> list[dict]:
    """Asientos de ejemplo cuadrados (fondeo + transferencia interna)."""
    return [
        balanced_entry(
            "fondeo-inicial-pen",
            "PEN",
            [
                ("1011", 170_000, 0),
                ("2011", 0, 170_000),
            ],
        ),
        balanced_entry(
            "traspaso-interno-pen",
            "PEN",
            [
                ("2011", 20_000, 0),
                ("2011", 0, 20_000),
            ],
        ),
    ]


def idempotency_key(scope: str, n: int) -> str:
    """Llave de idempotencia determinista para pruebas del motor."""
    return det_uuid(f"idempotency-{scope}-{n:04d}")


def build_seed_dataset(seed: int = DEFAULT_SEED) -> dict:
    """Construye el dataset completo. Misma semilla => mismo dataset."""
    users = demo_users()
    return {
        "meta": {"version": DATASET_VERSION, "seed": seed},
        "users": users,
        "accounts": demo_accounts(users, seed),
        "chart_of_accounts": chart_of_accounts(),
        "merchants": demo_merchants(users),
        "billers": demo_billers(),
        "journal_entries": sample_entries(),
        "sample_idempotency_keys": [idempotency_key("transfer", n) for n in (1, 2, 3)],
    }


def dataset_fingerprint(dataset: dict) -> str:
    """Huella sha256 del JSON canonico (sin timestamps: reproducible)."""
    canonical = json.dumps(dataset, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_dataset(dataset: dict) -> list[str]:
    """Valida el dataset. Retorna la lista de errores (vacia = valido)."""
    errors: list[str] = []
    users = {u["id"] for u in dataset.get("users", [])}
    chart = {c["code"] for c in dataset.get("chart_of_accounts", [])}

    for u in dataset.get("users", []):
        if not u.get("email", "").endswith("@example.com"):
            errors.append(f"user {u.get('alias')}: email debe ser @example.com (simulado)")
        if not u.get("doc_number", "").startswith("000"):
            errors.append(f"user {u.get('alias')}: doc_number debe ser ficticio (000...)")

    for a in dataset.get("accounts", []):
        if a["owner_id"] not in users:
            errors.append(f"account {a.get('alias')}: owner_id desconocido")
        if a["currency"] not in CURRENCIES:
            errors.append(f"account {a.get('alias')}: moneda {a['currency']} invalida")
        if not isinstance(a["balance_minor"], int) or isinstance(a["balance_minor"], bool):
            errors.append(f"account {a.get('alias')}: balance_minor debe ser entero")
        elif a["balance_minor"] < 0:
            errors.append(f"account {a.get('alias')}: balance_minor negativo")

    for m in dataset.get("merchants", []):
        if m["owner_id"] not in users:
            errors.append(f"merchant {m.get('alias')}: owner_id desconocido")

    for e in dataset.get("journal_entries", []):
        if e["currency"] not in CURRENCIES:
            errors.append(f"entry {e.get('alias')}: moneda invalida")
        for p in e["postings"]:
            if p["ledger_code"] not in chart:
                errors.append(
                    f"entry {e.get('alias')}: cuenta {p['ledger_code']} " "fuera del catalogo"
                )
            for field in ("debit_minor", "credit_minor"):
                if not isinstance(p[field], int) or isinstance(p[field], bool):
                    errors.append(f"entry {e.get('alias')}: {field} debe ser entero")
        debit = sum(p["debit_minor"] for p in e["postings"])
        credit = sum(p["credit_minor"] for p in e["postings"])
        if debit != credit or debit <= 0:
            errors.append(f"entry {e.get('alias')}: descuadrado ({debit} vs {credit})")

    keys = dataset.get("sample_idempotency_keys", [])
    if len(set(keys)) != len(keys):
        errors.append("sample_idempotency_keys: duplicadas")
    return errors
