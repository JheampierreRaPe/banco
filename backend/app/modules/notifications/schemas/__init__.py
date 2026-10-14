"""Esquemas Pydantic del centro de notificaciones (E6-T12, HU22).

Contrato fijado (`docs/05#6.9`): el centro expone solo metadatos (sin
`payload_json` crudo ni PII). Las vistas las construye el servicio
(`service._notification_view`); aqui solo el shape de entrada/salida.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class NotificationView(BaseModel):
    """Una notificacion del titular (metadatos, sin contenido crudo).

    A2: incluye los campos de render acotados `device`/`location`/
    `reference` (etiquetas saneadas, sin PII) para que el centro muestre
    dispositivo/hora/ubicacion; `None` en filas que no los traen.
    """

    id: uuid.UUID
    channel: str
    template_code: str | None = None
    status: str
    created_at: datetime | None = None
    read_at: datetime | None = None
    device: str | None = None
    location: str | None = None
    reference: str | None = None


class NotificationListData(BaseModel):
    """Pagina del centro del titular."""

    items: list[NotificationView] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0


class ChannelPreferencesView(BaseModel):
    """Preferencias por canal del titular."""

    push: bool = True
    email: bool = True
    sms: bool = False


class ChannelPreferencesUpdate(BaseModel):
    """Actualizacion total de preferencias (los tres flags son obligatorios)."""

    push: bool
    email: bool
    sms: bool


class ReportUnrecognizedBody(BaseModel):
    """Motivo libre opcional del reporte "no reconozco" (E6-T14, HU22)."""

    reason: str | None = Field(default=None, max_length=280)


__all__ = [
    "ChannelPreferencesUpdate",
    "ChannelPreferencesView",
    "NotificationListData",
    "NotificationView",
    "ReportUnrecognizedBody",
]
