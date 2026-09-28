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
- **Fachada de firma/binding para el motor (E2-T17, D-03):** `identity.service.device_login`
  expone `verify_signature(nonce, signature, public_key) -> bool` (ya existia para el login
  facial) y `get_device_binding(session, user_id, device_id) -> {device_id, public_key,
  status} | None` (lectura nueva, solo lectura). Las consume `transactions` para autorizar
  pagos sensibles **sin importar modelos de `identity`**; el `public_key` se trata como
  material secreto (R26) y cualquier ausencia/desajuste se responde generico (sin oraculo).
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
- **Fachada de lectura para el motor (E2-T07, D-01):** `get_account(session,
  account_id) -> {account_id, user_id, status, currency, account_number} | None`
  (solo lectura; `None` si no existe, `ValueError` si el id no es UUID). La usan
  `transfer_own`/`transfer_third_party` para validar propiedad/estado/moneda sin
  importar modelos de `accounts`. El `account_number` en claro es interno (jamas en
  logs ni respuestas; la API solo expone UUIDs).
- **Beneficiarios (E2-T13, HU07):** `GET /api/v1/beneficiaries` (lista `ACTIVE` + `?q=` de
  busqueda indexada, hasta 100, `< 1 s`), `POST` (alta; `Idempotency-Key` opcional respetada),
  `PUT /{id}` (edicion parcial incl. `preapproved_limit_minor`) y `DELETE /{id}?confirm=true`
  (soft-delete `status=DELETED`, exige confirmacion). Cifrado en reposo AES-256-GCM
  (`alias_enc`/`account_enc`/`holder_name_enc`; sobres versionados `B1`, fallback stdlib `B0`
  solo en `local`/tests) y `holder_doc_hash` como HMAC; la cuenta/CCI jamas viaja en claro (la
  API expone `account_masked`, `****1234`). Busqueda por indice ciego
  (`alias_search_hash`/`name_search_hash`/`account_hash` + invertido
  `beneficiary_search_tokens` por palabra/prefijos, AND) sin descifrar filas. Aislamiento por
  `owner_user_id` (del JWT, sin FK fisica); unicidad de duplicados `ACTIVE` por UNIQUE parciales
  `WHERE status='ACTIVE'` (migraciones `0021`/`0022`); evento `beneficiary.saved` por `outbox`
  (payload sin PII). Errores `DUPLICATE_BENEFICIARY` (409), `IDEMPOTENCY_CONFLICT` (409),
  `VALIDATION_ERROR` (400) / `NOT_FOUND` (404) / `NOT_AUTHORIZED` (403). Fachada interna
  `get_for_operation` entrega el claro **solo** al motor (`E2-T14`), nunca la API. Fuera de
  `local` el arranque exige `cryptography` + `BENEFICIARY_CRYPTO_KEY`/`BENEFICIARY_INDEX_PEPPER`
  (fail-fast).
- **Cableado E2-T23:** `accounts.events.on_ledger_entry_posted(entry)` (sesion/`commit`
  propios, `event_id = entry.id`) suscrito al bus via
  `app.composition.subscribe_accounts_movements`; alimenta solo `movements_view`
  (lectura). `handle_ledger_entry_posted` (retenidos) NO se suscribe al bus:
  `held` lo sincroniza `AccountsLedgerProjectionAdapter` en la transaccion del
  asiento y suscribirlo sin reconciliar duplicaria el efecto.
- **Puede llamar a:** `ledger` (crear subcuentas), `audit`.
- **Prohibido:** escribir asientos; calcular saldos por fuera del ledger.

## `transactions`

- **Responsabilidad:** motor transaccional, holds, idempotencia, estados, transferencias, QR/pagos.
- **Dueno de:** `transactions`, `transaction_status_history`, `holds`.
- **Expone:** `/transfers/*`, `/payments/*`, `/transactions/*`; eventos `transaction.initiated`,
  `funds.held`, `funds.released`, `transfer.settled`.
- **Transferencias (E2-T07, HU06):** `transfer_own` (ruta atomica SIN hold:
  un asiento `OWN_TRANSFER` `2000-S -> 2000-D`, sin comision, `AUTHORIZED ->
  POSTED -> SETTLED`) y `transfer_third_party` (hold + liquidacion; comision a
  **`4100-Ingresos por comisiones`**, default desde `config.parameters`
  `transfer.third_party_fee_minor`, fallback `0`). Endpoints
  `POST /transfers/own` y `POST /transfers/third-party` (`Idempotency-Key`
  obligatoria; insuficiente -> `201` con `status=REJECTED` +
  `failure_code=INSUFFICIENT_FUNDS`, sin tocar saldos). Validacion de cuentas
  solo por la fachada `accounts.service.get_account` (D-01).
- **Saga interbancaria (E2-T09, HU06):** `POST /transfers/interbank` (`202` +
  `tracking`) y `GET /transactions/{id}` (detalle + `tracking` + `history`);
  maquina `FUNDS_HELD -> POSTED -> SETTLED` con compensacion a `FAILED`
  (timeout/rechazo) o `REVERSED` (asiento inverso + `INTERBANK_RETURN`). El
  adaptador `InterbankGateway` (mock determinista, simulador `D01`) envia/consulta
  y el job `interbank_poll` (`poll_interbank_once`) avanza el lote. Un hold por
  transaccion SIN `expires_at` (el vencimiento lo gobierna la saga via
  `meta.expires_at`, no `release_expired_holds`); la comision se carga a
  **`4100` solo al liquidar** y `2200-Transito` cuadra a cero. El titular/CCI se
  valida por la fachada `validate_holder` (adaptador `HolderLookup`, E2-T08) antes
  de mover dinero.
- **Comprobante (E2-T10, HU06 CA-04):** `GET /transactions/{id}/receipt` (Bearer, solo
  lectura) devuelve `application/pdf` con numero de operacion unico derivado determinista del
  `transaction_id` (`OP-` + 12 hex de `sha256(str(id))`; estable entre reintentos, no
  secuencial/adivinable, no persistido), fecha/hora UTC, tipo, estado, montos en centimos,
  moneda y cuentas enmascaradas (`****1234`; interbancaria sin CCI en claro). Solo emitible en
  `POSTED`/`SETTLED`/`CONCILIATED` (otro estado -> `409 VALIDATION_ERROR`); sin `reportlab` ->
  `501 RECEIPT_FORMAT_NOT_SUPPORTED` (patron `E2-T03`). No crea asientos ni filas.
- **Autorizacion biometrica de pagos sensibles (E2-T17, HU08):** gate en el motor
  (no endpoint propio; habilita `POST /transactions/{id}/authorize`, `E2-T19`). Si el
  **acumulado del dia** del titular (`amount_minor + fee_minor` de estados no
  `REJECTED`/`FAILED`, mas la operacion en curso) supera
  `transfer.biometric_threshold_minor` (fachada `shared.config`, E2-T18), la operacion
  queda en **`PENDING_AUTHORIZATION`** con un `nonce` de un solo uso (TTL 120 s) en
  `transactions.metadata` (nunca material biometrico) a firmar con el dispositivo; en
  rutas con hold se crea la retencion antes de debitar y la ruta atomica propia
  (`04#7.1`) no retiene nada. La firma/binding se verifican por **fachada de `identity`**
  (D-03: `device_login.verify_signature` + `device_login.get_device_binding`); con firma
  valida avanza `AUTHORIZED` y continua (`FUNDS_HELD -> POSTED -> SETTLED`) reutilizando el
  hold sin duplicar asiento (replay por digest de firma). **CA-02** bloqueo preventivo:
  tras `auth.max_failed_attempts` fallos libera el hold, deja `REJECTED` con
  `BIOMETRIC_PREVENTIVE_BLOCK` y emite `auth.failed_attempt` (`blocked:true`) para
  `risk`/`E2-T20`. **CA-03** limite diario por perfil: `acumulado + amount + fee > limite`
  -> `REJECTED` con `DAILY_LIMIT_EXCEEDED` sin hold. Todo intento fallido emite
  `auth.failed_attempt` (CA-04), sin filtrar el motivo (sin oraculo). El `risk_profile` del
  titular lo aporta el llamante (por defecto rige el generico/`STANDARD`);
  `E2-T19` lo resuelve end-to-end en el router (`resolve_risk_profile`, cierra OBS-1).
- **Tope pre-aprobado por beneficiario (E2-T14, HU07 CA-03):** si la
  transferencia lleva `beneficiary_id` (o `metadata["beneficiary_id"]`) de un
  beneficiario del titular con `preapproved_limit_minor >= amount_minor +
  fee_minor` y misma moneda, el motor omite `PENDING_AUTHORIZATION` y autoriza
  directo (`AUTHORIZED` con `PREAPPROVED_EXEMPT_REASON`); el tope se lee por la
  fachada `accounts.beneficiaries_service.get_for_operation` (sin importar sus
  modelos, D-01) y la decision viaja en `transaction.initiated`
  (`exemption_reason="PREAPPROVED_LIMIT"` + `beneficiary_id`, solo UUIDs, sin
  PII). Por encima del tope o sin beneficiario rige el gate de `E2-T17`.
- **Endpoint de autorizacion (E2-T19, HU08 CA-01):**
  `POST /transactions/{id}/authorize` (Bearer, `Idempotency-Key` obligatoria)
  recibe `{nonce, signature, device_id}`, valida titular y firma/binding por la
  fachada `identity` (D-03), consume el `nonce` de un solo uso (TTL) y continua
  `AUTHORIZED -> FUNDS_HELD -> POSTED -> SETTLED` sin duplicar asiento; estado
  distinto -> `409`, firma/ajena -> `403` generico (sin oraculo), `nonce`
  vencido/reutilizado -> `EXPIRED_NONCE`. No recalcula el umbral.
- **Listado del titular (E2-T24, HU06/HU15/HU17):** `GET /transactions`
  (Bearer, solo lectura) pagina (`page`/`page_size`, default 20, max 100) las
  operaciones del `initiator_user_id` del JWT con filtros `date_from`/
  `date_to`/`type`/`status`; cuentas origen/destino siempre enmascaradas
  (`****1234`), orden estable `created_at DESC, id`; sin PII ni numeros
  completos (G16).
- **Reverso/compensacion (E2-T25, HU06/HU17/HU18):**
  `POST /transactions/{id}/reverse` (Bearer, `Idempotency-Key` obligatoria)
  crea un **asiento inverso** solo via la fachada `ledger.service.reverse_entry`
  (referencia `reverses_entry_id`; el original y sus `postings` intactos) en
  `POSTED`/`SETTLED`/`CONCILIATED` -> `REVERSED` (G17); idempotente (servicio +
  clave) y seguro ante concurrencia (exclusion por transaccion + CAS por
  `version`); `INTERBANK_TRANSFER` delega en la saga (`E2-T09`) y emite
  `transfer.reversed` por `outbox`.
- **Consume:** `fraud.alert.raised` (bloqueo), `account.created`.
- **Puede llamar a:** `accounts` (validar/actualizar saldos), `ledger` (asientos), `risk`, `fx`,
  `wallet`, `notifications`, `audit`.
- **Prohibido:** crear asientos por su cuenta; escribir tablas de otros modulos.

## `ledger`

- **Responsabilidad:** asientos de partida doble, balances, cierre diario, hash encadenado.
- **Dueno de:** `ledger_accounts`, `journal_entries`, `postings`, `ledger_balances`, `daily_closings`.
- **Expone (fachada interna):** `post_entry(...)`, `reverse_entry(...)`, `run_daily_closing()`;
  eventos `ledger.entry.posted`.
- **Emision E2-T23:** `post_entry`/`reverse_entry` registran `ledger.entry.posted`
  en `shared.outbox` en la misma transaccion del asiento (`flush`, sin `commit`;
  payload `entry_id`, `transaction_id`, `description`, `value_date`, `postings`).
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
- **Perfil minimo (E2-T18, HU08; frontera HU20 D-16):** `risk.risk_profiles` (columnas
  `docs/03b#112`: `user_id` PK, `avg_amount_minor`, `usual_countries`, `usual_hours`,
  `last_device_id`, `score`, `updated_at`) con repositorio/servicio
  `get_or_create`/`update_behavior` idempotentes por PK (migracion `0023`); lo consulta el motor
  (`E2-T17`). El motor antifraude completo (`alerts`/`risk_rules` y la ampliacion del perfil) es
  HU20 (`E6-T03`/`E6-T04`, Sprint 4): aqui no se crean ni se pisan esas tablas.
- **Consumo de `auth.failed_attempt` (E2-T20, HU08 CA-04/CA-02):** el
  consumidor `risk.on_auth_failed_attempt` (D-04: sesion y `commit` propios,
  `event_id=entry.id`, idempotente por `processed_events` con consumidor
  `risk-auth-failures`) sube la severidad del `risk_profiles` (`score`, extra
  si `blocked`), deja auditoria (`risk.auth_failed_attempt`/
  `risk.auth_preventive_block`) y notifica al cliente por push
  (`auth_failed_attempt`/`auth_blocked`); **no mueve dinero** ni crea
  `risk.alerts` (tablas de HU20, D-16).
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
- **Plantillas de seguridad (E2-T20, HU08):** `auth_failed_attempt` (intento
  biometrico fallido) y `auth_blocked` (bloqueo preventivo), canal push,
  invocadas por la fachada `send(...)` desde el consumidor `risk` sin PII.
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
- **Fachada de parametros (E2-T26, HU08/HU20):** `GET /admin/parameters`
  (listado/consulta con filtros `module`/`prefix`) y `PUT
  /admin/parameters/{key}` (validacion de tipo) delegan **toda** la
  lectura/escritura en `shared.config` (unico dueno); rol admin exigido desde
  el JWT; cada cambio auditado (`updated_by`/`updated_at`), sin evento
  `parameter.updated` (decision E2-T18). No mueve dinero.
- **Prohibido:** cambiar reglas sin dejar auditoria; escribir
  `config.parameters` directamente (siempre via la fachada de `shared/config`).

## `shared` / `config` (infraestructura)

- **Responsabilidad:** `outbox`, `processed_events` (inbox), `idempotency_keys`, `parameters`.
  **Decision (2026-09-25):** `shared`/`config` es la UNICA fuente duena del esquema y ciclo de
  vida de `config.parameters` (definicion, migraciones, lectura best-effort con fallback);
  `admin` solo lo expone via `/admin/parameters` con auditoria.
- **Uso:** cualquier modulo publica por `outbox`; consume registrando en `processed_events`;
  el middleware de dinero usa `idempotency_keys`.
- **Fachada de parametros (E2-T18, HU08):** `shared.config.parameters`
  (`get_parameter`/`get_int_parameter`, `get_biometric_threshold_minor`,
  `get_daily_limit_minor`, `get_max_failed_attempts`, `set_parameter`) es la **unica** via de
  lectura/escritura de `config.parameters`; lectura best-effort por llamada, **sin cache en
  proceso** (un cambio se refleja de inmediato; fallback a las constantes sembradas; nunca
  lanza), escritura transaccional (`flush`), idempotente por `key` UQ y auditada
  (`updated_at`/`updated_by`, sin PII). Claves: `transfer.biometric_threshold_minor`
  (1000000), `transfer.daily_limit_minor` (500000), `transfer.daily_limit_{PERFIL}_minor`
  (`STANDARD` sembrado en `0023`) y `auth.max_failed_attempts` (5). **No emite
  `parameter.updated`** (decision E2-T18; la trazabilidad la dan las columnas). `admin` solo lo
  expone via `/admin/parameters` (`E2-T26`); `transactions`/`risk` la consumen.
- **Worker E2-T23:** `run_outbox_once` (una pasada con sesion/`commit` propios) y
  `start_outbox_worker` (hilo daemon periodico, no bloquea) en `app.composition`,
  arrancados en `app.main.lifespan`; intervalo por `OUTBOX_POLL_INTERVAL_SECONDS`,
  desactivable con `OUTBOX_WORKER_ENABLED=0`.
- **Prohibido:** guardar logica de negocio aqui.
