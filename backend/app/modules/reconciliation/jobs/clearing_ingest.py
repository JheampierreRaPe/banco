"""Job de ingesta de lotes de compensacion (E5-T17, HU19 CA-04).

`run_clearing_ingest_once(session, source=None, ...)`: una pasada que
consulta el adaptador `ClearingSource` (`E5-T16`), delega cada lote en
el servicio `ingest_clearing_file` y hace `commit` por archivo. No mueve
dinero (solo inserta `clearing_files`/`clearing_items` y avanza
`RECEIVED -> PROCESSED` / `FAILED`); no emite eventos.

Gestion de transaccion (patron `E3-T19`): el llamante pasa la sesion y
cada archivo confirma con su propio `commit`; el fallo de un archivo
no tumba la pasada:
- `VALIDATION_ERROR` del servicio -> la fila `FAILED` ya quedo en
  `flush`: se confirma con `commit` (evidencia conservada) y el
  archivo se reporta en `failed`;
- fallo tecnico/adaptador (`ClearingSourceError`, red) -> `rollback`
  del intento y el archivo se reporta en `skipped` (reintentable);
- cualquier otro fallo inesperado -> `rollback` y `skipped`.

Idempotente y re-ejecutable: repetir la pasada hace replay (mismo
`file_ref` + mismo checksum, sin duplicar). Sin PII en logs (regla 7):
solo conteos y hashes cortos (nunca `file_ref`/`external_ref` en
claro). El `source`/`limit` son inyectables (tests deterministas; en
produccion el `runner` de `app.composition` pasa el reloj real).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.modules.reconciliation import repository as recon_repo
from app.modules.reconciliation import service as recon_service

logger = logging.getLogger(__name__)

#: Tope de archivos por pasada (acota el lote; el siguiente ciclo retoma).
DEFAULT_LIMIT = 100


def _hash(value: object) -> str:
    return recon_repo.hash_file_ref(value)


def run_clearing_ingest_once(
    session: Session,
    *,
    source: object | None = None,
    limit: int = DEFAULT_LIMIT,
    create_source: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Una pasada de ingesta: adapta lotes y los persiste (`commit` c/u).

    Retorna `{"processed": [...], "failed": [...], "skipped": [...],
    "error": None | <code>}` (refs de archivo; los logs solo llevan
    hashes y conteos). `create_source` solo existe para inyeccion en
    pruebas (por defecto, la fabrica `create_clearing_source` por
    entorno, `mock` sin red).
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise recon_repo.ReconciliationValidationError("limite invalido")
    if create_source is None:
        from app.adapters.clearing_source import create_clearing_source

        factory: Callable[[], Any] = create_clearing_source
    else:
        factory = create_source
    try:
        clearing = factory()
    except Exception as exc:  # noqa: BLE001 - la fabrica no debe tumbar la pasada
        logger.warning("clearing-ingest fabrica fallo error=%s", type(exc).__name__)
        return {"processed": [], "failed": [], "skipped": [], "error": "SOURCE_UNAVAILABLE"}

    try:
        files = clearing.fetch_files(source=source)
    except Exception as exc:  # noqa: BLE001 - caida del proveedor: pasada vacia
        code = getattr(exc, "code", type(exc).__name__)
        logger.warning("clearing-ingest fetch fallo code=%s", code)
        return {"processed": [], "failed": [], "skipped": [], "error": code}

    processed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    for entry in list(files)[:limit]:
        file_ref = entry.file_ref if hasattr(entry, "file_ref") else None
        try:
            items = clearing.fetch_items(entry.file_ref)
        except Exception as exc:  # noqa: BLE001 - lote no disponible: se omite
            logger.warning(
                "clearing-ingest lote omitido file_hash=%s code=%s",
                _hash(file_ref),
                getattr(exc, "code", type(exc).__name__),
            )
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            skipped.append(entry.file_ref)
            continue
        payload = {
            "source": entry.source,
            "file_ref": entry.file_ref,
            "checksum": entry.checksum,
            "item_count": entry.item_count,
            "items": [
                {
                    "external_ref": item.external_ref,
                    "amount_minor": item.amount_minor,
                    "currency": item.currency,
                    "value_date": item.value_date,
                    "direction": item.direction,
                    "raw": dict(item.raw) if item.raw is not None else None,
                }
                for item in items
            ],
        }
        try:
            recon_service.ingest_clearing_file(session, payload)
            session.commit()
        except recon_repo.ReconciliationValidationError:
            # La fila `FAILED` ya quedo en `flush`: se confirma para
            # conservar la evidencia y la pasada continua.
            try:
                session.commit()
            except Exception:  # noqa: BLE001 - si el commit falla, se revierte
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                    pass
                skipped.append(entry.file_ref)
                continue
            failed.append(entry.file_ref)
            continue
        except Exception as exc:  # noqa: BLE001 - fallo tecnico: el lote continua
            logger.warning(
                "clearing-ingest item fallo file_hash=%s error=%s",
                _hash(file_ref),
                type(exc).__name__,
            )
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - limpieza best-effort
                pass
            skipped.append(entry.file_ref)
            continue
        processed.append(entry.file_ref)
    logger.info(
        "clearing-ingest pasada processed=%d failed=%d skipped=%d",
        len(processed),
        len(failed),
        len(skipped),
    )
    return {"processed": processed, "failed": failed, "skipped": skipped, "error": None}


__all__ = ["DEFAULT_LIMIT", "run_clearing_ingest_once"]
