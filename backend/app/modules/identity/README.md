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
   `DUPLICATE_DOCUMENT`/`DUPLICATE_EMAIL`. E1-T36: el submit acepta
   `document.type=RUC` con `applicant.business_name` (razon social, max 150;
   se persiste en `users.business_name`; RUC juridica = nombres `""`) con
   validacion por tipo ANTES del proveedor (`validate_applicant` -> 422 sin
   invocar al microservicio).
- `POST /auth/kyc/document/lookup` (`api/kyc.py` + `service/document_lookup.py` +
  adaptador `DocumentLookupProvider`, E1-T35): titular por DNI/RUC via apiinti
  (`GET /dni/{numero}`, `GET /ruc/{numero}` con `Authorization: Bearer
  <APIINTI_API_KEY>` server-side; la key nunca sale al cliente); responde
  `{document_type, first_name, last_name, business_name}` (RUC juridica =
   razon social; consume `F-T44`); errores neutros `VALIDATION_ERROR` (422),
   `DUPLICATE_DOCUMENT` (409, E1-T37: precheck en BD con el HMAC de
   `kyc_onboarding.hash_document_number` despues del 422 y antes de la red,
   DNI y RUC, sin consulta externa; el 409 de `submit` queda intacto),
  `DOCUMENT_NOT_FOUND` (404), `DOC_LOOKUP_UNAVAILABLE` (503/504),
  `RATE_LIMITED` (429); sin persistencia.
- `POST /auth/activate` y `POST /auth/otp/resend` (`api/activation.py`,
  E1-T32/SCR-005): OTP de activacion solo por email (plantilla `otp_code_email`;
  nunca SMS); `/auth/activate` deprecado pero vivo (`Deprecation: true` + OpenAPI
  `deprecated`; la via canonica es `POST /auth/pin/setup`).
- `POST /auth/recovery/request` y `POST /auth/recovery/verify` (`api/recovery.py`,
  E1-T33): recuperacion pre-sesion por email (OTP `RECOVERY` solo por email;
  `verify` valida el OTP SIN abrir sesion y devuelve `{user_ref, device_bound}`,
  con binding del dispositivo nuevo y fila `access_recovery` `method='OTP'`;
  la unica sesion la abre `POST /auth/login/pin`; consume `F-T29`).
- `POST /auth/pin-reset` (`api/pin_reset.py`, E1-T34/SCR-005): reseteo de PIN
  con `email + DNI + OTP` (`{email, doc_number, code, pin}` -> `{user_ref,
  pin_set: true}`); consume el OTP `RECOVERY`, valida el DNI contra el hash
  HMAC server-side (nunca en claro), fija `pin_hash` + resetea
  `failed_attempts`/`locked_until`, registra `access_recovery` con
  `new_credential_set=true`; anti-enumeracion con un unico `401
  INVALID_PIN_RESET` generico; rate-limit por `email+IP` (ventana de verify);
  NO abre sesion (la unica la abre `POST /auth/login/pin`); consumen
  `F-T34`..`F-T42`.
- Login biometrico/PIN, sesiones y recuperacion (`api/device_login.py`,
  `api/pin_login.py`, `api/pin_setup.py`, `api/sessions.py`). E1-T38:
  `POST /auth/pin/setup` acepta `biometric_enabled?: bool = false` y lo
  persiste en `credentials.biometric_enabled` junto a `pin_hash` + `ACTIVE`;
  `POST /auth/login/facial` (`login_with_device`) lo exige (`is True`) antes
  del binding/firma y, sin consentimiento, responde `400 INVALID_LOGIN`
  generico (sin sesion ni tocar el binding; auditoria `auth.failed_attempt`
  con `reason="no_consent"`).

## Casos de uso (`service/`)

- `onboard_customer` (`service/__init__.py`, E1-T03): en una transaccion crea
  `User` + `Credential` + cuenta/subcuentas via fachadas `accounts`/`ledger`,
  `kyc.completed` via `outbox` y el OTP inicial `ACTIVATION`.
- `persist_kyc_submission` (`service/kyc_onboarding.py`, E1-T24): hashea el
  documento server-side (`doc_number_hash`/`doc_number_masked`), valida
  duplicados, orquesta alta + verificacion y guarda el intento fallido sin
  crear usuario. E1-T36: propaga `business_name` al alta.
- `kyc_proxy` (`service/kyc_proxy.py`, E1-T02): valida imagenes (base64 +
  magic bytes) y reenvia al adaptador; nunca persiste frames. E1-T36:
  `DOC_TYPES` incluye `RUC` y `validate_applicant` valida al titular por
  tipo antes del proveedor (422).
- `document_lookup` (`service/document_lookup.py`, E1-T35): valida
   `type in {DNI,RUC}` + `number` solo digitos (DNI=8/RUC=11) antes de la red,
   consulta al adaptador `DocumentLookupProvider` y normaliza
   `first_name`/`last_name` (persona natural) o `business_name` (razon social,
   RUC juridica); sin persistencia ni eventos; numero solo hasheado en logs.
   E1-T37: `DocumentAlreadyRegisteredError` + `document_is_registered`
   (HMAC del alta + `get_by_doc_hash`) con seam `is_registered` en
   `lookup_holder` (precheck 409 antes de la red, DNI y RUC).
- OTP, activacion, login y sesiones en sus propios modulos de servicio.
- `reset_pin` (`service/pin_reset.py`, E1-T34/SCR-005): fija el PIN con
  `email + DNI + OTP RECOVERY` (un solo 401 generico `INVALID_PIN_RESET`,
  rate-limit por `email+IP` con la ventana de verify, `access_recovery` con
  `new_credential_set=true`, auditoria `auth.pin_reset`; sin sesion).
- Parametros en `config.parameters` (E1-T34, regla de oro 6): `pin_login`
  lee `auth.lockout_seconds`/`auth.max_failed_attempts`, `otp_service`
  `otp.resend_wait_seconds`/`otp.max_attempts` (+ `otp.ttl_seconds`/
  `otp.max_resends` ya parametrizados) y `recovery`/`pin_reset`
  `auth.recovery_request_*`/`auth.recovery_verify_*`; lectura best-effort
  (SQL directo en SAVEPOINT via `otp_service.read_int_parameter`) con
  fallback a las constantes. `users.email` es NOT NULL (migracion `0018`;
  coherente con OTP solo email).

## Reglas

- `flush` sin `commit`: el endpoint confirma en exito y revierte ante fallo
  (atomicidad todo-o-nada).
- Sin PII ni frames/secretos en logs; el numero de documento solo viaja
  hasheado/enmascarado y el OTP jamas se refleja en respuestas.
- Fachadas de otros modulos por import perezoso (`accounts`, `ledger`,
  `outbox`, `audit`, `notifications`); no se tocan sus tablas.
