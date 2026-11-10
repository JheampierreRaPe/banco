"""Router del modulo `admin` (fachada de administracion).

Ensambla los routers de fachada del modulo (montados bajo `/api/v1` por
`app.main` via `iter_routers`, sin registro extra):

- `api/parameters.py` (E2-T26): `GET/PUT /admin/parameters` como fachada
  auditada sobre `shared/config` (dueno de `config.parameters`).
"""

from fastapi import APIRouter

from app.modules.admin.api.parameters import router as parameters_router

router = APIRouter(tags=["admin"])
router.include_router(parameters_router)

__all__ = ["router"]
