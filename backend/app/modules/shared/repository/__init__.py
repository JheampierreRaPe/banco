"""Repositorio del modulo `shared`: outbox + inbox idempotente + idempotencia."""

from app.modules.shared.repository.outbox import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_CLAIM_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    claim_entry,
    compute_backoff_seconds,
    insert_event,
    is_processed,
    list_pending,
    mark_failed,
    mark_published,
    try_mark_processed,
)
from app.modules.shared.repository.idempotency import (
    DEFAULT_TTL_SECONDS,
    TTL_ENV_VAR,
    compute_request_hash,
    find_key,
    is_expired,
    renew_expired,
    resolve_ttl_seconds,
    store_response,
    try_claim,
)

__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TTL_SECONDS",
    "TTL_ENV_VAR",
    "claim_entry",
    "compute_backoff_seconds",
    "compute_request_hash",
    "find_key",
    "insert_event",
    "is_expired",
    "is_processed",
    "list_pending",
    "mark_failed",
    "mark_published",
    "renew_expired",
    "resolve_ttl_seconds",
    "store_response",
    "try_claim",
    "try_mark_processed",
]
