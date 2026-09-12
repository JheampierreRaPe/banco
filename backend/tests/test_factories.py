"""Pruebas unitarias de factories y dataset semilla (Q-T01, sin BD)."""

from tests.factories import (
    build_seed_dataset,
    dataset_fingerprint,
    det_uuid,
    idempotency_key,
    validate_dataset,
)


def test_seed_reproducible_misma_semilla() -> None:
    assert dataset_fingerprint(build_seed_dataset(42)) == dataset_fingerprint(
        build_seed_dataset(42)
    )


def test_seed_distinta_semilla_difiere() -> None:
    assert dataset_fingerprint(build_seed_dataset(42)) != dataset_fingerprint(build_seed_dataset(7))


def test_dataset_valido() -> None:
    assert validate_dataset(build_seed_dataset()) == []


def test_uuids_deterministicos() -> None:
    assert det_uuid("user-ana-quintero") == det_uuid("user-ana-quintero")
    dataset = build_seed_dataset()
    ids = (
        [u["id"] for u in dataset["users"]]
        + [a["id"] for a in dataset["accounts"]]
        + [c["id"] for c in dataset["chart_of_accounts"]]
        + [m["id"] for m in dataset["merchants"]]
        + [b["id"] for b in dataset["billers"]]
        + [e["id"] for e in dataset["journal_entries"]]
    )
    assert len(set(ids)) == len(ids), "IDs duplicados en el dataset"


def test_cuentas_referencian_usuarios_y_monedas_validas() -> None:
    dataset = build_seed_dataset()
    owners = {u["id"] for u in dataset["users"]}
    assert dataset["accounts"], "La semilla debe incluir cuentas PEN/USD"
    assert {a["currency"] for a in dataset["accounts"]} == {"PEN", "USD"}
    for account in dataset["accounts"]:
        assert account["owner_id"] in owners
        assert isinstance(account["balance_minor"], int)
        assert account["balance_minor"] >= 0


def test_catalogo_contable_sin_codigos_duplicados() -> None:
    dataset = build_seed_dataset()
    codes = [c["code"] for c in dataset["chart_of_accounts"]]
    assert len(set(codes)) == len(codes)
    assert {"1011", "2011"}.issubset(set(codes))


def test_asientos_cuadrados() -> None:
    for entry in build_seed_dataset()["journal_entries"]:
        debit = sum(p["debit_minor"] for p in entry["postings"])
        credit = sum(p["credit_minor"] for p in entry["postings"])
        assert debit == credit > 0, f"Asiento {entry['alias']} descuadrado"


def test_idempotency_keys_unicas() -> None:
    assert idempotency_key("transfer", 1) != idempotency_key("transfer", 2)
    assert idempotency_key("transfer", 1) == idempotency_key("transfer", 1)
