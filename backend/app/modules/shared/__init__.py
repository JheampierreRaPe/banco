"""Modulo transversal `shared` (infraestructura, E5-T05, HU17).

Dueno de `shared.outbox` y `shared.processed_events` (inbox).
Ver `docs/modules/README.md#shared--config-infraestructura`.

Prohibido: guardar logica de negocio aqui. Solo persistencia del outbox,
consumo idempotente y publicacion diferida via worker.
"""

from app.modules.shared.models import OutboxEntry, ProcessedEvent

__all__ = ["OutboxEntry", "ProcessedEvent"]
