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
- **Consume:** `risk.alert.raised` (bloqueo), `notifications` (OTP).
- **OTP de activacion (E1-T32/SCR-005):** solo por `email` (`users.email`,
  plantilla `otp_code_email`); sin email no hay entrega y nunca SMS (entrega
  best-effort). `POST /auth/activate` deprecado pero vivo (`Deprecation: true` +
  OpenAPI `deprecated`; la via canonica es `POST /auth/pin/setup`).
- **OTP de recuperacion (E1-T33, HU04):** `POST /auth/recovery/request {email}` (siempre
  200 sin enumerar; cooldown reutilizando el `PENDING` vigente; rate-limit por `email+IP`)
  y `POST /auth/recovery/verify {email, code[, device_id/device_public_key]}` (valida
  el OTP SIN abrir sesion y devuelve `{user_ref, device_bound}`; registra
  `device_bindings` y `access_recovery method='OTP'`; la unica sesion la abre
  `POST /auth/login/pin`).
   Solo canal `email` (nunca SMS); errores `INVALID_RECOVERY_CODE` (mismo 401
   generico tambien para OTP vencido/bloqueado: sin oraculo) / `RATE_LIMITED`
   (solo ventana por `email+IP`); consume `F-T29`.
- **Reseteo de PIN (E1-T34/SCR-005, HU02/HU04):** `POST /auth/pin-reset
  {email, doc_number, code, pin}` (consume el OTP `RECOVERY`, valida el DNI
  contra el hash HMAC server-side, fija `pin_hash` + resetea
  `failed_attempts`/`locked_until`, registra `access_recovery` con
  `new_credential_set=true`; devuelve `{user_ref, pin_set: true}` SIN abrir
  sesion; la unica sesion la abre `POST /auth/login/pin`). Error unico
  `INVALID_PIN_RESET` (mismo 401 generico para email/DNI/OTP invalidos: sin
  enumeracion) / `RATE_LIMITED` (ventana por `email+IP`, reutiliza la de
  verify); consumen `F-T34`..`F-T42`.
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
- **Puede llamar a:** adaptador `KycProvider`, `notifications`, `audit`.
- **Prohibido:** tocar cuentas, ledger o transacciones.

## `accounts`

- **Responsabilidad:** cuentas, saldos (proyeccion), beneficiarios, consolidado y movimientos.
- **Dueno de:** `accounts`, `account_balances`, `beneficiaries`, `movements_view`,
  `daily_balance_snapshots`.
- **Expone:** `/accounts*`, `/beneficiaries*`; eventos `account.created`, `beneficiary.saved`.
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

- **Responsabilidad:** gestion de usuarios internos, roles, permisos y parametros.
- **Dueno de:** `parameters` (schema `config`), `role_permissions`.
- **Expone:** `/admin/*`.
- **Prohibido:** cambiar reglas sin dejar auditoria.

## `shared` / `config` (infraestructura)

- **Responsabilidad:** `outbox`, `processed_events` (inbox), `idempotency_keys`, `parameters`.
- **Uso:** cualquier modulo publica por `outbox`; consume registrando en `processed_events`;
  el middleware de dinero usa `idempotency_keys`.
- **Prohibido:** guardar logica de negocio aqui.
