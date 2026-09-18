"""Router del modulo `identity` (E1-T02: proxy KYC pre-registro; E1-T10: activacion OTP;
E1-T13: login con dispositivo; E1-T14: login con PIN; E1-T15: sesiones/refresh/logout).

Combina de forma aditiva los routers de `api/kyc.py` (intacto),
`api/activation.py` (intacto), `api/device_login.py` (intacto),
`api/pin_login.py` (intacto), `api/pin_setup.py` (fijado inicial del PIN,
addendum E1-T14) y `api/sessions.py` para el ensamblado del
monolito (`app.main` via `iter_routers`, prefijo `/api/v1`).
"""

from fastapi import APIRouter

from app.modules.identity.api.activation import router as activation_router
from app.modules.identity.api.device_login import router as device_login_router
from app.modules.identity.api.kyc import router as kyc_router
from app.modules.identity.api.pin_login import router as pin_login_router
from app.modules.identity.api.pin_setup import router as pin_setup_router
from app.modules.identity.api.sessions import router as sessions_router

router = APIRouter()
router.include_router(kyc_router)
router.include_router(activation_router)
router.include_router(device_login_router)
router.include_router(pin_login_router)
router.include_router(pin_setup_router)
router.include_router(sessions_router)

__all__ = ["router"]
