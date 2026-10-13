"""Verificacion del PIN del titular para autorizar pagos (E2-T31, HU08).

Fachada de `identity` que la variante PIN del `authorize` usa por D-03
(mismo rol que `device_login.verify_signature`/`get_device_binding` para
la firma): verifica un PIN en claro contra `credentials.pin_hash` del
titular con comparacion en tiempo constante (`pin_login.verify_pin`).

Solo lectura/verificacion: no expone modelos, no toca los contadores del
login (`failed_attempts`/`locked_until` son del inicio de sesion, no del
reto de pago) y no emite eventos. El anti-brute-force del pago vive en el
bloque de la transaccion (mismo contador/bloqueo preventivo CA-02 que la
firma, en el motor de `transactions`).

Sin credencial o sin PIN -> comparacion ciega contra un hash ficticio y
`False` (mismo costo criptografico, sin enumeracion ni oraculo de
tiempos). Entradas malformadas -> `False` generico (sin excepciones).
Sin secretos ni PII en logs (jamas el PIN ni su hash). `flush` sin
`commit` (solo lecturas; quien llama decide la transaccion).
"""

from __future__ import annotations

import logging
import secrets
import uuid

from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo
from app.modules.identity.service import pin_login as pin_login_service

logger = logging.getLogger(__name__)

#: Hash ficticio para la rama ciega (titular sin credencial o sin PIN):
#: se verifica contra el para igualar el costo PBKDF2 y no filtrar por
#: timing (mismo patron que `pin_login._DUMMY_HASH`).
_DUMMY_HASH = pin_login_service.hash_pin(secrets.token_hex(8))


def verify_holder_pin(session: Session, user_id: uuid.UUID | str | None, pin: object) -> bool:
    """Verifica el PIN del titular (`True` solo si coincide).

    `user_id` es el titular de la transaccion (`initiator_user_id`);
    `pin` es el presentado en el `authorize`. Cualquier desajuste
    (titular malformado, sin credencial, sin PIN, formato desconocido)
    responde `False` tras la comparacion ciega: el llamante responde el
    generico `403 NOT_AUTHORIZED` sin distinguir el motivo.
    """
    candidate = pin if isinstance(pin, str) else ""
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError):
        pin_login_service.verify_pin(candidate, _DUMMY_HASH)
        return False
    try:
        credential = identity_repo.get_credential(session, uid)
    except Exception:  # noqa: BLE001 - sin acceso rige el generico
        credential = None
    stored = credential.pin_hash if credential is not None and credential.pin_hash else None
    if not stored:
        pin_login_service.verify_pin(candidate, _DUMMY_HASH)
        return False
    try:
        return bool(pin_login_service.verify_pin(candidate, stored))
    except Exception:  # noqa: BLE001 - cualquier fallo cripto es generico
        return False


__all__ = ["verify_holder_pin"]
