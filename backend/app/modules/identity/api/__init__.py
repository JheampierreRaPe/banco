"""Router del modulo `identity` (E1-T02: proxy KYC pre-registro; E1-T10: activacion OTP;
E1-T13: login con dispositivo; E1-T14: login con PIN; E1-T15: sesiones/refresh/logout;
E1-T31: recuperacion de acceso por email con OTP; E1-T34: reseteo de PIN con email+DNI+OTP).

Combina de forma aditiva los routers de `api/kyc.py` (intacto),
`api/activation.py` (intacto), `api/device_login.py` (intacto),
`api/pin_login.py` (intacto), `api/pin_setup.py` (fijado inicial del PIN,
addendum E1-T14), `api/recovery.py` (recuperacion por email, E1-T33),
`api/pin_reset.py` (reseteo de PIN con email+DNI+OTP, E1-T34) y
`api/sessions.py` para el ensamblado del
monolito (`app.main` via `iter_routers`, prefijo `/api/v1`).
"""

from fastapi import APIRouter

from app.modules.identity.api.activation import router as activation_router
from app.modules.identity.api.device_login import router as device_login_router
from app.modules.identity.api.kyc import router as kyc_router
from app.modules.identity.api.pin_login import router as pin_login_router
from app.modules.identity.api.pin_reset import router as pin_reset_router
from app.modules.identity.api.pin_setup import router as pin_setup_router
from app.modules.identity.api.recovery import router as recovery_router
from app.modules.identity.api.sessions import router as sessions_router

router = APIRouter()
router.include_router(kyc_router)
router.include_router(activation_router)
router.include_router(device_login_router)
router.include_router(pin_login_router)
router.include_router(pin_setup_router)
router.include_router(recovery_router)
router.include_router(pin_reset_router)
router.include_router(sessions_router)

__all__ = ["router"]
