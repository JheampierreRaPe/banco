"""Nombre del dueno del QR enmascarado (E4-T24, HU14/HU13, solo backend).

- Camino feliz: `mask_payee_name` puro (`Diego Gonzales` ->
  `DIEG••• GONZ•••`); `payee_name_masked` presente en
  `get_personal_charge_status` (dueno ve su QR personal) y en
  `preview_qr_charge` personal y de comercio (el pagador ve a quien
  paga; `business_name` intacto en comercio).
- Tolerancia: fachada caida / usuario sin fila / perfil vacio ->
  `payee_name_masked` en `None` sin romper; QR ajeno sigue `404`
  neutro.
- Sin PII: el nombre completo jamas aparece en la respuesta (barrido);
  solo la mascara. Sin BD real: `profile_resolver` inyectado y
  repositorios/dominio stubbeados (la fachada real es import perezoso,
  D-03). Sin `float`, sin secretos en logs.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.modules.wallet.domain import masking as masking_domain
from app.modules.wallet.domain import qr as qr_domain
from app.modules.wallet.domain import qr_payment as qr_payment_domain
from app.modules.wallet.repository import qr as qr_repo
from app.modules.wallet.schemas.payment import QrPreviewResponse
from app.modules.wallet.schemas.personal_qr import PersonalQrStatusResponse
from app.modules.wallet.service import qr as qr_service
from app.modules.wallet.service import qr_payment as qr_payment_service

OWNER = uuid.uuid4()
MERCHANT_OWNER = uuid.uuid4()
CHARGE_ID = uuid.uuid4()
MERCHANT_ID = uuid.uuid4()

DIEGO = {"first_name": "Diego", "last_name": "Gonzales", "business_name": None}
ROSA = {"first_name": "Rosa", "last_name": "Quispe Mamani", "business_name": None}
EMPRESA = {"first_name": "", "last_name": "", "business_name": "EMPRESA SAC"}


def _resolver_for(profile):
    def _resolve(session, user_id):
        assert str(user_id)  # el servicio pasa el `user_id` resuelto
        return dict(profile)

    return _resolve


def _failing_resolver(session, user_id):
    raise RuntimeError("fachada caida")


# ------------------------------------------------------- formato (CA-01)


@pytest.mark.parametrize(
    ("first", "last", "business", "expected"),
    [
        ("Diego", "Gonzales", None, "DIEG••• GONZ•••"),
        ("Diego", "Gonzales Perez", None, "DIEG••• GONZ•••"),  # primer apellido
        ("  diego  ", "  gonzales  ", None, "DIEG••• GONZ•••"),  # trim + upper
        ("Ana", "Lo", None, "ANA LO"),  # parte <= 4 completa
        ("Ana", None, None, "ANA"),  # sin apellido
        (None, None, "EMPRESA SAC", "EMPR•••"),  # razon social: una parte
        (None, None, "SAC", "SAC"),  # razon social corta completa
        ("Diego", "Gonzales", "EMPRESA SAC", "DIEG••• GONZ•••"),  # nombre manda
        ("José", "Ñaupari", None, "JOSÉ ÑAUP•••"),  # acentos se conservan
        ("", "", "", None),  # vacio -> fallback
        ("   ", None, None, None),
        (None, None, None, None),
    ],
)
def test_mask_payee_name_formato_acordado(first, last, business, expected):
    """CA-01: primeros 4 + `•••` por parte, `<= 4` completa, espacio separador."""
    assert masking_domain.mask_payee_name(first, last, business) == expected


def test_mask_part_unidad():
    """`mask_part`: mayusculas, `<= 4` completa, resto oculto."""
    assert masking_domain.mask_part("abcd") == "ABCD"
    assert masking_domain.mask_part("abcde") == "ABCD•••"
    assert masking_domain.mask_part("  ") == ""


def test_resolver_fachada_caida_da_none():
    """CA-04: fachada caida / perfil vacio / no-dict -> `None` sin romper."""
    assert (
        masking_domain.resolve_payee_name_masked(
            object(), OWNER, profile_resolver=_failing_resolver
        )
        is None
    )
    assert (
        masking_domain.resolve_payee_name_masked(
            object(), OWNER, profile_resolver=_resolver_for({})
        )
        is None
    )
    assert (
        masking_domain.resolve_payee_name_masked(
            object(), OWNER, profile_resolver=lambda s, u: ["no", "dict"]
        )
        is None
    )


def test_resolver_ok_enmascara_nombre_natural():
    """El resolver aplica la mascara al perfil de la fachada."""
    assert (
        masking_domain.resolve_payee_name_masked(
            object(), OWNER, profile_resolver=_resolver_for(DIEGO)
        )
        == "DIEG••• GONZ•••"
    )
    assert (
        masking_domain.resolve_payee_name_masked(
            object(), OWNER, profile_resolver=_resolver_for(EMPRESA)
        )
        == "EMPR•••"
    )


# --------------------------------- estado del QR personal (CA-03, CA-04)


def _personal_row(**overrides):
    base = {
        "id": CHARGE_ID,
        "payee_type": "PERSONAL",
        "owner_user_id": OWNER,
        "currency": "PEN",
        "concept": None,
        "status": "ACTIVE",
        "paid_at": None,
        "signature": "00" * 32,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_get_personal_charge_status_expone_mascara(monkeypatch):
    """CA-03: `GET /wallet/qr/{id}` trae `payee_name_masked` del titular."""
    monkeypatch.setattr(qr_repo, "get_qr_charge", lambda session, cid: _personal_row())
    view = qr_service.get_personal_charge_status(
        object(), user_id=OWNER, charge_id=CHARGE_ID, profile_resolver=_resolver_for(DIEGO)
    )
    assert view["payee_name_masked"] == "DIEG••• GONZ•••"
    assert view["payee_type"] == "PERSONAL" and view["reusable"] is True
    PersonalQrStatusResponse(data=view, meta={})  # contrato OpenAPI valida
    dumped = json.dumps(view, default=str, ensure_ascii=False)
    assert "DIEG••• GONZ•••" in dumped
    assert "Diego" not in dumped and "Gonzales" not in dumped  # CA-02: sin fuga


def test_get_personal_charge_status_sin_nombre_da_none(monkeypatch):
    """CA-04: sin nombre resoluble -> `None` sin romper el estado."""
    monkeypatch.setattr(qr_repo, "get_qr_charge", lambda session, cid: _personal_row())
    view = qr_service.get_personal_charge_status(
        object(), user_id=OWNER, charge_id=CHARGE_ID, profile_resolver=_failing_resolver
    )
    assert view["payee_name_masked"] is None
    assert view["status"] == "ACTIVE"
    PersonalQrStatusResponse(data=view, meta={})


def test_get_personal_charge_status_ajeno_sigue_404(monkeypatch):
    """QR ajeno/de comercio/inexistente -> `404` neutro (sin oraculo)."""
    monkeypatch.setattr(qr_repo, "get_qr_charge", lambda session, cid: None)
    with pytest.raises(qr_service.QrNotFoundError):
        qr_service.get_personal_charge_status(
            object(),
            user_id=OWNER,
            charge_id=CHARGE_ID,
            profile_resolver=_resolver_for(DIEGO),
        )


# ------------------------------------------------- preview (CA-03, CA-05)


def _stub_preview_common(monkeypatch, charge):
    monkeypatch.setattr(
        qr_payment_domain, "decode_qr_content", lambda token: SimpleNamespace(ref=str(CHARGE_ID))
    )
    monkeypatch.setattr(qr_repo, "get_qr_charge", lambda session, cid: charge)
    monkeypatch.setattr(qr_payment_domain, "verify_charge_integrity", lambda *a, **k: None)
    monkeypatch.setattr(qr_payment_domain, "ensure_charge_payable", lambda *a, **k: None)


def test_preview_personal_expone_mascara(monkeypatch):
    """CA-03: el pagador ve a quien paga en el QR personal."""
    charge = SimpleNamespace(
        id=CHARGE_ID,
        payee_type="PERSONAL",
        owner_user_id=OWNER,
        currency="PEN",
        status="ACTIVE",
    )
    _stub_preview_common(monkeypatch, charge)
    monkeypatch.setattr(
        qr_payment_service,
        "resolve_personal_settlement_account",
        lambda session, ch, cur: uuid.uuid4(),
    )
    view = qr_payment_service.preview_qr_charge(
        object(), qr_content="BO-QR1.x", profile_resolver=_resolver_for(DIEGO)
    )
    assert view["payee"] == {"owner_user_id": str(OWNER)}
    assert view["merchant"] is None
    assert view["payee_name_masked"] == "DIEG••• GONZ•••"
    QrPreviewResponse(data=view, meta={})
    dumped = json.dumps(view, default=str)
    assert "Diego" not in dumped and "Gonzales" not in dumped  # CA-02


def test_preview_comercio_conserva_business_name_y_agrega_mascara(monkeypatch):
    """CA-05: comercio conserva `business_name` + `payee_name_masked` aditivo."""
    charge = SimpleNamespace(
        id=CHARGE_ID,
        payee_type="MERCHANT",
        merchant_id=MERCHANT_ID,
        amount_minor=5_000,
        currency="PEN",
        expires_at=None,
        status="ACTIVE",
    )
    merchant = SimpleNamespace(
        id=MERCHANT_ID,
        business_name="Bodega Central",
        status="ACTIVE",
        user_id=MERCHANT_OWNER,
    )
    _stub_preview_common(monkeypatch, charge)
    monkeypatch.setattr(qr_repo, "get_merchant", lambda session, mid: merchant)
    view = qr_payment_service.preview_qr_charge(
        object(), qr_content="BO-QR1.x", profile_resolver=_resolver_for(ROSA)
    )
    assert view["merchant"] == {"merchant_id": str(MERCHANT_ID), "business_name": "Bodega Central"}
    assert view["payee"] is None
    assert view["payee_name_masked"] == "ROSA QUIS•••"
    QrPreviewResponse(data=view, meta={})
    dumped = json.dumps(view, default=str, ensure_ascii=False)
    assert "Rosa" not in dumped and "Quispe" not in dumped  # CA-02


def test_preview_sin_nombre_da_none(monkeypatch):
    """CA-04: preview personal sin nombre resoluble -> `None` sin romper."""
    charge = SimpleNamespace(
        id=CHARGE_ID,
        payee_type="PERSONAL",
        owner_user_id=OWNER,
        currency="PEN",
        status="ACTIVE",
    )
    _stub_preview_common(monkeypatch, charge)
    monkeypatch.setattr(
        qr_payment_service,
        "resolve_personal_settlement_account",
        lambda session, ch, cur: uuid.uuid4(),
    )
    view = qr_payment_service.preview_qr_charge(
        object(), qr_content="BO-QR1.x", profile_resolver=_failing_resolver
    )
    assert view["payee_name_masked"] is None
    assert view["payee"] == {"owner_user_id": str(OWNER)}
    QrPreviewResponse(data=view, meta={})


# ------------------------------------------------- contrato (CA-06)


def _data_properties(response_cls):
    """Propiedades del `data` resolviendo el `$ref` de Pydantic v2."""
    schema = response_cls.model_json_schema()
    data_schema = schema["properties"]["data"]
    ref = data_schema.get("$ref", "")
    if ref.startswith("#/$defs/"):
        return schema["$defs"][ref[len("#/$defs/") :]]["properties"]
    return data_schema.get("properties", {})


def test_contrato_openapi_expone_payee_name_masked():
    """El contrato genera `payee_name_masked` (nullable) en ambos esquemas."""
    assert "payee_name_masked" in _data_properties(PersonalQrStatusResponse)
    assert "payee_name_masked" in _data_properties(QrPreviewResponse)
    # Aditivo: los payloads viejos (sin el campo) siguen validando.
    legacy_status = {
        "charge_id": str(CHARGE_ID),
        "qr_content": "BO-QR1.x",
        "status": "ACTIVE",
        "amount_minor": None,
        "currency": "PEN",
        "expires_at": None,
        "payee_type": "PERSONAL",
        "reusable": True,
    }
    assert PersonalQrStatusResponse(data=legacy_status, meta={}).data.payee_name_masked is None
    legacy_preview = {
        "charge_id": CHARGE_ID,
        "merchant": None,
        "payee": {"owner_user_id": OWNER},
        "amount_minor": None,
        "currency": "PEN",
        "expires_at": None,
        "status": "ACTIVE",
    }
    assert QrPreviewResponse(data=legacy_preview, meta={}).data.payee_name_masked is None
    assert qr_domain is not None and qr_payment_domain is not None  # frontera: sin tocar dominio
