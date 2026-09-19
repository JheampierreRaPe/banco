# Modulo identity

Contrato y fronteras: ver `docs/modules/README.md#identity`.

Responsabilidad: registro, KYC (HU01/HU02), OTP, autenticacion, recuperacion,
usuarios, roles. Dueno de `users`, `credentials`, `kyc_verifications`,
`otp_codes`, `device_bindings`, `sessions`, `roles`, `user_roles`,
`access_recovery`.

## Endpoints

- `POST /auth/kyc/challenge` y `POST /auth/kyc/submit` (`api/kyc.py`):
  proxy al microservicio KYC. El submit (E1-T24) recibe `document.number`,
  `applicant{first_name,last_name,email,phone}` y, si `overall_result=true`,
  integra en la misma transaccion el alta (`onboard_customer`) y la
  persistencia del intento (`save_verification`); devuelve
  `user_id`/`status`/`account_id`. Duplicados de documento/email -> 409
  `DUPLICATE_DOCUMENT`/`DUPLICATE_EMAIL`.
- `POST /auth/activate` y `POST /auth/otp/resend` (`api/activation.py`).
- Login biometrico/PIN, sesiones y recuperacion (`api/device_login.py`,
  `api/pin_login.py`, `api/pin_setup.py`, `api/sessions.py`).

## Casos de uso (`service/`)

- `onboard_customer` (`service/__init__.py`, E1-T03): en una transaccion crea
  `User` + `Credential` + cuenta/subcuentas via fachadas `accounts`/`ledger`,
  `kyc.completed` via `outbox` y el OTP inicial `ACTIVATION`.
- `persist_kyc_submission` (`service/kyc_onboarding.py`, E1-T24): hashea el
  documento server-side (`doc_number_hash`/`doc_number_masked`), valida
  duplicados, orquesta alta + verificacion y guarda el intento fallido sin
  crear usuario.
- `kyc_proxy` (`service/kyc_proxy.py`, E1-T02): valida imagenes (base64 +
  magic bytes) y reenvia al adaptador; nunca persiste frames.
- OTP, activacion, login y sesiones en sus propios modulos de servicio.

## Reglas

- `flush` sin `commit`: el endpoint confirma en exito y revierte ante fallo
  (atomicidad todo-o-nada).
- Sin PII ni frames/secretos en logs; el numero de documento solo viaja
  hasheado/enmascarado y el OTP jamas se refleja en respuestas.
- Fachadas de otros modulos por import perezoso (`accounts`, `ledger`,
  `outbox`, `audit`, `notifications`); no se tocan sus tablas.
