# Contrato de modulos (fronteras y responsabilidades)

Cada modulo es un *bounded context*. Un agente lee **solo la seccion de su modulo**. Regla
transversal: ningun modulo accede a tablas de otro; se usan fachadas (`service/`), adaptadores o
eventos (`outbox`/`inbox`).

## `identity`

- **Responsabilidad:** registro, KYC (HU01), OTP, autenticacion, recuperacion, usuarios, roles.
- **Dueno de:** `users`, `credentials`, `device_bindings`, `kyc_verifications`, `otp_codes`,
  `sessions`, `roles`, `user_roles`, `access_recovery`.
- **Expone:** `/auth/*`, `/me`; eventos `kyc.completed`, `user.activated`, `auth.login_succeeded`,
  `auth.failed_attempt`.
- **KYC (HU01):** proxy al microservicio: `challenge`, `evaluate` (paso en vivo con rafaga
  `frames_b64`, E1-T29) y `submit` (varios frames por tarea o `image_b64`; propaga `failed_step`,
  `step_results`, `overall_reason`). `/api/v1/document/validate` valida la legibilidad del documento
   (base64 -> `file` multipart; `{is_valid, issues, checks}`, E1-T30). No persiste frames ni
   imagenes; `verify-full` y `/api/v1/document/validate` con timeout dedicado
   (`KYC_VERIFY_TIMEOUT_SECONDS`, `KYC_DOCUMENT_TIMEOUT_SECONDS`).
- **Titular por documento (E1-T35, HU01):** `POST /auth/kyc/document/lookup
  {type: DNI|RUC, number}` -> `{document_type, first_name, last_name,
  business_name}` (persona natural: nombres/apellidos; RUC juridica: razon
   social en `business_name`; consume `F-T44`). Proxy server-side a apiinti
   (`GET /dni/{numero}`, `GET /ruc/{numero}` con `Authorization: Bearer
   <APIINTI_API_KEY>`; parseo tolerante en un unico punto; errores neutros
   `VALIDATION_ERROR`/`DOCUMENT_NOT_FOUND`/`DOC_LOOKUP_UNAVAILABLE`/
   `RATE_LIMITED`; sin persistencia; rate limit en memoria compartido con KYC).
   Precheck "documento ya registrado" (E1-T37, DNI y RUC): despues del 422 y
   antes de la red, `document_is_registered` (HMAC de
   `kyc_onboarding.hash_document_number` + `get_by_doc_hash`) -> `409
   DUPLICATE_DOCUMENT` (`"El documento ya se encuentra registrado"`) sin
   consulta externa; el 409 de `submit` queda intacto.
- **Alta con RUC (E1-T36, HU01):** `POST /auth/kyc/submit` acepta
  `document.type=RUC` con `applicant.business_name` (razon social, max 150;
  se persiste en `identity.users.business_name` via `create_user`/
  `onboard_customer`/`persist_kyc_submission`; RUC juridica = nombres `""`,
  RUC natural = nombres completos). Validacion por tipo ANTES del proveedor
  (`kyc_proxy.validate_applicant` -> `422 VALIDATION_ERROR` sin invocar al
  microservicio); `DNI|CE|PASSPORT` intactos (`business_name` se ignora).
   CHECK `ck_users_doc_type` = `('DNI','CE','PASSPORT','RUC')` (migracion
   `0020`). `kyc-service/` intacto.
- **Consentimiento biometrico (E1-T38, HU02/HU03):** `POST /auth/pin/setup`
  acepta `biometric_enabled?: bool = false` (default preserva el contrato) y
  lo persiste en `identity.credentials.biometric_enabled` junto a
  `pin_hash` + `ACTIVE`; `POST /auth/login/facial` lo exige (`is True`)
  ademas de binding `ACTIVE` + firma valida y, sin el, responde `400
  INVALID_LOGIN` generico (sin sesion ni tocar el binding; sin codigo nuevo
   de error). La biometria es opcional: el PIN es siempre el fallback.
- **Consentimiento post-login (E1-T39, HU02/HU03):** `POST
  /auth/biometric/consent` (Bearer, `{enabled: bool}`) fija
  `credentials.biometric_enabled` del `user_id` del JWT (idempotente;
  `401 NOT_AUTHENTICATED` / `404 NOT_FOUND` neutro; auditoria
  `auth.biometric_consent` sin PII) y `POST /auth/login/pin` devuelve
   `biometric_enabled` vigente en su `data` para sincronizar el boton
  biometrico del cliente (el facial sigue intacto).
- **Rebind de clave en login con PIN (E1-T42, HU03/HU04):** `POST
  /auth/login/pin` con `device_id` + `device_public_key` reescribe el
  `public_key` del binding existente con la clave vigente (una sola fila,
  `last_used_at` refrescado, `status` preservado: un `REVOKED` no se reactiva
  por PIN; el alta sigue `ACTIVE`), best-effort en savepoint con auditoria
  `auth.device_binding` (`registered`/`touched`/`failed`); la clave se persiste
  tal cual (`hmac:`/PEM); sin cambio de request/response (el facial posterior
  verifica contra la clave vigente con el unico `400 INVALID_LOGIN` generico).
- **Consume:** `risk.alert.raised` (bloqueo), `notifications` (OTP).
- **OTP de activacion (E1-T32/SCR-005):** solo por `email` (`users.email`,
  plantilla `otp_code_email`); sin email no hay entrega y nunca SMS (entrega
  best-effort). `POST /auth/activate` deprecado pero vivo (`Deprecation: true` +
  OpenAPI `deprecated`; la via canonica es `POST /auth/pin/setup`).
- **OTP de recuperacion (E1-T33, HU04; E1-T41 retira `verify`):** `POST
  /auth/recovery/request {email}` (siempre 200 sin enumerar; cooldown
  reutilizando el `PENDING` vigente; rate-limit por `email+IP`). `POST
  /auth/recovery/verify` fue ELIMINADO por decision del dueno (la UI
  "recupera mi acceso" redirigia a login sin dar acceso); se conservan
  `request`, `service/recovery.py`, `repository/recovery.py` y la tabla
  `access_recovery` porque `POST /auth/pin-reset` depende de ellos
  (consume el OTP `RECOVERY` y registra `access_recovery` con
  `new_credential_set=true`).
   Solo canal `email` (nunca SMS); errores `RATE_LIMITED`
   (ventana por `email+IP`; la ventana de verificacion se conserva y rige
   a `/auth/pin-reset`).
- **Reseteo de PIN (E1-T34/SCR-005, HU02/HU04):** `POST /auth/pin-reset
  {email, doc_number, [doc_type,] code, pin}` (consume el OTP `RECOVERY`, valida el documento
  contra el hash HMAC server-side, fija `pin_hash` + resetea
  `failed_attempts`/`locked_until`, registra `access_recovery` con
  `new_credential_set=true`; devuelve `{user_ref, pin_set: true}` SIN abrir
  sesion; la unica sesion la abre `POST /auth/login/pin`). Error unico
  `INVALID_PIN_RESET` (mismo 401 generico para email/DNI/OTP invalidos: sin
  enumeracion) / `RATE_LIMITED` (ventana por `email+IP`, reutiliza la de
  verify); consumen `F-T34`..`F-T42`. E1-T40: `doc_type` (`DNI`/`RUC`,
  default `DNI` por compatibilidad F-T43) valida solo formato/longitud
  (DNI 8 / RUC 11 digitos, 422 estandar; paridad con el lookup E1-T35); la
  resolucion sigue por `email` + `doc_number_hash` (sin cruzar con
  `users.doc_type`).
- **Precheck de email (E1-T40, HU01):** `POST /auth/kyc/email/check
  {email}` (pre-registro, sin auth) para el boton **Continuar** de
  `kyc-start`: `200 {available: true}` si no esta registrado, `409
  DUPLICATE_EMAIL` (`"El correo ya esta registrado"`, neutro) si existe.
  Normaliza (`strip().lower()`) y lee via `get_by_email`; rate-limit previo
  (ventana compartida con el proxy KYC); nunca llama al proveedor ni
  persiste; el 409 de `submit` queda intacto.
- **`users.email` NOT NULL (E1-T34):** migracion `0018` (con pre-check que
  aborta ante NULL; sin backfill: la demo tenia 0 NULL); el OTP solo viaja
  por email.
- **Parametros en `config.parameters` (E1-T34, regla de oro 6):**
  `auth.lockout_seconds` (900), `auth.max_failed_attempts` (5),
  `otp.resend_wait_seconds` (30), `otp.max_attempts` (3),
  `auth.recovery_request_window_seconds`/`auth.recovery_request_max_requests`
  (60/10) y `auth.recovery_verify_window_seconds`/
  `auth.recovery_verify_max_requests` (60/10) —migracion `0019`, lectura
  best-effort con fallback a las constantes—; rigen `pin_login` (lockout),
  `otp_service` (reenvio/intentos) y `recovery`/`pin-reset` (rate-limits).
- **Login en dispositivo nuevo, paso 1 (E1-T45, HU03/HU04):** `POST
  /auth/login/device/request {email, doc_type: DNI|RUC, document_number}` (pre-sesion,
  sin `userRef` local) verifica que email y documento son de la MISMA cuenta
  `ACTIVE` (hash HMAC + `hmac.compare_digest`, sin cruzar `users.doc_type`) y
  emite OTP proposito `LOGIN` solo por email (`otp_code_email`, best-effort;
  cooldown reutiliza el `PENDING`); siempre 200 identico (anti-enumeracion),
  `429 RATE_LIMITED` por `email+IP` ANTES de la existencia reutilizando la
  ventana `auth.recovery_verify_*` con scope `"device_login"` (sin claves
  nuevas); auditoria `auth.device_login_requested` sin PII; el paso 2 es
   `E1-T46`; sin migracion (el proposito `LOGIN` ya existe).
- **Login en dispositivo nuevo, paso 2 (E1-T46, HU03/HU04):** `POST
  /auth/login/device/complete {email, doc_type: DNI|RUC, document_number, code,
  pin, device_id, device_public_key[, platform, biometric_type]}` (pre-sesion)
  valida OTP `LOGIN` + PIN en la MISMA transaccion (**PIN primero**: un OTP
  valido con PIN erroneo no se consume; el fallo solo mueve el lockout del PIN,
  5 fallos -> `423 ACCOUNT_LOCKED`), registra/actualiza el binding best-effort
  en savepoint (alta `ACTIVE`; existente preserva `status`: un `REVOKED` no
  revive pero la sesion abre igual, decision del dueno) y abre sesion
  (`{access_token, refresh_token, session_id, user_ref, biometric_enabled}`);
  unico `401 INVALID_LOGIN` generico (sin enumeracion), `429 RATE_LIMITED`
  compartido con el paso 1 (ventana `auth.recovery_verify_*`, scope
  `"device_login"`); PIN `4-6` digitos (patron `pin_reset`, `422` sin oraculo);
  auditoria `auth.device_login` + `auth.device_binding` sin PII; evento
  `auth.login_succeeded` via outbox; sin migracion.
- **Perfil minimo para saludo/avatar (E1-T44, HU01/HU02):** `GET /api/v1/me`
  (Bearer) devuelve solo el nombre del titular del token (`first_name`,
  `last_name`, `business_name`/razon social; RUC juridica = nombres `""` +
  razon social, el resto `business_name` `NULL`); jamas documento, correo,
  telefono ni hashes (`401 NOT_AUTHENTICATED` / `404 NOT_FOUND` neutro; solo
  lectura, sin migracion; el cliente solo presenta).
- **Puede llamar a:** adaptador `KycProvider`, `notifications`, `audit`.
- **Prohibido:** tocar cuentas, ledger o transacciones.

## `accounts`

- **Responsabilidad:** cuentas, saldos (proyeccion), beneficiarios, consolidado y movimientos.
- **Dueno de:** `accounts`, `account_balances`, `beneficiaries`, `movements_view`,
  `daily_balance_snapshots`.
- **Expone:** `/accounts*`, `/beneficiaries*`; eventos `account.created`, `beneficiary.saved`.
- **Total consolidado (E1-T43, HU05):** `GET /api/v1/accounts/totals` (Bearer)
  devuelve el total contable consolidado por moneda calculado en el servidor
  desde la proyeccion `account_balances` (`total_minor = Σ(available + held)`
  por moneda, misma fuente que `GET /accounts`; `primary_currency = "PEN"`,
  `as_of = max(updated_at)`/`now(UTC)`); el cliente solo muestra el monto
  (cliente delgado). Solo lectura, sin migracion.
- **Consume:** `ledger.entry.posted` (para proyectar movimientos), `kyc.completed`.
- **Puede llamar a:** `ledger` (crear subcuentas), `audit`.
- **Prohibido:** escribir asientos; calcular saldos por fuera del ledger.

## `transactions`

- **Responsabilidad:** motor transaccional, holds, idempotencia, estados, transferencias, QR/pagos.
- **Dueno de:** `transactions`, `transaction_status_history`, `holds`.
- **Expone:** `/transfers/*`, `/payments/*`, `/transactions/*`; eventos `transaction.initiated`,
  `funds.held`, `funds.released`, `transfer.settled`.
- **Consume:** `fraud.alert.raised` (bloqueo), `account.created`.
- **Puede llamar a:** `accounts` (validar/actualizar saldos), `ledger` (asientos), `risk`, `fx`,
  `wallet`, `notifications`, `audit`.
- **Prohibido:** crear asientos por su cuenta; escribir tablas de otros modulos.

## `ledger`

- **Responsabilidad:** asientos de partida doble, balances, cierre diario, hash encadenado.
- **Dueno de:** `ledger_accounts`, `journal_entries`, `postings`, `ledger_balances`, `daily_closings`.
- **Expone (fachada interna):** `post_entry(...)`, `reverse_entry(...)`, `run_daily_closing()`;
  eventos `ledger.entry.posted`.
- **Consume:** nada (es el registro final).
- **Puede llamar a:** `audit`.
- **Prohibido:** llamar a modulos de negocio; los asientos solo entran por su fachada.

## `credits`

- **Responsabilidad:** simulacion, solicitud, evaluacion, contrato, desembolso y cuotas.
- **Dueno de:** `loan_products`, `loan_applications`, `loans`, `loan_schedules`, `loan_payments`,
  `contracts`.
- **Expone:** `/credits/*`; eventos `credit.approved`, `credit.disbursed`, `loan.installment.paid`.
- **Consume:** `bureau.score.received` (adaptador).
- **Puede llamar a:** `transactions` (desembolso/cuota), `identity` (firma), `risk`, `notifications`,
  `audit`.
- **Prohibido:** acreditar dinero sin pasar por el motor.

## `wallet`

- **Responsabilidad:** billetera, vinculos, QR, servicios y recargas.
- **Dueno de:** `wallets`, `wallet_links`, `merchant_accounts`, `qr_charges`, `qr_payments`,
  `billers`, `service_payments`, `topups`, `service_subscriptions`.
- **Expone:** `/wallet*`, `/merchants/qr*`, `/services/*`, `/topups`; eventos
  `qr.charge.created`, `qr.payment.confirmed`, `service.paid`.
- **Consume:** `transfer.settled`.
- **Puede llamar a:** `transactions`, `accounts`, `notifications`, `audit`.
- **Prohibido:** debitar directamente sin el motor.

## `fx`

- **Responsabilidad:** tipos de cambio, cotizaciones, compra/venta, bolsillos y metas de ahorro.
- **Dueno de:** `fx_rates`, `fx_quotes`, `fx_operations`, `savings_pockets`, `pocket_movements`.
- **Expone:** `/fx/*`, `/pockets*`; eventos `fx.executed`, `pocket.transferred`, `pocket.yield.paid`.
- **Consume:** nada critico.
- **Puede llamar a:** adaptador `FxRateProvider`, `transactions`, `ledger`, `audit`.
- **Prohibido:** asentar un cambio cruzando monedas en un solo asiento.

## `risk`

- **Responsabilidad:** reglas antifraude, perfiles, alertas, listas, ROS.
- **Dueno de:** `risk_rules`, `risk_profiles`, `alerts`, `screening_results`, `ros_reports`,
  `blocked_entities`.
- **Expone:** `/risk/*`, `/compliance/*`; eventos `fraud.alert.raised`, `screening.hit`,
  `ros.filed`.
- **Consume:** `transaction.initiated`, `transfer.settled`, `kyc.completed`, `auth.failed_attempt`.
- **Puede llamar a:** adaptador `SanctionsLists`, `transactions` (bloqueo), `notifications`, `audit`.
- **Prohibido:** mover dinero.

## `reconciliation`

- **Responsabilidad:** clearing, cruce, excepciones, reclamos y ajustes.
- **Dueno de:** `clearing_files`, `clearing_items`, `reconciliation_runs`,
  `reconciliation_exceptions`, `claims`.
- **Expone:** `/reconciliation/*`; eventos `reconciliation.exception`, `claim.resolved`.
- **Consume:** `transfer.settled`, `ledger.entry.posted`.
- **Puede llamar a:** adaptador `ClearingSource`, `ledger` (asiento de ajuste), `audit`.
- **Prohibido:** editar el asiento original; todo ajuste es un asiento nuevo.

## `notifications`

- **Responsabilidad:** push, correo, SMS, plantillas y preferencias.
- **Dueno de:** `notification_templates`, `notifications`, `user_channel_preferences`.
- **Expone:** fachada `send(...)`; eventos `notification.sent`, `notification.failed`.
- **Consume:** practicamente todos los eventos de negocio.
- **Puede llamar a:** adaptador de notificaciones; `audit`.
- **Prohibido:** depender de modulos de negocio.

## `audit`

- **Responsabilidad:** bitacora append-only con hash encadenado y verificacion.
- **Dueno de:** `audit_log`, `audit_verifications`.
- **Expone:** `/audit/logs` (solo lectura, rol Auditor); fachada `record(...)`.
- **Consume:** todos los eventos sensibles.
- **Prohibido:** modificar/eliminar registros; ser llamado dentro de transacciones de dinero si
  bloquea el flujo (usar outbox).

## `admin`

- **Responsabilidad:** gestion de usuarios internos, roles y permisos; administra parametros via fachada (sin ser dueno del esquema).
- **Dueno de:** `role_permissions` (el esquema y ciclo de vida de `config.parameters` es de `shared`/`config`; ver abajo).
- **Expone:** `/admin/*` (incluye `GET/PUT /admin/parameters` como fachada de lectura/escritura auditada).
- **Prohibido:** cambiar reglas sin dejar auditoria.

## `shared` / `config` (infraestructura)

- **Responsabilidad:** `outbox`, `processed_events` (inbox), `idempotency_keys`, `parameters`.
  **Decision (2026-09-25):** `shared`/`config` es la UNICA fuente duena del esquema y ciclo de
  vida de `config.parameters` (definicion, migraciones, lectura best-effort con fallback);
  `admin` solo lo expone via `/admin/parameters` con auditoria.
- **Uso:** cualquier modulo publica por `outbox`; consume registrando en `processed_events`;
  el middleware de dinero usa `idempotency_keys`.
- **Prohibido:** guardar logica de negocio aqui.
