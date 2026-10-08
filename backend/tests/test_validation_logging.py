"""Observabilidad 422: log seguro sin PII + contrato `{"detail"}` intacto.

Cubre `POST /transfers/third-party` con body invalido:
- el handler de `RequestValidationError` registra `loc` + `type` (sin `input`)
- la respuesta mantiene el formato FastAPI `{"detail": [...]}` (no envelope).
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.transactions.api import get_current_user_id


@pytest.fixture()
def auth_client() -> TestClient:
    """Cliente con auth falsa: la validacion del body llega a 422 de esquema."""

    def _override_user() -> uuid.UUID:
        return uuid.uuid4()

    app.dependency_overrides[get_current_user_id] = _override_user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)


def test_third_party_invalid_body_logs_fields_and_keeps_detail_contract(
    auth_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        response = auth_client.post("/api/v1/transfers/third-party", json={})
    assert response.status_code == 422
    payload = response.json()
    # Contrato previo: 422 de esquema FastAPI con `detail` (no envelope `error`).
    assert "detail" in payload
    assert "error" not in payload
    assert isinstance(payload["detail"], list) and payload["detail"]

    assert "validation_error" in caplog.text
    assert "source_account_id" in caplog.text
    assert "amount_minor" in caplog.text
    # El motivo viaja como `type` (p. ej. `missing`), nunca el cuerpo crudo.
    assert "missing" in caplog.text


def test_validation_log_excludes_raw_input_values(
    auth_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel_uuid = "SENTINEL-PII-XYZ-999"
    sentinel_target = "OTRO-SENTINEL-ABC-123"
    body = {
        "source_account_id": sentinel_uuid,
        "target_account_id": sentinel_target,
        "amount_minor": -5,
        "currency": "pen",
    }
    with caplog.at_level(logging.WARNING):
        response = auth_client.post("/api/v1/transfers/third-party", json=body)
    assert response.status_code == 422
    assert "detail" in response.json()

    # Campos y tipos si se registran; valores crudos jamas (regla 7: sin PII).
    assert "source_account_id" in caplog.text
    assert "amount_minor" in caplog.text
    assert sentinel_uuid not in caplog.text
    assert sentinel_target not in caplog.text
