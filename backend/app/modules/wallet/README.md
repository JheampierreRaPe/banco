# Modulo wallet

Contrato y fronteras: ver `docs/modules/README.md#wallet`.

## Billetera y vinculos (E4-T01, HU13 CA-01)

- Tablas propias (`docs/03b#91`, migracion `0037_wallet`): `wallets`
  (una fila por titular, `UQ user_id`, REF logica a `identity.users`) y
  `wallet_links` (`wallet_id` FK mismo schema, `account_id` REF logica a
  `accounts`, `token_ref` UQ, `last4`, `brand`; `CHECK` sobre `VARCHAR`).
- Capas: `domain/tokenization.py` (tokenizador puro + interfaz
  `WalletTokenizationProvider` + `mock` determinista local),
  `service/tokenization.py` (fachada por entorno
  `WALLET_TOKENIZATION_PROVIDER`, default `mock`), `models/`,
  `repository/`, `service/` (casos de uso + idempotencia patron E2-T13),
  `schemas/`,   `api/` (`GET /wallet`, `POST /wallet/links`,
  `DELETE /wallet/links/{id}` -> baja logica a `INACTIVE`, solo titular,
  `404` neutro si ajeno/inexistente/ya dado de baja), `events/`
  (`wallet.link.created` por `outbox`, payload sin PII).
- Rol de comercio en `GET /wallet` (E4-T23, HU13/HU14): `get_wallet_view` +
  `WalletData` exponen `is_merchant: bool` (solo `true` con fila propia
  `ACTIVE` en `wallet.merchant_accounts`, mismo criterio que
  `require_merchant_for_user`) y `merchant_status: ACTIVE|BLOCKED|CLOSED|
  null` (estado de la fila propia; `null` sin cuenta). Solo lectura, sin
  migracion, sin PII (ni RUC ni nombre); lectura best-effort con
  fail-closed en `savepoint` (BD anterior a `0039` -> no-comercio sin
  tumbar la vista). El frontend lo usa para ocultar la entrada de
  comercio a las cuentas personales (billetera + inicio); el `403` de
  `/merchants/qr*` no cambia.
- Tokenizacion (G67): el PAN se tokeniza server-side y se descarta; en
  BD/respuestas/logs/snapshots solo `token_ref`/`last4`/`brand` y
  `account_masked` (`****1234`). Sin PII/secretos en logs (regla 7).
- La cuenta enlazada se valida solo por la fachada
  `accounts.service.get_account` (titular + `ACTIVE`; error neutro
  `404`). Sin imports de modelos de otros modulos (regla 4).
- Pruebas: `backend/tests/test_e4_t01_wallet_links.py` (tokenizacion,
  CRUD, duplicados, idempotencia, aislamiento, evento sin PII) +
  `backend/tests/test_e4_t01d_wallet_fix_m123.py` (M-1 frontera sin
  `transactions.repository`/`models`, M-2 token ajeno no reactiva,
  M-3 baja a `INACTIVE` + `404` neutro) +
  `backend/tests/test_e4_t23_wallet_merchant_flag.py` (6 pruebas E4-T23:
  flag personal/comercio `ACTIVE`/`CLOSED`/sin billetera, contrato sin PII
  y `POST /merchants/qr` del no-comercio sigue `403`).

## Pago express interno (E4-T02, HU13 CA-02/CA-04)

- Servicio interno **sin endpoint publico** (decision G66: lo consumen
  HU15/HU16): `service/express.py::pay_express` (re-exportado en
  `service/`) + regla pura `domain/limits.py::decide_express`
  (`EXPRESS`/`REQUIRES_REINFORCED`/`LIMIT_EXCEEDED`, sin `float`).
- Mueve el dinero **solo** por la fachada
  `transactions.service.transfer_third_party` (`docs/04#7.2`,
  `tx_type=THIRD_PARTY_TRANSFER`, `metadata.wallet_link_id`; lectores
  `wallet_threshold_reader`/`wallet_daily_limit_reader`): bajo el
  limite base (`wallet.express_limit_minor`, S/ 200) liquida sin reto;
  sobre el base queda `PENDING_AUTHORIZATION` (PIN/firma `E2-T19`/
  `E2-T31`, hold reutilizado); sobre el diario (`wallet.daily_limit_minor`,
  S/ 5,000) queda `REJECTED` (`DAILY_LIMIT_EXCEEDED`) sin tocar saldos.
  Semillas `0038` (idempotentes); lectura best-effort con fallback.
- `Idempotency-Key` obligatoria (replay = misma operacion; cuerpo
  distinto = `CONFLICT`, via la fachada
  `transactions.service.find_by_idempotency_key`). Solo vinculos `ACCOUNT` `ACTIVE` del titular
  (`CARD` -> `VALIDATION_ERROR`; ajeno/inexistente/inactivo ->
  `NOT_FOUND` neutro). Eventos del motor + auditoria best-effort
  `wallet.express.executed` (sin PII ni cifras, sin evento propio).
- Al autorizar una pendiente express, HU15/HU16 pasan
  `daily_limit_reader=wallet_daily_limit_reader` (re-chequeo `E2-T36`
  con el limite de billetera).
- Pruebas: `backend/tests/test_e4_t02_wallet_express.py` (regla pura,
  feliz, base exacto, refuerzo PIN con hold unico, diario,
  insuficiente, idempotencia/`CONFLICT`, tarjeta/ajeno, parametros,
  cuadre, sin PII, cadena `0038`).

Pendiente (otras tareas): UI (`E4-T04`/`E4-T09`), QA (`E4-T05`/`E4-T10`;
la QA de HU15 es `E4-T15`), endpoint `POST /payments/qr` (`E4-T12`).

## Endpoint de pago QR (E4-T12, HU15 CA-01..CA-04)

- Ruta (contrato en `docs/05#6.5`, ownership `wallet`, `docs/18`
  G74): `POST /api/v1/payments/qr` (`{qr_content, link_id}`,
  `Idempotency-Key` obligatoria, solo el titular pagador) ->
  `201 {transaction_id, qr_payment_id, charge_id, merchant
  {merchant_id, business_name}, amount_minor, currency, status
  (SETTLED/PENDING_AUTHORIZATION/REJECTED), failure_code?,
  authorization?}` + `meta.replayed`.
- Capas: `api/payments.py` (router incluido en `api/`, auth JWT
  `Bearer` propia del modulo; delega en `service.pay_qr_charge` sin
  duplicar logica) + `schemas/payment.py`. `<=` limite base ->
  `SETTLED` en un toque; `>` base -> `PENDING_AUTHORIZATION` +
  `authorization {method: "BIOMETRIC", nonce, expires_at}` (patron
  `_challenge_fields`, `E2-T19`; se confirma con
  `POST /transactions/{id}/authorize`, firma o PIN); `REJECTED` con
  `failure_code` (`DAILY_LIMIT_EXCEEDED`/`INSUFFICIENT_FUNDS`, patron
  `E2-T07`) sin tocar saldos. Finaliza el diferido con
  `finalize_qr_payment` cuando la operacion ya liquido (cargo `PAID`
  + aviso `qr_charge_paid` unico; `docs/18` G72 lado endpoint) y
  espeja a `REJECTED` si el motor rechazo. Errores: `422
  VALIDATION_ERROR` (sin clave, QR corrupto/manipulado/vencido/ajeno),
  `404 NOT_FOUND` neutro (vinculo ajeno), `409 CONFLICT` (ya pagado /
  clave con cuerpo distinto). Sin PII/secretos en logs ni respuestas;
  montos enteros.
- El re-chequeo diario al autorizar usa
  `wallet_daily_limit_reader` en el flujo de billetera (`docs/18`
  G75).
- Pruebas: `backend/tests/test_e4_t12_qr_payment_api.py` (21 pruebas:
  feliz en un toque + reto + autorizacion/finalizacion por replay,
  bloqueo sin factor, errores sin mover fondos, idempotencia,
  diario, aislamiento, contrato/OpenAPI, frontera, head `0041`).
- Preview (fix MEDIO-3, HU15 CA-01): `POST /api/v1/payments/qr/preview`
  (`{qr_content}`, Bearer, sin `Idempotency-Key`: no mueve dinero ->
  `200 {charge_id, merchant {merchant_id, business_name}, payee?,
  payee_name_masked, amount_minor, currency, expires_at, status}`;
  `422` vencido/corrupto/manipulado sin efectos, `409` ya cobrado).
  Solo lectura via `service.qr_payment.preview_qr_charge` (misma ruta
  `api/payments.py`; contrato en `docs/05#6.5`). `payee_name_masked`
  (E4-T24): nombre + un apellido del dueno enmascarado (personal: el
  titular; comercio: el dueno natural, con `business_name` intacto);
  `None` si no resoluble.

## Cobro QR por el motor (E4-T11, HU15 CA-01/CA-04)

- Tabla propia (`docs/03b#94`, migracion `0040_wallet_qr_payments`,
  `down_revision="0039_wallet_qr"`): `qr_payments`
  (`transaction_id` REF logica a `transactions` con UQ, sin FK fisica;
  `charge_id` FK mismo schema; `payer_user_id` REF logica;
  `merchant_id` FK mismo schema; `amount_minor CK>0`; `status` como
   `CHECK` sobre `VARCHAR`: `PENDING`/`PENDING_AUTHORIZATION`/`PAID`/
   `REJECTED`). Fix guarda (ALTO-2/MEDIO-2): migracion
   `0041_wallet_qr_payment_guard` (head vigente tras `E4-T16`:
   `0042_wallet_services`) reemplaza el IDX de
   `charge_id` por el unico parcial `uq_qr_payments_charge_active`
   (`status <> 'REJECTED'`).
- Capas: `domain/qr_payment.py` (decodifica el `qr_content` `BO-QR1`
  via el adaptador `E4-T03` + verifica `payload_hash`/firma HMAC/
  envelope + estado `ACTIVE`/no vencido; puro y determinista; errores
  `InvalidQrError`/`QrExpiredError`/`QrAlreadyPaidError`/
  `QrMerchantUnknownError`/`QrPaymentConflictError`/
  `QrPaymentNotFoundError` con `code` mapeable), `repository/
  qr_payment.py` (`create`/`get_by_transaction`/`get_by_charge`/
  `get_active_by_charge` (guarda: solo cobros reales/pendientes)/
  `lock_charge` (`FOR UPDATE` en Postgres)/`mark_paid`/`set_status`) + `mark_paid`/`paid_at` ya existente en
  `repository/qr.py`, `service/qr_payment.py::pay_qr_charge` (valida
  **antes de operar**: vencido/corrupto/manipulado -> rechazo sin
  retencion ni asiento; ya cobrado -> `CONFLICT`; abono = primera
  cuenta `ACTIVE` del titular comercio con la moneda del cargo por la
  fachada `accounts.service.list_active_by_user`, decision 1a del
  brief; dinero **solo** por `pay_express` con la misma
  `Idempotency-Key`; `SETTLED` -> `PAID` + cargo `PAID`/`paid_at` +
  `qr.payment.confirmed` por `outbox` sin PII + aviso `qr_charge_paid`
  best-effort; `PENDING_AUTHORIZATION` sin marcar `PAID`;
  `REJECTED` sin tocar el cargo) + `finalize_qr_payment` (tras
  autorizar con `daily_limit_reader=wallet_daily_limit_reader`, G75;
  lo invoca `E4-T12`; idempotente). `Idempotency-Key` obligatoria
  (replay = mismo pago; cuerpo distinto = `CONFLICT`).
- Evento `qr.payment.confirmed` (constante ya existente en
  `app/core/events.py`, re-exportada en `wallet/events/`).
  **No mueve dinero por su cuenta** (todo por el motor, `docs/04#7.4`
  sin comision). Sin endpoint publico (el router es `E4-T12`).
- Pruebas: `backend/tests/test_e4_t11_qr_payment.py` (22 pruebas:
  feliz con asiento cuadrado + evento + aviso; vencido/corrupto/firma
  alterada/ajeno sin mover fondos; doble cobro `CONFLICT`; replay +
  `CONFLICT`; insuficiente/`REJECTED`; refuerzo PIN + finalizacion;
  diario/`REJECTED`; comercio sin cuenta; sin PII; frontera sin tablas
  ajenas; cadena `0041`) + `backend/tests/test_e4_t11b_qr_payment_guard.py`
  (11 pruebas del fix: carrera con un solo cobro/asiento, unicidad en BD,
  reintento tras `REJECTED`, preview + endpoint, migracion `0041`
  reversible).

## QR firmado + `qr_charges` (E4-T06, HU14 CA-01/CA-03 + caducidad)

- Tablas propias (`docs/03b#92`/`#93`, migracion `0039_wallet_qr`,
  head vigente, `down_revision="0038_wallet_express_params"`):
  `merchant_accounts` (`user_id` UQ + REF logica a `identity.users`,
  `tax_id_hash` hasheado, nunca el RUC en claro) y `qr_charges`
  (`merchant_id` FK mismo schema, `amount_minor CK>0`, `mode`/`status`
  como `CHECK` sobre `VARCHAR`; indices `(merchant_id, status)` y
  `(expires_at)`). No crea `qr_payments` (es de `HU15`).
- Capas: `domain/qr.py` (payload canonico `BO-QR1` + `payload_hash`
  SHA-256 + firma **HMAC-SHA256** con secreto de servidor,
  encapsulada en `QrSigner` para migrar a `Ed25519`; `is_expired`
  con `now` inyectable; sobre interoperable via el adaptador
  `E4-T03`; `hash_tax_id` para el RUC), `models`
  (`MerchantAccount`/`QrCharge`), `repository/qr.py`
  (`create_merchant`, `create_qr_charge`, `get_qr_charge`,
  `mark_paid`/`mark_expired`/`mark_cancelled`,
  `ensure_demo_merchants`), `service/qr.py::create_qr_charge`
  (firma, `expires_at = now + wallet.qr_ttl_seconds` en `DYNAMIC`,
  `NULL` en `STATIC` sin caducidad estricta, persiste `ACTIVE` y
  emite `qr.charge.created` por `outbox` sin PII), `events/`
  (`QR_CHARGE_CREATED`).
- Decisiones (`docs/18` G68/G69/G70): HMAC-SHA256 en el MVP (simple,
  determinista, sin dependencias); comercios demo sembrados por la
  migracion (opcion (a) del brief: asumir existentes + demo);
  `STATIC` sin caducidad estricta en el MVP (`expires_at NULL`);
  TTL `wallet.qr_ttl_seconds` (default `300` s, semilla `0039`,
  lectura `get_wallet_qr_ttl_seconds` best-effort con fallback).
- **No mueve dinero** (la generacion no contabiliza; el pago es
  HU15). Sin endpoint publico (el router es `E4-T08`).
- Pruebas: `backend/tests/test_e4_t06_wallet_qr.py` (dominio,
  persistencia, TTL, demos, cadena `0039`).

## Caducidad automatica del QR (E4-T07, HU14 CA-02)

- Job `jobs/qr_expiry.py::run_qr_expiry_once(session, *, now=None,
  limit=100)`: una pasada que marca `ACTIVE` -> `EXPIRED` los
  `qr_charges` con `expires_at <= now` (borde exacto incluido;
  `STATIC` con `expires_at NULL` nunca vence), con `commit` por item
  (patron `E3-T19`) e idempotente (los `PAID`/`EXPIRED`/`CANCELLED`
  nunca se reprocesan). Selector `repository/qr.py::
  list_expired_charges` (filtro en Python via `domain.qr.is_expired`,
  robusto naive/aware). **No mueve dinero** (solo `status`); sin
  evento nuevo (basta el `status`, decision 1 del brief); sin PII en
  logs (solo conteos).
- Validacion on-read reutilizable (`service/qr.py::is_charge_valid` /
  `is_charge_expired`, re-exportadas en `service/`): un cargo solo es
  utilizable si esta `ACTIVE` y no expiro (fail-closed con `None` o
  estado desconocido). La usan el endpoint de estado (`E4-T08`) y el
  pago (`HU15`) antes de operar (`docs/04#7.4`: el vencido se rechaza
  antes, sin retencion).
- Wiring (patron `E3-T19`/M-2): runner
  `app.composition.run_wallet_qr_jobs_once` + worker
  `start_wallet_qr_jobs_worker` (hilo daemon + parada por evento) con
  flags `WALLET_QR_JOBS_ENABLED` / `WALLET_QR_EXPIRY_ENABLED`
  (default `1`) e intervalo `WALLET_QR_JOBS_INTERVAL_SECONDS`
  (default `60` s); arrancados en el `lifespan` de `backend/app/main.py`
  y en `infra/docker/worker_entrypoint.py::maybe_start_wallet_qr_jobs`
  (inertes con el flag apagado, `/health` intacto).
- Pruebas: `backend/tests/test_e4_t07_wallet_qr_expiry.py` (job feliz,
  borde exacto, terminales preservados, `STATIC`, idempotencia,
  on-read valido/invalido, runner con flags, wiring `lifespan`/worker,
  sin PII). Sin migracion (reuso de `status`/`expires_at` de `0039`).

## Reconciliacion del cobro QR (R-1, HU15)

- Job `jobs/qr_reconcile.py::run_qr_reconcile_once(session, *,
  limit=100)`: una pasada que finaliza los `qr_payments` en
  `PENDING_AUTHORIZATION` cuya operacion del motor ya esta `SETTLED`
  (el cliente autorizo pero nunca reintento `POST /payments/qr`, asi
  que `E4-T12` nunca finalizo), con `commit` por item (patron
  `E3-T19`) e idempotente (solo `SETTLED` se finaliza; otro estado ->
  `skipped` sin tocar nada). Ejecuta el helper existente
  `service/qr_payment.py::finalize_qr_payment` (cargo `PAID` +
  `paid_at`, evento `qr.payment.confirmed` por `outbox`, aviso unico
  `qr_charge_paid`). Selector propio
  `repository/qr_payment.py::list_pending_authorization` (solo tablas
  propias); el estado de la operacion se lee por la fachada
  `transactions.service.find_transaction_by_id` (solo lectura, patron
  M-1; sin importar modelos ni repositorios ajenos). **No mueve
  dinero** (ya se movio al autorizar); sin PII en logs (solo conteos).
- Wiring (patron `E3-T19`/M-2): el runner
  `app.composition.run_wallet_qr_jobs_once` corre la reconciliacion
  **antes** que la caducidad (un autorizado se cierra a `PAID` antes de
  poder expirar a `EXPIRED` sin aviso), con flag por job
  `WALLET_QR_RECONCILE_ENABLED` (default `1`, `wallet_qr_reconcile_enabled()`;
  el general `WALLET_QR_JOBS_ENABLED=0` apaga ambos); `lifespan` y
  `worker_entrypoint.py::maybe_start_wallet_qr_jobs` sin cambios de
  firma (usan el runner). Sin migracion (reuso de `status` de `0041`).
- Pruebas: `backend/tests/test_r1_qr_reconcile.py` (6 pruebas:
  autorizado-sin-replay -> `PAID` + un evento + un aviso + caducidad
  que ya no lo expira; no-`SETTLED` no se finaliza; `limit` invalido;
  runner con flags y orden; fachada + frontera sin tablas ajenas).

## Endpoints del comercio `/merchants/qr*` (E4-T08, HU14 CA-01/CA-02 parcial/CA-04 parcial)

- Rutas (contrato en `docs/05#6.5`): `POST /api/v1/merchants/qr`
  (`{amount_minor, currency, concept?, mode?}` ->
  `201 {charge_id, payload, payload_hash, signature, amount_minor,
  currency, concept, mode, status, expires_at, created_at}` +
  `meta.replayed`; `Idempotency-Key` opcional, patron `E2-T13`) y
  `GET /api/v1/merchants/qr/{id}` (solo el dueno ->
  `200 {charge_id, status, amount_minor, currency, expires_at,
  paid_at?}` con `status` efectivo on-read: `ACTIVE` vencido se
  reporta `EXPIRED`).
- Capas: `api/qr.py` (router incluido en `api/`, auth JWT `Bearer`
  propio del modulo) + `schemas/qr.py` + `service/qr.py::
  require_merchant_for_user` (rol = cuenta `ACTIVE` propia; si no
  `403 NOT_AUTHORIZED`) / `create_qr_charge_idempotent` /
  `get_charge_status` (`404` neutro si ajeno/inexistente) /
  `notify_merchant_charge_paid` (ruta del aviso via
  `notifications.send`, plantilla `qr_charge_paid`; el disparador es
  el pago de `HU15`, `docs/18` G72).
- **No mueve dinero** (el pago es HU15); sin endpoint de alta de
  comercio (comercios demo de `0039` + `create_merchant` interno,
  `docs/18` G69). Sin PII/secretos en logs ni respuestas.
- Pruebas: `backend/tests/test_e4_t08_merchant_qr.py` (12 pruebas:
  feliz con firma/TTL/evento + estado `ACTIVE`, `STATIC`, vencido ->
  `EXPIRED` sin job, `422`/`404`/`403`/`401`, idempotencia replay +
  `409`, abono stub sin PII, frontera sin tablas de dinero).

## QR personal estatico + nombre enmascarado (E4-T21/E4-T24, HU14)

- Rutas (contrato en `docs/05#6.5`): `POST /api/v1/wallet/qr`
  (`{currency?, concept?}` -> `201` con el QR reutilizable sin monto
  ni caducidad, `payee_type: "PERSONAL"`) y
  `GET /api/v1/wallet/qr/{id}` (solo el titular dueno; ajeno/
  inexistente/de comercio -> `404` neutro ->
  `200 {charge_id, qr_content, status, amount_minor: null, currency,
  expires_at: null, paid_at?, payee_type, reusable, payee_name_masked}`).
- `payee_name_masked` (E4-T24): nombre + un apellido del dueno
  enmascarado (`DIEG••• GONZ•••` = primeros 4 por parte + resto oculto
  con `•••`, partes separadas por un espacio; parte `<= 4` completa;
  razon social como una sola parte). Helper puro y determinista
  `domain/masking.py::mask_payee_name` + resolucion best-effort
  `resolve_payee_name_masked` por la fachada de solo lectura
  `identity.service.profile.get_profile` (import perezoso, D-03; sin
  importar modelos ajenos); sin nombre resoluble -> `None` (fallback
  neutro). Presente tambien en `POST /payments/qr/preview` (personal:
  el titular; comercio: el dueno natural, con `business_name`
  intacto). El nombre completo jamas viaja ni se loguea (reglas 7/9).
- Sin migracion (solo lectura). Pruebas:
  `backend/tests/test_e4_t24_payee_name_masked.py` (22 pruebas:
  formato, estado, preview personal/comercio, tolerancia, no-fuga,
  contrato aditivo) + `backend/tests/test_e4_t21_personal_qr.py`.

## Interoperabilidad con el par (E4-T03, HU13 CA-03)

- Adaptador `WalletInteropGateway` (`backend/app/adapters/wallet_interop.py`,
  contrato en `docs/02#9`): interfaz + `MockWalletInteropGateway` determinista
  (modos `success`/`down`/`timeout`, `counterparts`/`rejected` configurables,
  sin red) + `HttpWalletInteropGateway`
  (`POST {base_url}/interop/counterparts/validate`, timeout/reintentos con
  backoff/circuit breaker) + fabrica `create_wallet_interop_provider`
  (`WALLET_INTEROP_PROVIDER=mock|http`, default `mock`, valor no reconocido
  -> `mock`).
- Sobre `BO-QR1` (D06, formato propio firmado): `encode/decode` locales
  (JSON canonico -> base64url); `sig` opaco de passthrough (la firma local la
  fija `E4-T06` en `wallet.qr_charges`, no aqui). Errores tipados
  `INTEROP_UNAVAILABLE` (`503`) / `INTEROP_TIMEOUT` (`502`).
- Sin PII/secretos en logs (solo hash de la referencia); no mueve dinero ni
  persiste tablas.
- Pruebas: `backend/tests/test_e4_t03_wallet_interop.py` (determinismo,
  rechazo tipado, timeout/caida simulados, seleccion por entorno, sin red).

## Catalogo de servicios + suscripciones (E4-T16, HU16 CA-01/CA-03 base)

- Tablas propias (`docs/03b#95`, migracion `0042_wallet_services`,
  head vigente, `down_revision="0041_wallet_qr_payment_guard"`):
  `billers` (`category` IDX, `name`, `required_fields` JSONB,
  `enabled`; catalogo extensible sin cambios estructurales: filas
  nuevas, columnas fijas; 6 demos sembrados idempotentemente por
  categoria + operadores de recarga, `docs/18` G77 decision 3),
  `service_payments` (`user_id`/`transaction_id` REF logicas,
  `biller_id` FK mismo schema, `supply_ref` IDX, `amount_minor
  CK>0`, `status` default `PENDING`) y `topups` (`operator`/
  `phone`, `amount_minor CK>0`, `currency` default `PEN`,
  `transaction_id` REF; vacias, las escribe el pago de `E4-T17`) y
  `service_subscriptions` (`biller_id` FK mismo schema,
  `supply_ref`, `notify_days_before` default `3` coherente con HU16,
  `status` default `ACTIVE`; las consume el job de `E4-T18`). Sin
  `CHECK` sobre `status` en las operativas (los estados finales los
  fija `E4-T17`).
- Capas: `models` (`Biller`/`ServicePayment`/`Topup`/
  `ServiceSubscription`; `sa.JSON()` generico para SQLite, `JSONB`
  en la migracion), `repository/services.py`
  (`list_billers(session, *, category?, query?)`, `get_biller`,
  `create_subscription` (idempotente por naturaleza: devuelve la
  `ACTIVE` existente), `list_subscriptions`, `DEMO_BILLERS` +
  `ensure_demo_billers` para tests/dev sin migracion),
  `schemas/services.py`, `service/services.py`
  (`normalize_catalog_filters`, `list_catalog`,
  `register_subscription`: el `biller` se resuelve antes de llamar
  al proveedor —inexistente/deshabilitado -> 404 neutro— y el
  suministro se valida contra el proveedor vigente —invalido ->
  422 neutro, caido -> 503, timeout -> 502—), `api/services.py`
  (`GET /api/v1/services/catalog` filtrable por `category`/`q`,
  `200`; `POST /api/v1/services/subscriptions`, `201` con
  `replayed`; contrato en `docs/05#6.5`; router incluido en
  `api/`).
- Adaptador `ServiceProviderGateway`
  (`backend/app/adapters/service_provider.py`, patron `E4-T03`,
  `docs/18` G77 decision 2): interfaz (`lookup_biller` por
  nombre/suministro + `validate_supply`) + `mock` determinista sin
  red (modos `success`/`down`/`timeout`, catalogo demo espejo de la
  siembra, deuda simulada del hash) + `http` (`POST
  {base_url}/services/supply/validate`, timeout/reintentos con
  backoff/circuit breaker) + fabrica `create_service_provider`
  (`SERVICE_PROVIDER=mock|http`, default `mock`, valor no
  reconocido -> `mock`; env en `backend/.env.example`).
- Decisiones (`docs/18` G77): `POST /services/subscriptions`
  aditivo (opcion recomendada del brief; lo consume `E4-T18`); MVP
  con `mock` local y `http` real despues sin tocar el caso de uso;
  `required_fields` demo (`supply_ref` en servicios, `phone` en
  operadores de recarga, categoria `TELEFONIA` incluida).
- **No mueve dinero** (el pago/recarga es `E4-T17`). Sin
  PII/secretos en logs ni respuestas fuera de lo necesario (solo
  hash del suministro en logs; montos enteros, sin `float`).
- Pruebas: `backend/tests/test_e4_t16_services_catalog.py` (35
  pruebas: catalogo/busqueda/extensibilidad, suscripcion + replay +
  aislamiento, adaptador determinista/seleccion/`http` simulado sin
  red real, errores `401`/`404`/`422`/`502`/`503`, sin PII, frontera
  solo `wallet`, cadena `0042` reversible).

## Pago de servicio y recarga por el motor (E4-T17, HU16 CA-02/CA-04)

- Tablas propias (las escribe este pago, `docs/03b#95`, sin migracion
  nueva —reuso de `0042_wallet_services`—): `service_payments`
  (`transaction_id` REF logica, `receipt_data` JSONB con el
  comprobante, `status` `PENDING`/`PAID`/`REJECTED`) y `topups`
  (`operator`/`phone`, `transaction_id` REF, `status`).
- Capas: `service/services.py::pay_service`/`pay_topup` (validan el
  suministro/telefono contra el proveedor vigente **antes de cobrar**
  —invalido -> 422 neutro, caido -> 503, timeout -> 502—; dinero
  **solo** por `pay_express` con la misma `Idempotency-Key`, sin
  comision, `docs/04#7.10`; `SETTLED` -> `PAID` + comprobante +
  evento, `PENDING_AUTHORIZATION` -> `PENDING`, `REJECTED` sin
  comprobante ni evento; idempotencia por (`Idempotency-Key`,
  titular) con replay y `CONFLICT`) + `finalize_service_payment`/
  `finalize_topup` (tras autorizar con
  `daily_limit_reader=wallet_daily_limit_reader`, G75; los invoca el
  replay) + `to_topup_receipt`/`build_receipt_no`, `repository/
  services.py` (`create_service_payment`/`create_topup`/
  `mark_service_payment_paid`/`set_*_status`, `flush` sin `commit`),
  `schemas/services.py` + `api/services.py` (`POST
  /api/v1/services/payments` `{biller_id, supply_ref, amount_minor,
  currency?, link_id}` -> `201 {payment_id, transaction_id, status,
  amount_minor, currency, receipt}` y `POST /api/v1/topups`
  `{operator, phone, amount_minor, currency?, link_id}` ->
  `201 {topup_id, ...}`; `Idempotency-Key` obligatoria; contrato en
  `docs/05#6.5`).
- Asiento (`docs/18` G73, fijado en `docs/04#7.10`): retencion `Debe
  2000-Pagador / Haber 2100-Pagador` + liquidacion `Debe 2100-Pagador
  / Haber 2000-Liquidacion` (cuenta `accounts` interna del proveedor,
  `WALLET_SERVICES_SETTLEMENT_ACCOUNT_ID` por entorno, resuelta por
  la fachada `accounts`; sin columna nueva en `billers`; sin ella el
  pago se rechaza fail-closed antes de operar).
- Comprobante (CA-04, decision 2): `receipt_no` oficial `OP-…`
  (convencion `E2-T10`: `OP-` + 12 hex de `sha256(transaction_id)`,
  unico y no secuencial) + concepto + monto/moneda + instante +
  referencia enmascarada (`supply_masked`/`phone_masked`); sin PII en
  claro en respuestas, eventos ni logs.
- Eventos `service.paid`/`topup.completed` (constantes en
  `app/core/events.py`, re-exportadas en `wallet/events/`; payload
  sin PII) por `outbox` + auditoria best-effort. **No escribe
  `transactions`/`ledger`/`accounts`** (solo fachadas
  `transactions.service`/`accounts.service`; regla de oro 4).
- Pruebas: `backend/tests/test_e4_t17_service_payments.py` (42
   pruebas: feliz con asiento cuadrado + evento + saldos, recarga
   CA-02, pendiente + autorizacion/finalizacion, diario/fondos sin
   tocar saldos, idempotencia/`CONFLICT`/clave en vuelo, comprobante
   `OP-…`, errores `401`/`404`/`409`/`422`/`502`/`503`, sin PII,
   frontera sin tablas ajenas, OpenAPI, cadena `0042`).

## Job de recordatorios de servicios (E4-T18, HU16 CA-03)

- Sin endpoint (job best-effort, **no mueve dinero**):
  `jobs/service_reminders.py::run_service_reminders_once(session, *,
  now=None)` (una pasada con `commit` por item, patron `E3-T19`)
  recorre las `service_subscriptions` `ACTIVE` (lector
  `repository/services.py::list_active_subscriptions`) y avisa por
  push las que vencen en la ventana exacta, via el helper
  `service/services.py::notify_service_reminder` (fachada
  `notifications.send`, plantilla `service_reminder`: solo referencia
  del servicio y fecha, sin PII ni cifras).
- Origen del vencimiento (decision 1 del brief, `docs/18` G77b):
  `service_subscriptions` no tiene columna de fecha y el adaptador
  `ServiceProviderGateway` no expone vencimiento → vencimiento
  derivado determinista y puro (`subscription_due_day`: dia `1..28`
  del `subscription_id` + `next_due_date`: proxima ocurrencia `>=
  hoy`); el proveedor real lo reemplazara sin tocar el job.
- Ventana efectiva = minimo entre el parametro global
  `wallet.service_reminder_days_before` (default `3`,
  `shared/config::get_wallet_service_reminder_days_before`,
  best-effort con fallback, entero `>= 1`; semilla `0043`,
  `PUT /admin/parameters` sin redespliegue) y el
  `notify_days_before` de la fila (1..30, default `3`).
- Idempotencia por `(subscription_id, dia)` en `processed_events`
  (consumidor `wallet-service-reminders`, `event_id` UUID5; la marca
  se confirma al reclamarla, como en `E3-T19`: el fallo posterior
  queda `failed` sin duplicar). Sin evento nuevo (basta el push).
- Wiring en ambos runtimes (patron M-2):
  `app.composition.run_wallet_service_jobs_once` /
  `start_wallet_service_jobs_worker` (flags
  `WALLET_SERVICE_JOBS_ENABLED` /
  `WALLET_SERVICE_REMINDERS_ENABLED`, intervalo
  `WALLET_SERVICE_JOBS_INTERVAL_SECONDS` defecto 3600 s), en el
  `lifespan` (`backend/app/main.py`) y en
  `infra/docker/worker_entrypoint.py`
  (`maybe_start_wallet_service_jobs`); `/health` intacto. Env en
  `.env.example`, `backend/.env.example` y `docker-compose.yml`.
- Pruebas: `backend/tests/test_e4_t18_service_reminders.py` (20
  pruebas: ventana exacta, idempotencia, parametro,
  `notify_days_before` por fila, sin PII en `data`/logs, wiring,
  cadena `0043`).
