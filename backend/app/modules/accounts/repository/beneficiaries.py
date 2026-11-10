"""Repositorio de beneficiarios (`accounts.beneficiaries`, E2-T13, HU07).

Capa de datos sin endpoints ni reglas de negocio: solo SQLAlchemy sobre el
schema propio (`beneficiaries`, `beneficiary_search_tokens`). No accede a
tablas de otros modulos (`owner_user_id` es UUID logico, sin FK fisica).

Convencion: las funciones hacen `flush` y no `commit`; quien llama decide
la transaccion. No publica eventos aqui (regla de oro 8: el `service/`
registra `beneficiary.saved` en el outbox). Sin PII en logs: aqui no se
logea nada (los hashes/ids los correlaciona el llamante si aplica).

La busqueda (`search_active`) se resuelve SOLO con los indices ciegos
(`beneficiary_search_tokens` + columna `alias_search_hash`): jamas
descifra filas para filtrar (ver `service/beneficiaries_crypto`).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.modules.accounts.models.beneficiaries import Beneficiary, BeneficiarySearchToken

#: Tope de resultados de lista/busqueda (HU07: hasta 100 registros).
SEARCH_LIMIT = 100


def _coerce_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} debe ser UUID, recibido: {value!r}") from exc


def create_beneficiary(
    session: Session,
    *,
    owner_user_id: uuid.UUID | str,
    alias_enc: str,
    alias_search_hash: str,
    photo_url: str | None,
    bank_code: str | None,
    account_enc: str,
    account_hash: str,
    account_last4: str,
    holder_name_enc: str | None,
    name_search_hash: str | None,
    holder_doc_hash: str | None,
    currency: str | None,
    token_hashes: set[str],
) -> Beneficiary:
    """Inserta el beneficiario ACTIVE + sus tokens de busqueda (`flush`)."""
    row = Beneficiary(
        owner_user_id=_coerce_uuid(owner_user_id, "owner_user_id"),
        alias_enc=alias_enc,
        alias_search_hash=alias_search_hash,
        photo_url=photo_url,
        bank_code=bank_code,
        account_enc=account_enc,
        account_hash=account_hash,
        account_last4=account_last4,
        holder_name_enc=holder_name_enc,
        name_search_hash=name_search_hash,
        holder_doc_hash=holder_doc_hash,
        currency=currency,
        status="ACTIVE",
    )
    session.add(row)
    session.flush()
    _replace_tokens(session, row, token_hashes)
    return row


def _replace_tokens(session: Session, row: Beneficiary, token_hashes: set[str]) -> None:
    """Sincroniza el indice invertido del beneficiario (`flush`)."""
    session.execute(
        sa.delete(BeneficiarySearchToken).where(
            BeneficiarySearchToken.beneficiary_id == row.id,
        )
    )
    for digest in sorted(set(token_hashes or set())):
        session.add(
            BeneficiarySearchToken(
                beneficiary_id=row.id,
                token_hash=digest,
                owner_user_id=row.owner_user_id,
            )
        )
    session.flush()


def sync_tokens(session: Session, row: Beneficiary, token_hashes: set[str]) -> None:
    """Re-indexa tras editar alias/nombre (`flush`)."""
    _replace_tokens(session, row, token_hashes)


def get_beneficiary(session: Session, beneficiary_id: uuid.UUID | str) -> Beneficiary | None:
    """Lee un beneficiario por id (`None` si no existe)."""
    return session.get(Beneficiary, _coerce_uuid(beneficiary_id, "beneficiary_id"))


def get_active(session: Session, owner_user_id: uuid.UUID | str, beneficiary_id: uuid.UUID | str):
    """Lee un beneficiario ACTIVE del titular (`None` si no existe/ajeno/baja)."""
    owner = _coerce_uuid(owner_user_id, "owner_user_id")
    bid = _coerce_uuid(beneficiary_id, "beneficiary_id")
    stmt = sa.select(Beneficiary).where(
        Beneficiary.id == bid,
        Beneficiary.owner_user_id == owner,
        Beneficiary.status == "ACTIVE",
    )
    return session.scalars(stmt).first()


def list_active(
    session: Session, owner_user_id: uuid.UUID | str, *, limit: int = SEARCH_LIMIT
) -> list[Beneficiary]:
    """Lista los beneficiarios ACTIVE del titular (hasta `limit`, orden estable)."""
    owner = _coerce_uuid(owner_user_id, "owner_user_id")
    stmt = (
        sa.select(Beneficiary)
        .where(Beneficiary.owner_user_id == owner, Beneficiary.status == "ACTIVE")
        .order_by(Beneficiary.created_at, Beneficiary.id)
        .limit(max(1, min(int(limit), SEARCH_LIMIT)))
    )
    return list(session.scalars(stmt).all())


def find_active_duplicate_alias(
    session: Session,
    owner_user_id: uuid.UUID | str,
    alias_search_hash: str,
    *,
    exclude_id: uuid.UUID | str | None = None,
) -> Beneficiary | None:
    """Duplicado por alias exacto (normalizado) del titular, ACTIVE."""
    owner = _coerce_uuid(owner_user_id, "owner_user_id")
    stmt = sa.select(Beneficiary).where(
        Beneficiary.owner_user_id == owner,
        Beneficiary.alias_search_hash == alias_search_hash,
        Beneficiary.status == "ACTIVE",
    )
    if exclude_id is not None:
        stmt = stmt.where(Beneficiary.id != _coerce_uuid(exclude_id, "beneficiary_id"))
    return session.scalars(stmt).first()


def find_active_duplicate_account(
    session: Session,
    owner_user_id: uuid.UUID | str,
    account_hash: str,
    *,
    exclude_id: uuid.UUID | str | None = None,
) -> Beneficiary | None:
    """Duplicado por cuenta/CCI exacto del titular, ACTIVE."""
    owner = _coerce_uuid(owner_user_id, "owner_user_id")
    stmt = sa.select(Beneficiary).where(
        Beneficiary.owner_user_id == owner,
        Beneficiary.account_hash == account_hash,
        Beneficiary.status == "ACTIVE",
    )
    if exclude_id is not None:
        stmt = stmt.where(Beneficiary.id != _coerce_uuid(exclude_id, "beneficiary_id"))
    return session.scalars(stmt).first()


def search_active(
    session: Session,
    owner_user_id: uuid.UUID | str,
    token_hashes: set[str],
    *,
    limit: int = SEARCH_LIMIT,
) -> list[Beneficiary]:
    """Busqueda indexada (AND entre hashes) SIN descifrar filas.

    Intersecta los conjuntos `beneficiary_id` por cada hash (ambito del
    titular) y retorna las filas ACTIVE en orden estable, hasta `limit`.
    Conjunto vacio -> `[]` (sin full-scan).
    """
    owner = _coerce_uuid(owner_user_id, "owner_user_id")
    digests = sorted(set(token_hashes or set()))
    if not digests:
        return []
    matched: set[uuid.UUID] | None = None
    for digest in digests:
        stmt = sa.select(BeneficiarySearchToken.beneficiary_id).where(
            BeneficiarySearchToken.owner_user_id == owner,
            BeneficiarySearchToken.token_hash == digest,
        )
        ids = set(session.scalars(stmt).all())
        matched = ids if matched is None else (matched & ids)
        if not matched:
            return []
    assert matched is not None
    stmt = (
        sa.select(Beneficiary)
        .where(
            Beneficiary.id.in_(matched),
            Beneficiary.owner_user_id == owner,
            Beneficiary.status == "ACTIVE",
        )
        .order_by(Beneficiary.created_at, Beneficiary.id)
        .limit(max(1, min(int(limit), SEARCH_LIMIT)))
    )
    return list(session.scalars(stmt).all())


def soft_delete(session: Session, row: Beneficiary) -> Beneficiary:
    """Baja logica (`status=DELETED`) + retira tokens del indice (`flush`)."""
    row.status = "DELETED"
    session.execute(
        sa.delete(BeneficiarySearchToken).where(
            BeneficiarySearchToken.beneficiary_id == row.id,
        )
    )
    session.flush()
    return row


__all__ = [
    "SEARCH_LIMIT",
    "create_beneficiary",
    "find_active_duplicate_account",
    "find_active_duplicate_alias",
    "get_active",
    "get_beneficiary",
    "list_active",
    "search_active",
    "soft_delete",
    "sync_tokens",
]
