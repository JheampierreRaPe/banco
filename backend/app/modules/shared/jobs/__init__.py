"""Jobs del modulo `shared`: publicacion diferida del outbox."""

from app.modules.shared.jobs.publisher import publish_pending

__all__ = ["publish_pending"]
