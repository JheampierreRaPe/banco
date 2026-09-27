"""Casos de uso de beneficiarios frecuentes (E2-T13, HU07 CA-01..CA-04).

Lee/escribe via `accounts/repository/beneficiaries` (E2-T01): sin FK entre
schemas, dinero entero en centimos (el tope es `BIGINT >= 0`), nunca
`float`. Sin PII en logs (regla de oro 7): aqui no se loguea alias,
nombre, CCI ni documento (solo `id` y tamaños).

Reglas aplicadas:

- RBAC: el `owner_user_id` se toma del contexto de auth (JWT), nunca del
  cuerpo; toda funcion lo recibe y filtra/verifica propiedad aqui.
- Cifrado en reposo (`docs/02#10`): `alias`, `account_or_cci` y
  `holder_name` se cifran antes de persistir; `holder_doc` solo deja su
  HMAC (`holder_doc_hash`); jamas en claro en BD ni en respuestas.
- Busqueda indexada `< 1 s` (hasta 100): por igualdad sobre
  `beneficiary_search_tokens` (palabra + prefijos, AND entre terminos);
  vacia (`q` ausente) = lista ACTIVE hasta 100. Sin descifrar para
  filtrar.
- Enmascaramiento por defecto (regla de oro 9): las vistas exponen
  `account_masked` (`****` + ultimos 4); el claro solo sale por la
  fachada interna `get_for_operation` (la usara E2-T14/motor, nunca la
  API).
- Duplicados por titular: mismo alias normalizado o misma cuenta/CCI
  (ACTIVE) -> `DuplicateBeneficiaryError` (`DUPLICATE_BENEFICIARY`,
  `docs/18` pendiente de registrar el codigo).
- Soft-delete con confirmacion: `delete_beneficiary(..., confirm=True)`;
  sin `confirm` -> `ValueError` (el router lo traduce a 400).
- Idempotencia de alta (recomendada, no mueve dinero):
  `create_beneficiary_idempotent` usa `shared.idempotency_keys` (import
  perezoso: `shared` es infraestructura transversal, igual que `outbox`);
  misma clave + mismo cuerpo -> replay sin duplicar; distinta -> 409.
- Eventos (regla de oro 8): `beneficiary.saved` se registra en el outbox
  en la misma transaccion (`flush`, sin `commit`); el payload lleva solo
  UUIDs y estado, sin PII.

Convencion: `flush` sin `commit`; el endpoint confirma.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.accounts.repository import beneficiaries as beneficiaries_repo
from app.modules.accounts.service import beneficiaries_crypto as crypto

#: Evento emitido al crear/editar/eliminar (`docs/modules/README.md#accounts`).
BENEFICIARY_SAVED_EVENT = "beneficiary.saved"

#: Limites de validacion (reglas configurables fuera del codigo si el
#: negocio los vuelve variables: hoy son formato, no umbrales).
ALIAS_MAX_LEN = 80
HOLDER_NAME_MAX_LEN = 150
BANK_CODE_MAX_LEN = 20
PHOTO_URL_MAX_LEN = 2000
HOLDER_DOC_MAX_LEN = 30
_ACCOUNT_DIGITS_RE = re.compile(r"^[0-9]{8,20}$")
_BANK_CODE_RE = re.compile(r"^[A-Z0-9]{1,20}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_PHOTO_SCHEMES = ("http://", "https://")

#: Endpoint logico para la idempotencia del alta (clave `shared`, UQ por usuario).
IDEMPOTENCY_ENDPOINT = "/beneficiaries"


class BeneficiaryNotFoundError(LookupError):
    """El beneficiario no existe o esta dado de baja (-> 404 NOT_FOUND)."""


class BeneficiaryForbiddenError(PermissionError):
    """El beneficiario pertenece a otro titular (-> 403 NOT_AUTHORIZED)."""


class DuplicateBeneficiaryError(ValueError):
    """Alias o cuenta/CCI ya existe para el titular (-> 409 DUPLICATE_BENEFICIARY)."""


class IdempotencyConflictError(ValueError):
    """Misma `Idempotency-Key` con distinto cuerpo (-> 409)."""


def _coerce_owner(owner_user_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(owner_user_id, uuid.UUID):
        return owner_user_id
    try:
        return uuid.UUID(str(owner_user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"owner_user_id debe ser UUID, recibido: {owner_user_id!r}") from exc


def normalize_account_or_cci(value: object) -> str:
    """Normaliza cuenta/CCI (sin espacios/guiones); exige 8..20 digitos."""
    if not isinstance(value, str):
        raise TypeError("account_or_cci debe ser texto de 8 a 20 digitos")
    digits = re.sub(r"[\s\-]+", "", value.strip())
    if _ACCOUNT_DIGITS_RE.fullmatch(digits) is None:
        raise ValueError("account_or_cci con formato invalido: debe tener entre 8 y 20 digitos")
    return digits


def _require_alias(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("alias es obligatorio")
    alias = value.strip()
    if len(alias) > ALIAS_MAX_LEN:
        raise ValueError(f"alias supera {ALIAS_MAX_LEN} caracteres")
    return alias


def _clean_holder_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("holder_name debe ser texto")
    name = value.strip()
    if not name:
        return None
    if len(name) > HOLDER_NAME_MAX_LEN:
        raise ValueError(f"holder_name supera {HOLDER_NAME_MAX_LEN} caracteres")
    return name


def _clean_bank_code(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("bank_code con formato invalido")
    code = value.strip().upper()
    if not code:
        return None
    if len(code) > BANK_CODE_MAX_LEN or _BANK_CODE_RE.fullmatch(code) is None:
        raise ValueError("bank_code con formato invalido")
    return code


def _clean_photo_url(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("photo_url debe ser texto")
    url = value.strip()
    if not url:
        return None
    if len(url) > PHOTO_URL_MAX_LEN or not url.lower().startswith(_PHOTO_SCHEMES):
        raise ValueError("photo_url con formato invalido (http(s)://...)")
    return url


def _clean_holder_doc(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("holder_doc debe ser texto")
    doc = re.sub(r"[\s\-.]+", "", value.strip()).upper()
    if not doc:
        return None
    if len(doc) > HOLDER_DOC_MAX_LEN:
        raise ValueError(f"holder_doc supera {HOLDER_DOC_MAX_LEN} caracteres")
    return doc


def _clean_currency(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("currency debe ser ISO-4217 de 3 letras")
    cur = value.strip().upper()
    if not cur:
        return None
    if _CURRENCY_RE.fullmatch(cur) is None:
        raise ValueError(f"currency debe ser ISO-4217 de 3 letras, recibido: {value!r}")
    return cur


def _clean_limit(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("preapproved_limit_minor debe ser int (centimos), sin float")
    if value < 0:
        raise ValueError("preapproved_limit_minor debe ser >= 0")
    return value


def to_view(row) -> dict:
    """Vista para respuestas API: alias/nombre en claro (solo el titular),
    cuenta enmascarada, documento ausente (solo hash en BD)."""
    return {
        "id": str(row.id),
        "alias": crypto.decrypt_value(row.alias_enc),
        "photo_url": row.photo_url,
        "bank_code": row.bank_code,
        "account_masked": crypto.mask_account_or_cci(row.account_last4),
        "holder_name": crypto.decrypt_value(row.holder_name_enc) if row.holder_name_enc else None,
        "preapproved_limit_minor": (
            int(row.preapproved_limit_minor) if row.preapproved_limit_minor is not None else None
        ),
        "currency": row.currency,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at is not None else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at is not None else None,
    }


def _require_owned_active(session: Session, owner: uuid.UUID, beneficiary_id: object):
    """Fila ACTIVE del titular o error: 404 si no existe/baja, 403 si es ajena."""
    try:
        bid = (
            beneficiary_id
            if isinstance(beneficiary_id, uuid.UUID)
            else uuid.UUID(str(beneficiary_id))
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise BeneficiaryNotFoundError(f"beneficiario inexistente: {beneficiary_id}") from exc
    row = beneficiaries_repo.get_beneficiary(session, bid)
    if row is None or row.status != "ACTIVE":
        raise BeneficiaryNotFoundError(f"beneficiario inexistente: {beneficiary_id}")
    if row.owner_user_id != owner:
        raise BeneficiaryForbiddenError("beneficiario de otro titular")
    return row


def _record_saved_event(session: Session, row, *, event: str) -> None:
    """Registra `beneficiary.saved` en el outbox (payload sin PII: solo UUIDs)."""
    from app.modules.shared.repository.outbox import insert_event

    insert_event(
        session,
        aggregate_type="beneficiary",
        aggregate_id=row.id,
        event_type=BENEFICIARY_SAVED_EVENT,
        payload={
            "event": event,
            "beneficiary_id": str(row.id),
            "owner_user_id": str(row.owner_user_id),
            "status": row.status,
        },
    )


def create_beneficiary(
    session: Session,
    owner_user_id: uuid.UUID | str,
    *,
    alias: object,
    account_or_cci: object,
    photo_url: object = None,
    bank_code: object = None,
    holder_name: object = None,
    holder_doc: object = None,
    preapproved_limit_minor: object = None,
    currency: object = None,
) -> dict:
    """Alta de beneficiario (CA-01): valida, cifra, indexa y registra evento."""
    owner = _coerce_owner(owner_user_id)
    clean_alias = _require_alias(alias)
    digits = normalize_account_or_cci(account_or_cci)
    name = _clean_holder_name(holder_name)
    doc = _clean_holder_doc(holder_doc)
    limit = _clean_limit(preapproved_limit_minor)
    cur = _clean_currency(currency)

    alias_hash = crypto.alias_index(clean_alias)
    if beneficiaries_repo.find_active_duplicate_alias(session, owner, alias_hash) is not None:
        raise DuplicateBeneficiaryError("alias ya existe para el titular")
    account_hash = crypto.account_index(digits)
    if beneficiaries_repo.find_active_duplicate_account(session, owner, account_hash) is not None:
        raise DuplicateBeneficiaryError("cuenta/CCI ya existe para el titular")

    try:
        row = beneficiaries_repo.create_beneficiary(
            session,
            owner_user_id=owner,
            alias_enc=crypto.encrypt_value(clean_alias),
            alias_search_hash=alias_hash,
            photo_url=_clean_photo_url(photo_url),
            bank_code=_clean_bank_code(bank_code),
            account_enc=crypto.encrypt_value(digits),
            account_hash=account_hash,
            account_last4=digits[-4:],
            holder_name_enc=crypto.encrypt_value(name) if name else None,
            name_search_hash=crypto.name_index(name) if name else None,
            holder_doc_hash=crypto.blind_index(doc, domain="doc") if doc else None,
            preapproved_limit_minor=limit,
            currency=cur,
            token_hashes=crypto.token_hashes_for(clean_alias, name),
        )
    except IntegrityError as exc:
        # Carrera con otro alta concurrente: el UNIQUE parcial
        # (`0022`, solo ACTIVE) gana al chequeo previo por SELECT.
        # El llamante revierte la transaccion (incluida la `claim`).
        raise DuplicateBeneficiaryError("alias o cuenta/CCI ya existe para el titular") from exc
    _record_saved_event(session, row, event="created")
    return to_view(row)


def _minimal_idempotency_snapshot(*, beneficiary_id: object, status: object) -> dict:
    """Snapshot de idempotencia SIN PII: solo referencia + estado.

    El `view` completo (alias/nombre en claro) jamas se persiste en
    `shared.idempotency_keys.response_snapshot`; el replay reconstruye la
    respuesta leyendo la fila desde el almacen cifrado (`to_view`).
    """
    return {"beneficiary_id": str(beneficiary_id), "status": str(status)}


def _replay_idempotent_view(session: Session, owner: uuid.UUID, stored) -> dict:
    """Reconstruye la respuesta de un alta idempotente desde la BD cifrada.

    Misma forma/valores que el alta original (`to_view`). Los snapshots
    heredados con `view` completo (previos al fix, clave `id`) se retornan
    tal cual por compatibilidad; los nuevos (clave `beneficiary_id`) se
    re-leen. Sin snapshot util -> `IdempotencyConflictError` (409).
    """
    snapshot = stored.response_snapshot
    if not isinstance(snapshot, dict):
        raise IdempotencyConflictError("operacion en curso para esta Idempotency-Key")
    beneficiary_id = snapshot.get("beneficiary_id")
    if beneficiary_id is not None:
        row = beneficiaries_repo.get_beneficiary(session, beneficiary_id)
        if row is None or row.owner_user_id != owner or row.status != "ACTIVE":
            raise IdempotencyConflictError("beneficiario asociado a la clave ya no esta disponible")
        return to_view(row)
    if snapshot.get("id") is not None:  # legado: view completo previo al fix
        return dict(snapshot)
    raise IdempotencyConflictError("operacion en curso para esta Idempotency-Key")


def create_beneficiary_idempotent(
    session: Session,
    owner_user_id: uuid.UUID | str,
    *,
    body: dict,
    idempotency_key: str | None,
) -> tuple[dict, bool]:
    """Alta con `Idempotency-Key` opcional (recomendada).

    Sin clave: crea directo (`replayed=False`). Con clave: misma clave +
    mismo cuerpo -> replay del snapshot (`replayed=True`, sin duplicar);
    misma clave + distinto cuerpo -> `IdempotencyConflictError` (409).
    `body` debe ser el dict canonico ya validado del request.
    """
    owner = _coerce_owner(owner_user_id)
    if idempotency_key is None:
        return create_beneficiary(session, owner, **body), False
    from app.modules.shared.repository.idempotency import (
        compute_request_hash,
        find_key,
        is_expired,
        renew_expired,
        resolve_ttl_seconds,
        store_response,
        try_claim,
    )

    key = str(idempotency_key).strip()
    if not key:
        return create_beneficiary(session, owner, **body), False
    if len(key) > 80:
        raise ValueError("Idempotency-Key supera 80 caracteres")
    digest = compute_request_hash({"owner": str(owner), **body})
    row, created = try_claim(
        session,
        key=key,
        user_id=owner,
        endpoint=IDEMPOTENCY_ENDPOINT,
        method="POST",
        request_hash=digest,
        ttl_seconds=resolve_ttl_seconds(),
    )
    if created:
        view = create_beneficiary(session, owner, **body)
        store_response(
            session,
            row,
            response_snapshot=_minimal_idempotency_snapshot(
                beneficiary_id=view["id"], status=view.get("status", "ACTIVE")
            ),
            transaction_id=view["id"],
        )
        return view, False
    stored = find_key(session, key=key, user_id=owner)
    if stored is None:  # pragma: no cover - carrera extrema
        raise IdempotencyConflictError("clave de idempotencia en conflicto")
    if is_expired(stored):
        renew_expired(
            session,
            stored,
            request_hash=digest,
            endpoint=IDEMPOTENCY_ENDPOINT,
            method="POST",
        )
        view = create_beneficiary(session, owner, **body)
        store_response(
            session,
            stored,
            response_snapshot=_minimal_idempotency_snapshot(
                beneficiary_id=view["id"], status=view.get("status", "ACTIVE")
            ),
            transaction_id=view["id"],
        )
        return view, False
    if stored.request_hash != digest:
        raise IdempotencyConflictError("Idempotency-Key ya usada con un cuerpo distinto")
    return _replay_idempotent_view(session, owner, stored), True


def list_beneficiaries(session: Session, owner_user_id: uuid.UUID | str) -> list[dict]:
    """Lista ACTIVE del titular (hasta 100), con cuenta enmascarada."""
    owner = _coerce_owner(owner_user_id)
    return [to_view(row) for row in beneficiaries_repo.list_active(session, owner)]


def search_beneficiaries(
    session: Session, owner_user_id: uuid.UUID | str, query: str | None
) -> list[dict]:
    """Busqueda en tiempo real por alias/nombre (CA-02): `q` por tokens
    indexados (AND), hasta 100, `< 1 s`; `q` vacio = lista. Sin descifrar
    para filtrar (el descifrado es solo presentacion al titular)."""
    owner = _coerce_owner(owner_user_id)
    if query is None or not str(query).strip():
        return list_beneficiaries(session, owner)
    digests = crypto.query_token_hashes(query)
    if not digests:
        return []
    rows = beneficiaries_repo.search_active(session, owner, digests)
    return [to_view(row) for row in rows]


def update_beneficiary(
    session: Session,
    owner_user_id: uuid.UUID | str,
    beneficiary_id: uuid.UUID | str,
    **changes: object,
) -> dict:
    """Edicion parcial (CA-03/CA-04): solo el titular; re-cifra y re-indexa
    lo que cambie; `None` limpia opcionales (`photo_url`, `holder_name`,
    `holder_doc`, `preapproved_limit_minor`, `currency`, `bank_code`)."""
    owner = _coerce_owner(owner_user_id)
    row = _require_owned_active(session, owner, beneficiary_id)

    if "alias" in changes and changes["alias"] is not None:
        clean_alias = _require_alias(changes["alias"])
        alias_hash = crypto.alias_index(clean_alias)
        dup = beneficiaries_repo.find_active_duplicate_alias(
            session, owner, alias_hash, exclude_id=row.id
        )
        if dup is not None:
            raise DuplicateBeneficiaryError("alias ya existe para el titular")
        row.alias_enc = crypto.encrypt_value(clean_alias)
        row.alias_search_hash = alias_hash
    if "account_or_cci" in changes and changes["account_or_cci"] is not None:
        digits = normalize_account_or_cci(changes["account_or_cci"])
        account_hash = crypto.account_index(digits)
        dup = beneficiaries_repo.find_active_duplicate_account(
            session, owner, account_hash, exclude_id=row.id
        )
        if dup is not None:
            raise DuplicateBeneficiaryError("cuenta/CCI ya existe para el titular")
        row.account_enc = crypto.encrypt_value(digits)
        row.account_hash = account_hash
        row.account_last4 = digits[-4:]
    if "photo_url" in changes:
        row.photo_url = _clean_photo_url(changes["photo_url"])
    if "bank_code" in changes:
        row.bank_code = _clean_bank_code(changes["bank_code"])
    if "holder_name" in changes:
        name = _clean_holder_name(changes["holder_name"])
        row.holder_name_enc = crypto.encrypt_value(name) if name else None
        row.name_search_hash = crypto.name_index(name) if name else None
    if "holder_doc" in changes:
        doc = _clean_holder_doc(changes["holder_doc"])
        row.holder_doc_hash = crypto.blind_index(doc, domain="doc") if doc else None
    if "preapproved_limit_minor" in changes:
        row.preapproved_limit_minor = _clean_limit(changes["preapproved_limit_minor"])
    if "currency" in changes:
        row.currency = _clean_currency(changes["currency"])
    session.flush()

    current_alias = crypto.decrypt_value(row.alias_enc)
    current_name = crypto.decrypt_value(row.holder_name_enc) if row.holder_name_enc else None
    beneficiaries_repo.sync_tokens(
        session, row, crypto.token_hashes_for(current_alias, current_name)
    )
    _record_saved_event(session, row, event="updated")
    return to_view(row)


def delete_beneficiary(
    session: Session,
    owner_user_id: uuid.UUID | str,
    beneficiary_id: uuid.UUID | str,
    *,
    confirm: bool = False,
) -> dict:
    """Soft-delete (`status=DELETED`, CA-04): exige `confirm=True`
    (confirmacion de seguridad); no borra fisicamente (trazabilidad)."""
    if confirm is not True:
        raise ValueError("la eliminacion exige confirmacion de seguridad (confirm=true)")
    owner = _coerce_owner(owner_user_id)
    row = _require_owned_active(session, owner, beneficiary_id)
    beneficiaries_repo.soft_delete(session, row)
    _record_saved_event(session, row, event="deleted")
    return to_view(row)


def get_for_operation(
    session: Session, owner_user_id: uuid.UUID | str, beneficiary_id: uuid.UUID | str
) -> dict:
    """Fachada interna de lectura para el motor (E2-T14, D-01): retorna la
    cuenta/CCI y el nombre en claro SIN pasar por la API. Solo lectura
    (sin `flush`/`commit`). El llamante jamas lo registra en logs ni lo
    expone en respuestas (regla de oro 7/9). `None` si no existe/ajeno
    seria oraculo: aqui se distingue (404/403 los traduce el llamante)."""
    owner = _coerce_owner(owner_user_id)
    row = _require_owned_active(session, owner, beneficiary_id)
    return {
        "beneficiary_id": row.id,
        "owner_user_id": row.owner_user_id,
        "account_or_cci": crypto.decrypt_value(row.account_enc),
        "holder_name": crypto.decrypt_value(row.holder_name_enc) if row.holder_name_enc else None,
        "bank_code": row.bank_code,
        "currency": row.currency,
        "preapproved_limit_minor": (
            int(row.preapproved_limit_minor) if row.preapproved_limit_minor is not None else None
        ),
        "status": row.status,
    }


__all__ = [
    "BENEFICIARY_SAVED_EVENT",
    "IDEMPOTENCY_ENDPOINT",
    "BeneficiaryForbiddenError",
    "BeneficiaryNotFoundError",
    "DuplicateBeneficiaryError",
    "IdempotencyConflictError",
    "create_beneficiary",
    "create_beneficiary_idempotent",
    "delete_beneficiary",
    "get_for_operation",
    "list_beneficiaries",
    "normalize_account_or_cci",
    "search_beneficiaries",
    "to_view",
    "update_beneficiary",
]
