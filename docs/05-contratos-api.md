# 05 - Contratos de API y convenciones

## 1. Convenciones generales

- **Base**: `/api/v1` (version en la ruta).
- **Formato**: JSON UTF-8; fechas ISO-8601 UTC; dinero como `{ "amount_minor": 10000,
  "currency": "PEN" }`.
- **Nombres**: rutas en `kebab-case`, campos en `snake_case`.
- **Documentacion**: OpenAPI/Swagger generado por FastAPI, versionado con el codigo.
- **Sin logica en el router**: valida, traduce y delega al `service/` del modulo.

## 2. Autenticacion y autorizacion

| Mecanismo | Uso | Detalle |
|---|---|---|
| `Authorization: Bearer <jwt>` | Usuarios (app y panel) | Access token corto + refresh. |
| `X-API-Key` | Servicios entre si (p. ej. consumir KYC) | Como el microservicio existente. |
| Refresh token | Renovacion de sesion | Rotativo; se revoca al cerrar sesion. |

- El JWT lleva `sub` (user_id), `roles`, `session_id`, `exp`.
- RBAC segun la matriz de accesos (hoja "Matriz de acceso").
- Datos sensibles enmascarados salvo permiso explicito; el acceso completo se audita.

## 3. Cabeceras estandar

| Cabecera | Obligatoriedad | Proposito |
|---|---|---|
| `Authorization` | Todo endpoint privado | JWT. |
| `Idempotency-Key` | **Todo endpoint que mueve dinero** | Evita duplicados. |
| `X-Request-Id` | Recomendada | Correlacion de logs (si no llega, se genera). |
| `X-API-Key` | Servicios | Autenticacion tecnica. |
| `Accept-Language` | Opcional | Mensajes en `es`/`en`. |

## 4. Formato de respuesta

Exito simple:
```
{ "data": { ... }, "meta": { "request_id": "..." } }
```
Listas paginadas:
```
{
  "data": [ ... ],
  "meta": { "page": 1, "page_size": 20, "total": 57, "request_id": "..." }
}
```
Error (estilo problem+json):
```
{
  "error": {
    "code": "INSUFFICIENT_FUNDS",
    "message": "Saldo insuficiente para completar la operacion",
    "details": { "available_minor": 500, "required_minor": 1000 },
    "request_id": "..."
  }
}
```

Codigos de error reales (verificados en codigo, 2026-09-25; `DUPLICATE_BENEFICIARY`,
`IDEMPOTENCY_CONFLICT` y `RECEIPT_FORMAT_NOT_SUPPORTED` anadidos 2026-09-26, E2-T10/E2-T13;
`DAILY_LIMIT_EXCEEDED`/`BIOMETRIC_PREVENTIVE_BLOCK` y `CONFLICT` de reverso consolidados
2026-09-27, E2-T17/E2-T19/E2-T25):
`ACCOUNT_LOCKED`, `BIOMETRIC_PREVENTIVE_BLOCK`, `CONFLICT` (409), `DAILY_LIMIT_EXCEEDED`,
`DOC_LOOKUP_UNAVAILABLE`, `DOCUMENT_NOT_FOUND`, `DUPLICATE_BENEFICIARY` (409), `DUPLICATE_DOCUMENT`, `DUPLICATE_EMAIL`, `DUPLICATE_LINK` (409, E4-T01), `EXPIRED_NONCE`, `EXPIRED_OTP`,
`EXPORT_FORMAT_NOT_SUPPORTED` (501), `IDEMPOTENCY_CONFLICT` (409),
`INSUFFICIENT_FUNDS` (solo como `data.failure_code` con `data.status=REJECTED` en
`POST /transfers/*`, E2-T07; el HTTP sigue `201` para no romper la idempotencia),
`INVALID_CREDENTIALS`, `INVALID_LOGIN`, `INVALID_OTP`, `INVALID_PIN_FORMAT`,
`INVALID_PIN_RESET`, `INVALID_REFRESH`, `INVALID_SETUP_CODE`, `KYC_UNAVAILABLE`,
`NOT_AUTHENTICATED`, `NOT_AUTHORIZED`, `NOT_FOUND`, `PIN_ALREADY_SET`, `PIN_REQUIRED`,
`RATE_LIMITED`, `RECEIPT_FORMAT_NOT_SUPPORTED` (501), `REFRESH_EXPIRED`, `REFRESH_REUSED`,
`RESEND_LIMIT`, `SESSION_INACTIVE`, `TARGET_AMBIGUOUS` (409, E2-T27: el destino por DNI/celular
resuelve a un usuario con varias cuentas `ACTIVE`), `TARGET_NOT_FOUND` (404 neutro, E2-T27: DNI/
celular inexistente o usuario sin cuenta `ACTIVE`), `VALIDATION_ERROR`.
Fuentes: `backend/app/modules/identity/api/*` (+ `service/`), `backend/app/modules/accounts/api/__init__.py`
(`EXPORT_FORMAT_NOT_SUPPORTED`, `NOT_AUTHENTICATED`, `NOT_AUTHORIZED`, `NOT_FOUND`,
`VALIDATION_ERROR`), `backend/app/modules/accounts/api/beneficiaries.py`
(`DUPLICATE_BENEFICIARY`, `IDEMPOTENCY_CONFLICT`), `backend/app/modules/transactions/api/__init__.py`
(`RECEIPT_FORMAT_NOT_SUPPORTED`). `backend/app/modules/wallet/api/__init__.py` (`DUPLICATE_LINK`,
`IDEMPOTENCY_CONFLICT` de vinculos, E4-T01). Sin `INVALID_RECOVERY_CODE` (solo lo emitia el retirado `verify`, E1-T41).
`TARGET_NOT_FOUND`/`TARGET_AMBIGUOUS` (decisiones de destino 2026-10-03, E2-T27) se fijan en el
endpoint de lookup previo descrito en `#6.3`. `DUPLICATE_LINK` (`409`, E4-T01: la fuente ya esta
vinculada y `ACTIVE`; ver `docs/05#6.5`).

## 5. Codigos HTTP

| Codigo | Uso |
|---|---|
| 200/201 | Exito / creado. |
| 202 | Aceptado y en proceso (interbancario, evaluacion de credito). |
| 400 | Entrada invalida. |
| 401 | No autenticado. |
| 403 | Sin permiso / bloqueado por riesgo. |
| 404 | No encontrado. |
| 409 | Conflicto (idempotencia con cuerpo distinto, estado invalido). |
| 422 | Validacion de esquema. |
| 429 | Rate limit. |
| 500/503 | Error interno / dependencia externa no disponible. |

## 6. Catalogo de endpoints (MVP)

### 6.1 Identidad y KYC (`identity`) - HU01-HU04

| Metodo | Ruta | Proposito |
|---|---|---|
| POST | `/auth/kyc/challenge` | Inicia el flujo KYC (proxy al microservicio). |
| POST | `/auth/kyc/evaluate` | Evalua un paso de liveness en vivo (rafaga `frames_b64`). |
| POST | `/auth/kyc/document/validate` | Valida la legibilidad del documento (proxy; E1-T30). |
| POST | `/auth/kyc/document/lookup` | Consulta el titular por DNI/RUC (proxy a apiinti; E1-T35). |
| POST | `/auth/kyc/email/check` | Prechequea si el email ya esta registrado: `200 {available: true}` o `409 DUPLICATE_EMAIL` (E1-T40; paso "Continuar", sin proveedor ni persistencia). |
| POST | `/auth/kyc/submit` | Envia documento + segmentos de liveness y obtiene resultado. |
| POST | `/auth/activate` | Valida OTP y activa la cuenta (HU02; **deprecado pero vivo**, E1-T32/SCR-005: cabecera `Deprecation: true` y OpenAPI `deprecated`; la via canonica es `POST /auth/pin/setup`). |
| POST | `/auth/otp/resend` | Reenvia OTP con control de intentos (entrega solo por email). |
| POST | `/auth/login/challenge` | Emite `nonce` para firmar con biometria del dispositivo (HU03). |
| POST | `/auth/login/facial` | Valida el `nonce` firmado y abre sesion (HU03, biometria local). |
| POST | `/auth/login/pin` | Login alterno con PIN (E1-T39: `data` incluye `biometric_enabled: bool`). |
| POST | `/auth/biometric/consent` | Fija `credentials.biometric_enabled` del usuario del JWT (E1-T39, Bearer). |
| POST | `/auth/refresh` | Renueva tokens. |
| POST | `/auth/logout` | Revoca la sesion. |
| POST | `/auth/recovery/request` | Solicita OTP de recuperacion por email, siempre 200 sin enumerar (E1-T31; el OTP emitido lo consume `POST /auth/pin-reset`). |
| POST | `/auth/pin-reset` | Fija el PIN con `email + documento (DNI\|RUC) + OTP` y devuelve `{user_ref, pin_set: true}` sin abrir sesion (E1-T34/SCR-005; E1-T40: `doc_type` con default `DNI`; consumen `F-T34`..`F-T43`). |
| POST | `/auth/login/device/request` | Paso 1 del login en dispositivo nuevo: `email + documento (DNI\|RUC)` -> OTP `LOGIN` solo por email, siempre 200 sin enumerar (E1-T45; lo consume `POST /auth/login/device/complete` en E1-T46; consumen `F-T56`/`F-T57`). |
| POST | `/auth/login/device/complete` | Paso 2 del login en dispositivo nuevo: OTP `LOGIN` + PIN de la misma cuenta -> binding + sesion (`{access_token, refresh_token, session_id, user_ref, biometric_enabled}`; E1-T46; consumen `F-T56`/`F-T57`). Errores: unico `401 INVALID_LOGIN` generico (cuenta/documento/OTP/PIN invalidos, sin enumeracion), `423 ACCOUNT_LOCKED`, `429 RATE_LIMITED` (ventana por `email+IP` compartida con el paso 1), `422` de formato. |
| GET | `/me` | Nombre del titular autenticado para saludo/avatar (E1-T44, Bearer; ver decision abajo). |

> Decision E1-T41 (retiro de `verify`, item 4 del dueno): `POST
> /auth/recovery/verify` fue ELIMINADO (la UI "recupera mi acceso" no tenia
> sentido: redirigia a login sin dar acceso). Se CONSERVA `POST
> /auth/recovery/request` (+ `service/recovery.py`, `repository/recovery.py`
> y la tabla `access_recovery`) porque `POST /auth/pin-reset` depende de
> ellos: consume el OTP `RECOVERY` emitido por `request` y registra
> `access_recovery` con `new_credential_set=true`. El error
> `INVALID_RECOVERY_CODE` deja de existir en el catalogo (solo lo emitia
> `verify`); `RATE_LIMITED` de `request` y la ventana compartida de
> verificacion (que ahora rige a `/auth/pin-reset`) se conservan.
>
> Nota de alineacion (2026-09-25): `POST /auth/recover` queda ELIMINADO del catalogo
> (diferido, sin implementar). El flujo vigente de recuperacion es `POST
> /auth/recovery/request` + `POST /auth/pin-reset`; el antiguo `verify` se retiro en
> SCR-005/E1-T41.
>
> Decision E1-T31: el `/auth/recover` canonico de HU04 (dispositivo confiable +
> `nonce` firmado + cambio de credencial) queda diferido (sin implementar). El endpoint nuevo
> es pre-sesion para el caso "sin `user_ref` local" (reinstalar/borrar datos):
> `request {email}` -> `data: {accepted: true, ttl_seconds, resend_wait_seconds}`
> (constantes globales, identico exista o no el email; `429 RATE_LIMITED` por
> `email+IP`). OTP `RECOVERY` solo por
> email (plantilla `otp_code_email`; nunca SMS), cooldown reutilizando el `PENDING`
> vigente, auditoria `auth.recovery_requested` (sin PII).
>
> Decision E1-T34/SCR-005: `POST /auth/pin-reset` cierra el flujo para el
> usuario que recupera el acceso pero no recuerda su PIN: request
> `{email, doc_number, code, pin}` -> `data: {user_ref, pin_set: true}`
> (consume el OTP `RECOVERY` de `recovery.request`, fija
> `credentials.pin_hash` + resetea `failed_attempts`/`locked_until` +
> `access_recovery` con `new_credential_set=true`; NO abre sesion ni emite
> tokens —en el flujo recuperacion/pin-reset la sesion NO se abre ahi; se
> abre al autenticarse en login (PIN o biometria)—; auditoria
> `auth.pin_reset` sin PII). El DNI se valida contra el hash HMAC
> server-side (`doc_number_hash`, nunca en claro ni en logs).
> Errores: `401 INVALID_PIN_RESET` (un unico cuerpo generico para email no
> registrado, DNI que no coincide, sin OTP, codigo incorrecto, OTP vencido y
> OTP bloqueado por intentos agotados: sin enumeracion ni oraculo de campo),
> `429 RATE_LIMITED` (ventana por `email+IP`, verificada antes de la
> existencia; reutiliza la ventana de verify), `422` estandar de FastAPI
> para email/PIN malformados o PIN debil (4-6 digitos; se valida antes de
> consumir el OTP, sin oraculo de cuenta).
>
> Decision E1-T32/SCR-005: el OTP de **activacion** tambien viaja **solo por
> email** (`users.email`, plantilla `otp_code_email`; sin email no hay entrega y
> nunca SMS; `phone`/`channel='sms'` se ignoran sin error). `POST
> /auth/activate` queda **deprecado pero vivo** (mismo request/response/errores;
> `Deprecation: true` + OpenAPI `deprecated`); la activacion canonica con un solo
> OTP es `POST /auth/pin/setup`.
>
> Decision E1-T38: `POST /auth/pin/setup` acepta `biometric_enabled?: bool =
> false` (opcional; ausente/`false` preserva el contrato anterior y deja la
> credencial en `False`); con `true` lo persiste en
> `identity.credentials.biometric_enabled` en la misma transaccion que
> `pin_hash` + `ACTIVE` (el flag no viaja en la respuesta). `POST
> /auth/login/facial` exige consentimiento (`biometric_enabled is True`)
> ademas de binding `ACTIVE` + firma valida: sin el responde `400
> INVALID_LOGIN` generico (mismo cuerpo que firma/binding invalidos, sin
> sesion ni tocar el binding; la biometria es opcional y el PIN es siempre
> el fallback).
>
> Decision E1-T40: `POST /auth/kyc/email/check` (pre-registro, sin auth)
> prechequea el email al pulsar **Continuar** en `kyc-start` (decision del
> dueno items 2 y 5): request `{email}` -> `data: {available: true}` si no
> esta registrado; `409 DUPLICATE_EMAIL` con `"El correo ya esta
> registrado"` (neutro, sin eco del email) si existe. Normaliza
> (`strip().lower()`, igual que el alta) y consulta `get_by_email`; nunca
> llama al proveedor ni persiste. El rate-limit corre ANTES de resolver
> existencia (ventana en memoria compartida con el proxy KYC; prod:
> Redis/middleware); `422` estandar para email malformado. El `409
> DUPLICATE_EMAIL` de `POST /auth/kyc/submit` queda intacto (respaldo de
> carrera). `POST /auth/pin-reset` acepta `doc_type: "DNI"|"RUC"` (default
> `"DNI"` por compatibilidad F-T43): valida en el esquema (422 estandar)
> `doc_number` solo digitos con longitud exacta (DNI 8 / RUC 11, paridad
> con el lookup E1-T35); la resolucion sigue por `email` +
> `doc_number_hash` HMAC (`doc_type` NO se cruza con `users.doc_type`);
> errores intactos (`401 INVALID_PIN_RESET` generico, `429 RATE_LIMITED`,
> `422` de PIN). Sin migracion.
>
> Decision E1-T39: `POST /auth/biometric/consent` (Bearer) gestiona el
> consentimiento post-alta: request `{"enabled": true|false}` -> `200
> {"data": {"biometric_enabled": <bool>}, "meta": {"request_id": ...}}`
> (idempotente; `401 NOT_AUTHENTICATED` sin cabecera/`Bearer` invalido/`sub`
> no UUID; `404 NOT_FOUND` neutro si falta la credencial; `422` estandar de
> esquema). `POST /auth/login/pin` amplia su `data` con `biometric_enabled:
> bool` (consentimiento vigente, solo tras exito; tokens, `session_id`,
> `expires_in` y errores intactos) para que el cliente sincronice el boton
> biometrico. `POST /auth/login/facial` no cambia su contrato: el flag solo
> lo habilita/revoca.
>
> Decision E1-T42: `POST /auth/login/pin` con `device_id` +
> `device_public_key` reescribe el `public_key` del binding existente con la
> clave vigente (rebind best-effort en savepoint; una sola fila por
> `user_id`+`device_id`, `last_used_at` refrescado, `status` preservado: un
> `REVOKED` no se reactiva; el alta sigue `ACTIVE`; auditoria
> `auth.device_binding` con `registered`/`touched`/`failed`). La clave se
> persiste tal cual (`hmac:`/PEM, sin validar formato). Sin cambio de
> request/response: el `POST /auth/login/facial` posterior verifica la firma
> contra la clave vigente (cualquier desajuste sigue `400 INVALID_LOGIN`
> generico).
>
> Decision E1-T46: `POST /auth/login/device/complete` cierra el login sin
> `userRef` (paso 2 de E1-T45): request `{email, doc_type: DNI|RUC,
> document_number, code: 6 digitos, pin: 4-6 digitos, device_id,
> device_public_key[, platform, biometric_type]}` -> `200 {access_token,
> refresh_token, session_id, user_ref, biometric_enabled}` (el cliente persiste
> el `userRef`; la sesion lleva el `device_id`). Atomico PIN-primero: el PIN se
> verifica antes de consumir el OTP (un OTP valido con PIN erroneo NO se quema;
> el fallo solo mueve el lockout del PIN, 5 fallos -> `423 ACCOUNT_LOCKED`).
> Binding best-effort en savepoint (alta `ACTIVE`; existente preserva `status`:
> un `REVOKED` no revive pero la sesion abre igual). Unico `401 INVALID_LOGIN`
> generico (sin enumeracion); `429 RATE_LIMITED` compartido con el paso 1
> (ventana `auth.recovery_verify_*`, scope `"device_login"`); `422` de formato
> (PIN `4-6` digitos, patron `pin_reset`, sin oraculo). Sin migracion.
>
> Decision E1-T44: `GET /me` (Bearer) devuelve el minimo para el saludo/avatar
> del dashboard: `200 {"data": {"first_name": <str>, "last_name": <str>,
> "business_name": <str|null>}, "meta": {"request_id": ...}}` con los valores
> reales de `identity.users` del `user_id` del JWT (`sub` via
> `core.security.decode_token`, 05#2; nunca `user_id` por query/path).
> RUC de persona juridica: nombres `""` + razon social en `business_name`
> (E1-T36); el resto: `business_name` `null`. No expone documento, correo,
> telefono, estado, roles ni hashes. Errores: `401 NOT_AUTHENTICATED` (sin
> cabecera/`Bearer` invalido/`sub` no UUID), `404 NOT_FOUND` neutro (usuario
> inexistente); sin body (sin 422). Solo lectura, sin migracion.
>
> Decision E1-T45: `POST /auth/login/device/request` (pre-sesion, paso 1 del
> login en dispositivo nuevo, sin `userRef` local) separa LOGIN de RESTABLECER
> PIN: request `{email, doc_type: DNI|RUC (default DNI), document_number}` ->
> `200 {"data": {"accepted": true, "ttl_seconds", "resend_wait_seconds"},
> "meta": {"request_id": ...}}` (constantes globales, identico exista o no la
> cuenta, coincida o no el documento o sea o no `ACTIVE`; `422` estandar de
> FastAPI para email/documento malformados). Solo si `email` y documento
> pertenecen a la MISMA cuenta `ACTIVE` (email normalizado + hash HMAC
> `hash_document_number` comparado con `hmac.compare_digest`, sin cruzar
> `users.doc_type`) emite OTP proposito `LOGIN` (ya existe en el enum, sin
> migracion) entregado SOLO por email (`otp_code_email`, best-effort); en
> cooldown reutiliza el `PENDING` vigente sin duplicar ni re-notificar.
> Rate-limit por `email+IP` ANTES de resolver existencia (`429 RATE_LIMITED`):
> REUTILIZA la ventana `auth.recovery_verify_*` con scope propio
> `"device_login"` (sin claves nuevas en `config.parameters`, regla de oro 6).
> Auditoria `auth.device_login_requested` sin PII. El OTP lo consume el paso 2
> (`POST /auth/login/device/complete`, E1-T46); `pin-reset` no es base.

Detalle del flujo KYC (`identity`, E1-T29):

- `POST /auth/kyc/challenge` -> `data: {token, steps: [str], expires_in: int}`.
- `POST /auth/kyc/evaluate` -> request `{challenge_token, step, frames_b64: [str]}`
  y `data: {step, passed, reason, frames_analyzed, details}`.
- `POST /auth/kyc/submit` -> cada segmento acepta `frames_b64: [str]` (rafaga; fuente
  unica) **o** `image_b64: str` (compatibilidad, un solo frame); `document` lleva
  `{type: DNI|CE|PASSPORT|RUC, number, image_b64}` y `applicant` los datos del titular:
  `{first_name, last_name, business_name?, email, phone?}` (E1-T36: `business_name` es la
  razon social, opcional, max 150; `first_name`/`last_name` aceptan vacio a nivel de
  esquema). Validacion por tipo ANTES del proveedor (`422 VALIDATION_ERROR` sin invocar
  al microservicio): `RUC` exige `business_name` no vacio (juridica) o nombres completos
  (RUC de persona natural); `DNI|CE|PASSPORT` exigen nombres y `business_name` se ignora;
  tipo desconocido -> `422`. Con KYC exitoso el alta persiste la razon social en
  `identity.users.business_name` (RUC juridica: nombres `""`; el resto: `business_name`
  `NULL`). Duplicados -> `409 DUPLICATE_DOCUMENT`/`DUPLICATE_EMAIL`. La respuesta amplia
  `data` con:
  - `steps_verified: [str]` — **nombres** de los pasos de liveness verificados (no un
    conteo; para totales en UI usar `len`).
  - `steps_total: [str]` — **nombres** de todos los pasos del desafio.
  - `failed_step: str | null` — paso que fallo (del microservicio o derivado del primer
    `step_results` con `passed:false`).
  - `step_results: {paso: {passed, reason, details}}` — detalle por paso.
  - `overall_reason: str` — motivo integral del microservicio (se conserva ademas de
    `detail_code`).
- `POST /auth/kyc/document/validate` (E1-T30) -> request `{image_b64: str}` y
  `data: {is_valid: bool, issues: [str], checks: {}}`. `is_valid=false` es un 200 con los
  `issues` (no es error HTTP); solo los fallos de transporte/validacion se mapean a
  422/503/504/429. El backend valida base64/tamano/magic bytes y reenvia la imagen como
  `file` multipart (`KYC_DOCUMENT_TIMEOUT_SECONDS`, default 15 s); no persiste imagenes.
- `POST /auth/kyc/document/lookup` (E1-T35) -> request `{type: "DNI"|"RUC", number: str}`
  y `data: {document_type, first_name, last_name, business_name}`. Persona natural:
  `first_name`/`last_name` con valores y `business_name` vacio; RUC de persona juridica:
   `business_name` con la razon social y `first_name`/`last_name` vacios (campos no
   editables en el cliente; consume `F-T44`). El backend valida el formato antes de la
   red (`type` conocido, `number` solo digitos, DNI=8/RUC=11 -> `422 VALIDATION_ERROR`);
   luego prechequea en BD que el documento (DNI **y** RUC) no este ya registrado
   (`doc_number_hash` HMAC de `kyc_onboarding.hash_document_number` via
   `get_by_doc_hash`; E1-T37): si existe -> `409 DUPLICATE_DOCUMENT` con
   `"El documento ya se encuentra registrado"`, sin consulta externa y sin
   devolver datos del titular. Orden de chequeos: rate-limit -> `422` ->
   `409` -> proveedor. Solo si no existe, consulta desde el servidor
   `GET /dni/{numero}` o `GET /ruc/{numero}` contra
  `https://app.apiinti.dev/api/v1` (`APIINTI_BASE_URL`) con `Authorization: Bearer
  <APIINTI_API_KEY>` + `Content-Type: application/json` y normaliza la respuesta
  (punto unico de parseo: tolera `{"data": {...}}` o plana y claves alternativas;
  la forma exacta del JSON de apiinti se fija al probar contra el proveedor real).
   Errores neutros sin eco del numero ni del cuerpo del proveedor: `404
   DOCUMENT_NOT_FOUND`, `409 DUPLICATE_DOCUMENT` (documento ya registrado,
   antes de la red), `503/504 DOC_LOOKUP_UNAVAILABLE`, `429 RATE_LIMITED`
  (ventana en memoria compartida con el proxy KYC; prod: Redis/middleware). La key
  vive solo en el entorno del backend (`.env` de la raiz, cableada por
  `docker-compose.yml` al servicio `backend` con `${APIINTI_API_KEY:-}`); el
  frontend jamas la ve. Sin persistencia (pre-registro, read-only). Env:
  `DOC_LOOKUP_PROVIDER=mock|http` (default `mock`), `DOC_LOOKUP_TIMEOUT_SECONDS`
  (default 5), `DOC_LOOKUP_MAX_RETRIES` (default 2).

### 6.2 Cuentas y beneficiarios (`accounts`) - HU05, HU07

| Metodo | Ruta | Proposito |
|---|---|---|
| GET | `/accounts` | Consolidado de cuentas y saldos. |
| GET | `/accounts/totals` | Total contable consolidado por moneda (E1-T43, HU05; Bearer). |
| GET | `/accounts/{id}` | Detalle con disponible/retenido/contable. |
| GET | `/accounts/{id}/movements` | Movimientos paginados de `movements_view` (filtros: `date_from`/`date_to` fecha valor ISO y `direction` `DEBIT`/`CREDIT`, opcionales). Cada item incluye `entry_type` (E2-T35: concepto contable del asiento, `NULL` en filas legacy; el cliente lo traduce a etiqueta en español y oculta las patas internas `TRANSFER_HOLD`/`INTERBANK_HOLD`). |
| GET | `/accounts/{id}/movements/export` | Export de movimientos (MVP: CSV; `xlsx`/`pdf` solo si hay backend disponible, si no `501 EXPORT_FORMAT_NOT_SUPPORTED`). Filtros: `date_from`/`date_to` (fecha valor ISO; obligatorios en export) y `direction` (`DEBIT`/`CREDIT`, opcional). |
| GET/POST/PUT/DELETE | `/beneficiaries` | Gestion de beneficiarios frecuentes. |

> Contrato E1-T43 `GET /accounts/totals` (Bearer; HU05, hero `SALDO TOTAL` del
> dashboard): `200 {"data": {"as_of": "<ISO-8601>", "primary_currency": "PEN",
> "primary_total_minor": <int>, "totals": [{"currency": "PEN",
> "available_minor": <int>, "held_minor": <int>, "total_minor": <int>}, ...]},
> "meta": {"total": <n_monedas>}}`. Total contable consolidado por moneda
> calculado EN EL SERVIDOR desde la proyeccion `accounts.account_balances`
> (`total_minor = Σ(available_minor + held_minor)` por moneda; misma fuente que
> `GET /accounts`, para que cuadre con la lista); el cliente solo muestra el
> monto (cliente delgado, sin sumar). `primary_total_minor` es el consolidado
> PEN (0 si no hay cuentas PEN); `as_of` = `max(updated_at)` de las filas
> consideradas (`now(UTC)` si no hay filas); `meta.total` = numero de monedas.
> Sin 404 (usuario sin cuentas -> `200` con `totals: []`); errores `401
> NOT_AUTHENTICATED` (sin cabecera / `Bearer` invalido / `sub` no UUID).
> Solo lectura (sin `Idempotency-Key`, sin eventos). Dinero entero en centimos.

> Contrato E2-T13 beneficiarios `GET/POST/PUT/DELETE /api/v1/beneficiaries` (Bearer, HU07,
> `05#6.2`/G02): `GET /beneficiaries?q=<texto>` lista los beneficiarios `ACTIVE` del titular
> (hasta 100) o busca por alias/nombre en tiempo real `< 1 s` mediante indice ciego (`q`
> ausente/vacio = lista) -> `200 {"data": [Beneficiary], "meta": {"total", "q"}}`.
> `POST /beneficiaries` (alta) acepta `{alias, account_or_cci, [photo_url], [bank_code],
> [holder_name], [holder_doc], [currency]}` con `Idempotency-Key`
> opcional (recomendada; misma clave + mismo cuerpo = replay sin duplicar) -> `201 {"data":
> Beneficiary, "meta": {"replayed"}}`. `PUT /beneficiaries/{id}` (edicion parcial) y
> `DELETE /beneficiaries/{id}?confirm=true` (soft-delete, exige
> `confirm=true`) -> `200 {"data": Beneficiary, "meta": {}}`. `Beneficiary` expone `id`, `alias`,
> `photo_url`, `bank_code`, `account_masked` (`****` + ultimos 4), `holder_name`,
> `currency`, `status` (`ACTIVE`/`DELETED`), `created_at`/`updated_at`;
> **nunca** devuelve la cuenta/CCI en claro ni el documento (solo hash en BD). El
> `owner_user_id` sale del JWT, nunca del cuerpo. Nota E2-T33 (retiro del tope
> pre-aprobado, decision del dueno 2026-10-08): el campo historico
> `preapproved_limit_minor` ya no existe en el contrato; si un cliente antiguo lo
> envia en `POST`/`PUT`, se **ignora** (sin `422`) y nunca se devuelve en
> respuestas. Errores: `401 NOT_AUTHENTICATED`; `404
> NOT_FOUND` (inexistente/baja, id malformado o beneficiario ajeno no listado); `403
> NOT_AUTHORIZED` (beneficiario de otro titular en `PUT`/`DELETE`); `409 DUPLICATE_BENEFICIARY`
> (mismo alias o misma cuenta/CCI `ACTIVE` del titular); `409 IDEMPOTENCY_CONFLICT` (misma
> `Idempotency-Key` con cuerpo distinto); `400 VALIDATION_ERROR` (alias vacio, cuenta/CCI con
> formato invalido —CuyCash 14 digitos con DV valido, o CCI de 20 digitos—, moneda desconocida,
> o falta `confirm=true`). Dinero entero en centimos; datos sensibles cifrados en reposo (AES-256-GCM)
> e indexados para busqueda sin descifrar.

> Formato del numero de cuenta CuyCash (decision del dueno 2026-10-03, `E2-T28`): **14 digitos con
> digito verificador** — `001` (prefijo, 3) + `CCCCCCCCCC` (cuerpo, 10) + `D` (DV, 1). El DV es
> **modulo 11 con pesos ciclicos 2..7 de derecha a izquierda** sobre los 13 primeros digitos:
> `dv = 11 - (suma mod 11)`, mapeando `10`/`11` -> `0`. `accounts.accounts.account_number` sigue
> siendo `String(20)` UQ. En `GET /accounts` solo se expone `account_number_masked` (`****` +
> ultimos 4). `normalize_account_or_cci` de beneficiarios acepta una cuenta CuyCash de 14 digitos
> **con DV valido** o un CCI de 20 digitos (se retira el rango laxo `8..20`).

### 6.3 Transacciones (`transactions`) - HU06, HU08, HU15, HU17

| Metodo | Ruta | Proposito |
|---|---|---|
| POST | `/transfers/own` | Transferencia entre cuentas propias. |
| POST | `/transfers/third-party` | Transferencia a tercero (mismo banco). |
| POST | `/transfers/third-party/resolve-target` | Lookup previo de destino por DNI/celular (E2-T27). |
| POST | `/transfers/interbank` | Transferencia interbancaria (202 + estado). |
| POST | `/payments/qr` | Pago de QR (HU15). |
| POST | `/transactions/{id}/authorize` | Autorizacion biometrica de pago sensible (HU08). |
| POST | `/transactions/{id}/authorize/challenge` | Rotacion del reto tras `EXPIRED_NONCE` (HU08). |
| GET | `/transactions/{id}` | Estado y detalle. |
| GET | `/transactions` | Historial filtrable. |
| GET | `/transactions/{id}/receipt` | Comprobante. |
| POST | `/transactions/{id}/reverse` | Reverso autorizado (operaciones). |

> Nota de ownership (`docs/18` G74, `E4-T12`): `POST /payments/qr`
> figura en esta tabla por descubrimiento, pero la implementa
> `wallet` (`api/payments.py`, detalle en `docs/05#6.5`); el caso de
> uso es de billetera (HU15), no de transferencias.

> Contrato E2-T07 `POST /transfers/own` y `POST /transfers/third-party` (Bearer, HU06):
> request `{source_account_id, target_account_id, amount_minor (> 0), currency (ISO-4217),
> [fee_minor (>= 0, solo tercero; `None` = `transfer.third_party_fee_minor` de
> `config.parameters`, default `0`)], [external_ref], [metadata]}` con cabecera
> `Idempotency-Key` obligatoria -> `201 {"data": {"transaction_id", "type"
> (`OWN_TRANSFER`/`THIRD_PARTY_TRANSFER`), "status" (`SETTLED`/`REJECTED`),
> "source_account_id", "target_account_id", "amount_minor", "currency", "fee_minor",
> "failure_code" (`INSUFFICIENT_FUNDS` solo si `REJECTED`)}, "meta": {"request_id",
> "replayed"}}`. La propia es atomica sin hold (un asiento `2000-S -> 2000-D`, sin
> comision); la de tercero es hold+settle con la comision a `4100`. En
> `/transfers/third-party` `target_account_id` pasa a **opcional** si se envia
> `beneficiary_id` (E2-T30, ver abajo); en `/transfers/own` sigue obligatorio. Errores:
> `401 NOT_AUTHENTICATED`, `422` (falta `Idempotency-Key`, esquema invalido o
> `amount_minor <= 0` / `fee_minor < 0`, que el esquema rechaza con `ge=1`/`ge=0`),
> `400 VALIDATION_ERROR` (misma cuenta, moneda incoherente),
> `404 NOT_FOUND` (cuenta inexistente), `403 NOT_AUTHORIZED` (origen ajeno o
> cuenta no `ACTIVE`), `409 CONFLICT` (misma clave + cuerpo distinto). Saldo
> insuficiente = `201` con `status=REJECTED` (sin tocar saldos; reintentable con la
> misma clave). Solo UUIDs en respuestas (regla de oro 9).
>
> Contrato E2-T30 destino por beneficiario de la agenda `POST /transfers/third-party` (Bearer,
> HU06/HU07): el request de tercero admite dos formas de destino: (a) `target_account_id` de una
> cuenta real (comportamiento `E2-T07`) o (b) `beneficiary_id` (campo nuevo **opcional**) o
> `metadata["beneficiary_id"]`, resuelto server-side a la cuenta real del beneficiario.
> **Precedencia:** si `target_account_id` viene y resuelve (`accounts.service.get_account`), gana
> tal cual (el beneficiario solo habilita la exencion por tope, `E2-T14`); si falta o no resuelve y
> hay beneficiario, se resuelve por `accounts.service.beneficiaries.get_for_operation` ->
> `account_or_cci` -> `accounts.service.get_account_by_number` -> `account_id` real; sin ninguno de
> los dos -> `400 VALIDATION_ERROR` ("destino requerido"). Solo resuelven cuentas CuyCash de
> **14 digitos**; un **CCI de 20** de otro banco o un numero sin cuenta -> `404 NOT_FOUND`
> **neutro** (mismo cuerpo "cuenta destino inexistente", sin revelar si el numero existe), y **no**
> se deriva automaticamente a `POST /transfers/interbank` (encadenar la saga es tarea aparte). Un
> beneficiario inexistente, ajeno o dado de baja colapsa al **mismo `404 NOT_FOUND` neutro**
> (aislamiento por titular; nunca `403` por el beneficiario). La resolucion es de solo lectura y
> ocurre antes de consumir la `Idempotency-Key`; la respuesta (`TransferResponse`) no cambia y el
> `target_account_id` devuelto es el de la cuenta real resuelta. Sin PII: el `account_or_cci` en
> claro nunca va a logs ni respuestas (la API solo devuelve UUIDs). El schema rechaza campos
> desconocidos (`extra="forbid"`; no acepta identificadores del lookup previo de `E2-T27`). Sin
> eventos nuevos (`transaction.initiated`/`funds.held`/`transfer.settled` intactos;
> `transaction.initiated` conserva `exemption_reason`/`beneficiary_id` de `E2-T14`).
>
> Contrato E2-T27 lookup previo de destino por DNI/celular (Bearer, HU06; **decision del dueno
> 2026-10-03**): `POST /api/v1/transfers/third-party/resolve-target` (solo lectura, **sin**
> `Idempotency-Key`; no mueve dinero) resuelve la cuenta destino de un tercero **antes** de
> confirmar la transferencia. Request: exactamente UNO de `target_document`
> (`{type: DNI|CE|PASSPORT, number}`) **o** `target_phone` (mutuamente excluyente; ambos/ninguno ->
> `422`) -> `200 {"data": {"target_account_id", "holder_name_masked",
> "account_number_masked" (`****dddd`), "currency"}, "meta": {"request_id"}}`. El cliente muestra
> el titular enmascarado y luego ejecuta `POST /transfers/third-party` con ese
> `target_account_id`. El **POST de la transferencia NO se extiende con `target_document`/
> `target_phone`** (se descarto esa via; el destino por beneficiario de la agenda es otra cosa,
> ver `E2-T30` arriba). Errores: `401 NOT_AUTHENTICATED`;
> `404 TARGET_NOT_FOUND` **neutro** (mismo cuerpo si el DNI/celular no existe o si el usuario no
> tiene cuenta `ACTIVE`; anti-enumeracion); `409 TARGET_AMBIGUOUS` (el usuario resuelto tiene
> **varias cuentas `ACTIVE`**: no se elige "la principal", se pide usar el numero de cuenta);
> `422 VALIDATION_ERROR` (formato de documento/celular o combinacion de campos). Sin PII en
> respuestas ni logs (solo UUID + titular/cuenta enmascarados); el numero en claro nunca se
> devuelve.
>
> Contrato E2-T09 saga interbancaria `POST /api/v1/transfers/interbank` y
> `GET /api/v1/transactions/{id}` (Bearer, HU06): request
> `{source_account_id, amount_minor (> 0), currency (ISO-4217), cci (20 digitos),
> [holder_name (max 150)], [bank_code (max 6)], [fee_minor (>= 0; `None` =
> `transfer.interbank_fee_minor` de `config.parameters`, default `0`)],
> [external_ref (max 80; unica)], [metadata]}` con cabecera `Idempotency-Key`
> obligatoria. Respuesta `202 {"data": {"transaction_id", "type"
> (`INTERBANK_TRANSFER`), "status" (`FUNDS_HELD`/`POSTED`/`SETTLED`/`FAILED`/
> `REVERSED`/`REJECTED`), "source_account_id", "amount_minor", "currency",
> "fee_minor", "external_ref", "tracking": {"poll_url", "status", "external_ref",
> "gateway_ref", "attempts", "expires_at", "holder_name_masked"}, "failure_code"
> (`INSUFFICIENT_FUNDS`/`TRANSFER_TIMEOUT`/`INTERBANK_REJECTED`)}, "meta":
> {"request_id", "replayed"}}`. La saga valida el titular/CCI (E2-T08) y la cuenta
> origen antes de mover dinero; retiene el total, envia por la pasarela simulada y
> sigue por polling (`GET /transactions/{id}`). `GET /api/v1/transactions/{id}`
> devuelve `200 {"data": {..., "external_ref", "tracking" (solo si
> `type=INTERBANK_TRANSFER`; `{}` en otro caso), "failure_code", "history":
> [{"from_status", "to_status", "reason", "actor_type", "created_at"}]}, "meta":
> {"request_id"}}`. Errores: `401 NOT_AUTHENTICATED`; `422 VALIDATION_ERROR` (falta
> `Idempotency-Key`, esquema invalido o CCI con formato distinto de 20 digitos);
> `404 HOLDER_NOT_FOUND` (titular no coincide o CCI inexistente, mismo error
> neutro) y `404 NOT_FOUND` (cuenta/transaccion inexistente o id malformado);
> `403 NOT_AUTHORIZED` (cuenta/transaccion de otro titular o cuenta no `ACTIVE`);
> `429 RATE_LIMITED`; `503 HOLDER_LOOKUP_UNAVAILABLE` (timeout/caida del lookup);
> `409 CONFLICT` (misma clave + cuerpo distinto, o `external_ref` ya usada).
> Saldo insuficiente = `202` con `status=REJECTED` + `failure_code=INSUFFICIENT_FUNDS`
> (sin tocar saldos). El detalle es solo del titular. Solo UUIDs en respuestas
> (regla de oro 9); sin PII (CCI/titular solo como hash/mascara).
>
> Contrato E2-T10 comprobante `GET /api/v1/transactions/{id}/receipt` (Bearer, HU06 CA-04,
> `05#6.3`/G01/G15): solo lectura (sin `Idempotency-Key`; no mueve dinero) -> `200` con cuerpo
> PDF (`Content-Type: application/pdf`, `Content-Disposition: attachment;
> filename="comprobante-<OP-...>.pdf"`). El numero de operacion es **derivado determinista** del
> `transaction_id` (`OP-` + 12 hex de `sha256`, mayusculas): unico por transaccion, estable
> entre reintentos y no secuencial/adivinable; no se persiste ni crea asientos/filas. El
> comprobante incluye fecha/hora UTC, tipo, estado, montos en centimos (`amount_minor`/
> `fee_minor`/`total_minor`), moneda, cuentas **enmascaradas** (`****1234`; interbancaria:
> `CCI **** (banco <code>)` + titular enmascarado) y `external_ref`; jamas documento, CCI
> completo ni titular en claro. Errores: `404 NOT_FOUND` (transaccion inexistente o id
> malformado, neutro), `403 NOT_AUTHORIZED` (ajena), `409 VALIDATION_ERROR` (estado no
> emitible: solo `POSTED`/`SETTLED`/`CONCILIATED`) y `501 RECEIPT_FORMAT_NOT_SUPPORTED` (no hay
> backend PDF disponible, patron `E2-T03`).
>
> Contrato E2-T19 `POST /api/v1/transactions/{id}/authorize` (Bearer, HU08,
> `05#6.3`/G01): completa la autorizacion biometrica de un pago sensible en
> `PENDING_AUTHORIZATION`. Request `{nonce, signature, device_id}` con
> cabecera `Idempotency-Key` obligatoria -> `200 {"data": {"transaction_id",
> "status"}, "meta": {"request_id", "replayed"}}`. Con firma valida contra el
> `device_binding` vigente (fachada `identity`, D-03) consume el `nonce` (un
> solo uso, TTL) y continua `AUTHORIZED -> FUNDS_HELD -> POSTED -> SETTLED`
> reutilizando el hold sin duplicar asiento; el replay con la misma clave
> devuelve el mismo resultado. Solo el titular puede autorizarla. Errores:
> `401 NOT_AUTHENTICATED` (sin token), `403 NOT_AUTHORIZED` (transaccion
> ajena o firma/binding invalidos; cuerpo **generico** sin oraculo),
> `400 EXPIRED_NONCE` (reto expirado o `nonce` reutilizado), `404 NOT_FOUND`
> (transaccion inexistente o id malformado), `409 CONFLICT` (estado distinto
> de `PENDING_AUTHORIZATION`, o misma `Idempotency-Key` con cuerpo distinto),
> `422` (falta `Idempotency-Key` o payload malformado). La firma invalida no
> mueve dinero ni crea asiento; el evento de seguridad del fallo lo emite
> `E2-T17`/`E2-T20` (`auth.failed_attempt`). El endpoint **no recalcula** el
> umbral (lo decidio `E2-T17`). Sin PII ni secretos en logs.
>
> Contrato E2-T31 factor PIN en `POST /api/v1/transactions/{id}/authorize` (Bearer,
> HU08): el mismo endpoint admite **dos factores mutuamente excluyentes**:
> `{device_id, signature[, nonce]}` (E2-T19) **o** `{pin}` (PIN de seguridad del
> titular). El esquema exige **exactamente uno**: `signature`+`pin` juntos o
> ninguno -> `422`; `device_id` es obligatorio solo con `signature`; `nonce` solo
> aplica a la firma (con `pin` se ignora de forma neutra y en la via PIN no existe
> `EXPIRED_NONCE`). El PIN se verifica contra la credencial del titular por la
> fachada de `identity` (`pin_authorization.verify_holder_pin`, comparacion en
> tiempo constante; sin credencial o sin PIN usa una rama ciega) y, en exito, el
> flujo continua identico a la firma (`AUTHORIZED -> FUNDS_HELD -> POSTED ->
> SETTLED`, hold reutilizado). El PIN incorrecto y cualquier desajuste comparten el
> **mismo `403 NOT_AUTHORIZED` neutro** de la firma invalida, cuentan como intento
> fallido (mismo contador/bloqueo preventivo CA-02 de `E2-T17`) y emiten
> `auth.failed_attempt` sin motivo. Errores: `401 NOT_AUTHENTICATED`; `422`
> (falta `Idempotency-Key`, esquema invalido, o `signature`/`pin` juntos o
> ninguno); `404 NOT_FOUND` (inexistente/id malformado, neutro); `403
> NOT_AUTHORIZED` (ajena, PIN incorrecto, titular sin PIN o firma/binding
> invalido: generico, sin oraculo); `400 EXPIRED_NONCE` (solo firma); `409
> CONFLICT` (estado distinto de `PENDING_AUTHORIZATION` o misma clave con cuerpo
> distinto). Bloqueo preventivo -> `200` con `status=REJECTED` +
> `failure_code=BIOMETRIC_PREVENTIVE_BLOCK`. **El PIN nunca se refleja en
> respuestas ni logs**: el snapshot de idempotencia guarda solo su HMAC-SHA256
> con el secreto del servidor (`pin_digest`, G58), no el valor, y el handler
> de `422` devuelve `detail` con solo
> `loc`/`type`/`msg` (sin `input` ni `ctx`, que expondrian el body crudo).
>
> Contrato reintento tras `EXPIRED_NONCE`
> `POST /api/v1/transactions/{id}/authorize/challenge` (Bearer, HU08): rota
> el reto vigente de una operacion en `PENDING_AUTHORIZATION` (via
> `biometric.request_biometric_challenge` de `E2-T17`: `nonce` nuevo +
> vencimiento nuevo, conserva `failed_attempts`) ->
> `200 {"data": {"transaction_id", "nonce", "expires_in"}, "meta":
> {"request_id"}}`. Solo rotacion: no mueve dinero ni cuenta intento (sin
> `Idempotency-Key`, como `GET /transactions/{id}`; repetir la llamada
> reemite el reto). El cliente firma el `nonce` nuevo y reintenta
> `POST .../authorize` con una `Idempotency-Key` NUEVA (el fallo
> `EXPIRED_NONCE` quedo snapshotteado bajo la anterior: misma clave + cuerpo
> distinto = `409`; mismo cuerpo = replay del error). Errores:
> `401 NOT_AUTHENTICATED`, `404 NOT_FOUND` (inexistente), `403
> NOT_AUTHORIZED` (ajena), `409 CONFLICT` (no esta en
> `PENDING_AUTHORIZATION`). Sin PII ni secretos en logs.
>
> Contrato E2-T24 `GET /api/v1/transactions` (Bearer, HU06/HU15/HU17,
> `05#6.3`/G16): historial paginado de las operaciones **del titular**
> (`initiator_user_id` del JWT). Query opcional `date_from`/`date_to`
> (ISO-8601 UTC, sobre `created_at`), `type` (`transaction_type`), `status`
> (`transaction_status`), `page` (`>= 1`) y `page_size` (default 20, max 100)
> -> `200 {"data": [ {"transaction_id", "type", "status", "amount_minor",
> "fee_minor", "currency", "created_at", "settled_at", "external_ref",
> "source_account_masked", "target_account_masked"} ], "meta": {"page",
> "page_size", "total", "request_id"}}`. Orden determinista `created_at DESC,
> id`; las cuentas origen/destino viajan **siempre enmascaradas** (`****1234`),
> sin numero completo, documento, CCI ni titular. Solo lectura (no mueve
> dinero; sin `Idempotency-Key`). Errores: `401 NOT_AUTHENTICATED`; `422`
> (fechas invertidas o `type`/`status`/`page`/`page_size` invalidos). El
> listado nunca devuelve operaciones de otro titular (`CA-04`).
>
> Contrato E2-T25 `POST /api/v1/transactions/{id}/reverse` (Bearer,
> HU06/HU17/HU18, `05#6.3`/G17): compensa una operacion con un **asiento
> inverso** creado solo via `ledger.service.reverse_entry` (referencia
> `reverses_entry_id`; el original y sus `postings` permanecen intactos), sin
> editar ni borrar nada. Request opcional `{reason?}` (max 280) con cabecera
> `Idempotency-Key` obligatoria -> `200 {"data": {"transaction_id", "status"
> (`REVERSED`), "reversal_entry_id", "reverses_entry_id",
> "reversal_entry_ids", "reverses_entry_ids"}, "meta": {"request_id",
> "replayed"}}`. **Estados permitidos (G17):** `POSTED`/`SETTLED`/
> `CONCILIATED` -> `REVERSED` (terminal); `INITIATED`/`VALIDATED`/
> `PENDING_AUTHORIZATION`/`AUTHORIZED`/`FUNDS_HELD` piden cancelacion/
> liberacion de hold (no reverso) y `REJECTED`/`FAILED`/`REVERSED` son
> terminales. Idempotente: misma clave + mismo cuerpo = misma respuesta sin
> duplicar el asiento inverso; `INTERBANK_TRANSFER` delega en la saga
> (`E2-T09`). Emite `transfer.reversed` por `outbox` (y el
> `ledger.entry.posted` del inverso, `E2-T23`). Errores: `401
> NOT_AUTHENTICATED`, `403 NOT_AUTHORIZED` (ajena, generico), `404 NOT_FOUND`
> (inexistente/id malformado), `409 CONFLICT` (estado no permitido, misma
> clave con cuerpo distinto, o carrera), `422` (falta `Idempotency-Key` o
> esquema). Cuadre del inverso: `sum(debitos)=sum(creditos)` por moneda; el
> original + inverso suman cero por cuenta.
>
> Nota E2-T33 (retiro del tope pre-aprobado, decision del dueno 2026-10-08,
> `8-10-2026-2.txt` D1): el contrato E2-T14/E2-T32 (exencion biometrica por
> `preapproved_limit_minor`, `exemption_reason="PREAPPROVED_LIMIT"` en
> `transaction.initiated` y `PreapprovedCurrencyMismatchError`) queda
> **retirado**. Toda transferencia sobre `transfer.biometric_threshold_minor`
> exige autorizacion (`PENDING_AUTHORIZATION`), tenga o no `beneficiary_id`;
> `transaction.initiated` ya no lleva `exemption_reason`/`beneficiary_id` de
> exencion. El destino por beneficiario de la agenda (E2-T30) sigue intacto.
> Sin endpoint nuevo (reusan las transferencias existentes).

### 6.4 Creditos (`credits`) - HU09-HU12

| Metodo | Ruta | Proposito |
|---|---|---|
| GET | `/credits/products` | Productos y tasas. |
| POST | `/credits/simulate` | Simulacion de cuota/cronograma. |
| POST | `/credits/applications` | Solicitud + autorizacion de bureau. |
| GET | `/credits/applications/{id}` | Estado del expediente. |
| GET | `/credits/applications` | Mis créditos del titular (sin `status`). |
| GET | `/credits/applications?status=IN_REVIEW` | Bandeja en revision (analista). |
| POST | `/credits/applications/{id}/review` | Dictamen manual del analista. |
| POST | `/credits/applications/{id}/contract` | Genera contrato. |
| POST | `/credits/applications/{id}/sign` | Firma con biometria. |
| POST | `/credits/applications/{id}/disburse` | Desembolsa el prestamo (E3-T17; gap G62 cerrado). |
| GET | `/credits/loans` | Prestamos del cliente. |
| GET | `/credits/loans/{id}/schedule` | Cronograma definitivo. |
| POST | `/credits/loans/{id}/pay` | Pago de cuota. |
| PUT | `/credits/loans/{id}/auto-debit` | Debito automatico on/off (E3-T19). |
| POST | `/credits/simulate/export` | PDF del cronograma simulado (E3-T03; gap G21 cerrado). |

> Detalle del simulador (E3-T03 `Hecho`, 2026-10-09; HU09 CA-01..CA-04).
> Auth: `Authorization: Bearer <jwt>` en las tres rutas (401 `NOT_AUTHENTICATED`
> sin token valido). Envoltorio `05#4` (`{"data", "meta"}`) en JSON; el export
> devuelve `application/pdf` (`attachment; filename="simulacion-credito-<code>.pdf"`).
>
> - `GET /credits/products` -> `200 {"data": [producto], "meta": {"total": n}}`
>   (solo `enabled`, ordenados por `code`). Producto: `id`, `code`, `name`,
>   `tcea`/`tea`/`insurance_rate` (**strings decimales exactos**, `NUMERIC(18,8)`
>   sin perdida en JSON), `min_amount_minor`/`max_amount_minor` (centimos `int`),
>   `min_term_months`/`max_term_months`, `allowed_terms` (`null` = solo rango),
>   `enabled`.
> - `POST /credits/simulate` (cuerpo `{product_id?, amount_minor, term_months,
>   tcea?, insurance? = false}`; exactamente uno de `product_id`/`tcea`, sin
>   `float`) -> `200 {"data": {product? (id/code/name), amount_minor,
>   term_months, insurance, tea, tcea (strings), payment_minor (cuota base
>   francesa), total_principal_minor, total_interest_minor,
>   total_insurance_minor, total_paid_minor, schedule[]}, "meta": {}}`.
>   Fila = `installment_no`, `due_date: null` (efimero, sin desembolso),
>   `principal_minor`, `interest_minor`, `insurance_minor`, `total_minor`,
>   `balance_minor` (forma de `03b#84` + saldo posterior, sin persistir).
> - Reglas: con `product_id` la tasa manda del catalogo (`tea`, o su `tcea` si
>   `tea` es `NULL`) y monto/plazo respetan topes + `allowed_terms`; sin
>   producto se acepta `tcea` explicita en `0..5` (500% anual), monto
>   `1..10^12` centimos, plazo `1..360` meses, desgravamen `0`.
>   El desgravamen usa `insurance_rate` del producto (o `0`).
> - `POST` con `200` (no `201`): **no crea recurso** (calculo puro y efimero:
>   no genera `loan_applications`, no toca `transactions`/`ledger`, no emite
>   eventos), por eso **no exige `Idempotency-Key`** (cabeceras `05#3` no
>   aplican a operaciones que no mueven dinero).
> - `POST /credits/simulate/export`: mismo cuerpo y validaciones que
>   `simulate`; devuelve el mismo cronograma en PDF (titulo + parametros +
>   tabla, sin PII). Sin backend PDF (`reportlab`/`fpdf2`/`weasyprint`) ->
>   `501 EXPORT_FORMAT_NOT_SUPPORTED` sin romper `simulate`.
> - Codigos: `404 NOT_FOUND` (producto), `422 VALIDATION_ERROR` (monto/plazo
>   fuera de rango, plazo no permitido, tasa negativa o fuera de tope, cuerpo
>   incompleto o con ambas fuentes de tasa), `422` de esquema Pydantic,
>   `501 EXPORT_FORMAT_NOT_SUPPORTED` (solo export).
>
> Detalle del expediente (E3-T08 `Hecho`, 2026-10-09; HU10 CA-01..CA-04;
> cierra `docs/18` G21/G25).
> Auth: `Authorization: Bearer <jwt>` en las cuatro rutas (401 `NOT_AUTHENTICATED`
> sin token valido). Envoltorio `05#4` (`{"data", "meta"}`) en JSON.
> - `POST /credits/applications` (cuerpo `{product_id, requested_amount_minor
>   (> 0), term_months, total_income_minor (> 0), total_debt_minor (>= 0),
>   bureau_authorized (= `true`)}`, montos en centimos `int`, sin `float`;
>   cabecera `Idempotency-Key` obligatoria) -> `201 {"data": {id, user_id,
>   product_id, requested_amount_minor, term_months, status
>   (`APPROVED`/`REJECTED`), bureau_authorized, bureau_authorized_at,
>   score, dti_ratio (string `NUMERIC(12,4)`, informativo y acotado a
>   `±99999999.9999` al persistir sin cambiar el dictamen), total_income_minor,
>   total_debt_minor (= declarada + externa del bureau), decision_reason,
>   created_at, updated_at, decided_at}, "meta": {"replayed"}}` con dictamen
>   final, o `202` con la misma forma si se difiere (`IN_REVIEW`).
>   Un DTI extremo con entrada valida por esquema (p.ej. ingreso minimo +
>   deuda maxima) nunca es `500`: el motor rechaza por DTI y el expediente
>   persiste con el `dti_ratio` acotado (`422` solo si el numerico es
>   invalido; red de seguridad `DataError -> VALIDATION_ERROR`).
>   Sin `bureau_authorized=true` -> `422 VALIDATION_ERROR` sin evaluar ni
>   consultar al bureau; monto/plazo fuera del producto -> `422`;
>   producto inexistente -> `404 NOT_FOUND`; sin `Idempotency-Key` -> `422`;
>   misma clave + distinto cuerpo -> `409 IDEMPOTENCY_CONFLICT`; misma clave
>   + mismo cuerpo -> replay (`meta.replayed=true`) sin duplicar expediente
>   ni notificacion. Bureau caido tras reintentos -> `202 IN_REVIEW` (no
>   error 500). Flujo `SUBMITTED -> IN_EVALUATION ->
>   {APPROVED|REJECTED|IN_REVIEW}`; decidido no se reevalua (`409 CONFLICT`
>   en revision). Eventos por `outbox`: `credit.application.submitted` +
>   `credit.approved` / `credit.rejected` / `credit.review.required`.
> - `GET /credits/applications/{id}` -> `200 {"data": {expediente}, "meta": {}}`
>   (solo el titular; ajeno/inexistente -> `404 NOT_FOUND` neutro).
> - `GET /credits/applications` (E3-T23 backend `Hecho`, 2026-10-11;
>   HU10/HU11 "Mis créditos") -> `200 {"data": [{id, product_id,
>   product_name, requested_amount_minor, term_months, status,
>   decision_reason, created_at, updated_at, decided_at}],
>   "meta": {total, limit, offset}}` con los expedientes **del titular
>   autenticado** (`sub` del JWT; el `user_id` nunca viaja en el
>   request), orden `created_at DESC, id`; titular sin expedientes ->
>   `data: []` (no `404`). Vista minima: sin ingresos/deuda/score/
>   consentimiento/documento (regla de oro 7); la UI muestra `status`/
>   `decision_reason` TAL CUAL (cliente delgado). Paginacion
>   `limit` (1..100, defecto 50) / `offset` (defecto 0); `total` es el
>   conteo del titular. Sin `Authorization` -> `401 NOT_AUTHENTICATED`;
>   paginacion invalida -> `422`. Lectura sin `Idempotency-Key` (no
>   crea recurso ni mueve dinero). Comparte ruta con la bandeja por
>   decision 1 de E3-T23 (`docs/18` G59): **sin `status` -> titular**;
>   `?status=IN_REVIEW` -> analista (rol, `403` sin el; otro `status`
>   -> `422`); la bandeja devuelve el expediente completo intacto.
> - `GET /credits/applications?status=IN_REVIEW` (rol analista de credito
>   `analyst`/`credit_analyst` en el claim `roles`; `403 NOT_AUTHORIZED`
>   sin el) -> `200 {"data": [...], "meta": {total, limit, offset}}`
>   (solo lista `IN_REVIEW`; otro `status` -> `422`).
> - `POST /credits/applications/{id}/review` (rol analista; cuerpo
>   `{decision: APPROVED|REJECTED, reason (1..2000)}`) -> `200` con el
>   expediente resuelto; no `IN_REVIEW` o ya decidido -> `409 CONFLICT`;
>   el solicitante no puede dictaminar su propio expediente aunque traiga
>   rol analista (`409 CONFLICT`, separacion de funciones);
>   inexistente -> `404`. La aprobacion queda separada de la solicitud y
>   auditada (`credit.application.reviewed`); notificacion push al titular
>   (plantillas `credit_application_approved` / `..._rejected` /
>   `credit_review_required`, sin PII ni cifras).
>
> > Detalle del juego contractual (E3-T12 `Hecho`, 2026-10-11; HU11 CA-01;
> > decision del dueno: **tres documentos separados**, no un PDF combinado).
> > Auth: `Authorization: Bearer <jwt>` en ambas rutas (401 `NOT_AUTHENTICATED`
> > sin token valido). Envoltorio `05#4` (`{"data", "meta"}`) en JSON; la
> > descarga devuelve `application/pdf` (`attachment`).
> > - `POST /credits/applications/{id}/contract` (sin cuerpo: las condiciones
> >   se copian del expediente `APPROVED` + producto + motor E3-T01;
> >   cabecera `Idempotency-Key` obligatoria) -> `201 {"data":
> >   {application_id, status ("GENERATED"), documents: [{contract_id,
> >   doc_type (`CONTRATO`/`PAGARE`/`RESUMEN`), doc (`contrato`/`pagare`/
> >   `resumen`), doc_ref, doc_hash, status}]}, "meta": {"replayed"}}` con el
> >   juego generado, o `200` con la misma forma si ya existia (idempotente
> >   por naturaleza via UQ `(application_id, doc_type)`: otra clave sobre el
> >   mismo expediente devuelve el existente sin duplicar filas ni eventos).
> >   `doc_hash` = SHA-256 del contenido canonico (monto/plazo/TEA/TCEA/
> >   cronograma + motivo, reproducible). Evento por `outbox` (uno por
> >   expediente): `credit.contract.generated` (`contract_ids`,
> >   `application_id`, `user_id`, `status`, sin PII); auditoria best-effort
> >   `credit.contract.generated`. **No mueve dinero** (sin `loans`/
> >   `loan_schedules`, sin asientos; el desembolso es HU12).
> > - `GET /credits/applications/{id}/contract` (solo lectura, sin
> >   `Idempotency-Key`; no genera ni emite) -> `200` con los metadatos del
> >   juego (`?format=json`, defecto) o `200 application/pdf` con
> >   `?format=pdf&doc=contrato|pagare|resumen` (defecto `contrato`;
> >   re-render determinista desde las condiciones aprobadas).
> > - Codigos: `404 NOT_FOUND` (expediente inexistente/ajeno o sin juego,
> >   neutro), `409 CONFLICT` (expediente no `APPROVED`),
> >   `409 IDEMPOTENCY_CONFLICT` (misma clave con otro expediente),
> >   `422 VALIDATION_ERROR` (sin `Idempotency-Key`, `format`/`doc`
> >   invalidos), `501 EXPORT_FORMAT_NOT_SUPPORTED` (solo descarga sin
> >   backend PDF; la generacion/persistencia sigue intacta, patron
> >   E3-T03/E2-T10).
> >
> > Detalle de la firma + copia por correo (E3-T14 `Hecho`, 2026-10-11;
> > HU11 CA-02/CA-03/CA-04; decision del dueno: biometria + fallback PIN).
> > Auth: `Authorization: Bearer <jwt>` en ambas rutas (401 `NOT_AUTHENTICATED`
> > sin token valido). Envoltorio `05#4` (`{"data", "meta"}`).
> > - `POST /credits/applications/{id}/sign/challenge` (cuerpo opcional
> >   `{device_id?}`; **sin** `Idempotency-Key`: solo rota el reto, no
> >   mueve dinero, patron G39) -> `200 {"data": {application_id, nonce,
> >   expires_in (120 s), status ("GENERATED")}, "meta": {"request_id"}}`.
> >   Repetir reemite el reto. Sin juego -> `404 NOT_FOUND` neutro; ya
> >   `SIGNED` -> `409 CONFLICT`; `device_id` malformado -> `422`.
> > - `POST /credits/applications/{id}/sign` (dos vias mutuamente
> >   excluyentes: `{device_id, signature[, nonce]}` o `{pin}`; exactamente
> >   un factor o `422`; cabecera `Idempotency-Key` obligatoria) ->
> >   `200 {"data": {application_id, contract_id, status ("SIGNED"),
> >   signed_at, via ("device"|"pin"), documents: [{contract_id, doc_type,
> >   doc, doc_ref, doc_hash, status, signed_at, seal}]}, "meta":
> >   {"request_id", "replayed"}}`. Misma clave + mismo cuerpo = replay
> >   (una firma y **un** correo); misma clave + cuerpo distinto ->
> >   `409 IDEMPOTENCY_CONFLICT`. El fallo del factor se snapshottea
> >   (reintento identico = mismo error; reintento real = reto rotado +
> >   clave nueva, patron E2-T21). Tras el exito se encola la copia por
> >   correo (`notifications.send(channel="email",
> >   template_code="credit_contract_signed")`, best-effort: un fallo del
> >   proveedor deja la firma `SIGNED` y la notificacion `FAILED`).
> > - Codigos: `403 NOT_AUTHORIZED` (firma/binding/PIN invalido, generico
> >   sin oraculo), `400 EXPIRED_NONCE` (reto vencido/reutilizado fuera de
> >   ventana), `404 NOT_FOUND` (ajeno/inexistente, neutro), `409 CONFLICT`
> >   (no `GENERATED`/ya `SIGNED`), `422 VALIDATION_ERROR` (sin
> >   `Idempotency-Key`, esquema o ambos/ningun factor; el `422` sanitario
> >   no refleja el cuerpo, el PIN nunca fuga). **No mueve dinero** (sin
> >   `loans`, sin asientos; el desembolso es HU12).
> >
> > Detalle del desembolso + lectura de prestamos (E3-T17 `Hecho`,
> > 2026-10-13; HU12 CA-01/CA-02/CA-03-tiempo; gap G62 cerrado para estas
> > rutas; `PUT .../auto-debit` y `POST .../pay` son de E3-T19/E3-T20).
> > Auth: `Authorization: Bearer <jwt>` en las tres rutas (401 `NOT_AUTHENTICATED`
> > sin token valido). Envoltorio `05#4` (`{"data", "meta"}`) en JSON.
> > - `POST /credits/applications/{id}/disburse` (cuerpo `{account_id?}`
> >   (UUID; omitido = cuenta principal del titular, la `ACTIVE` mas
> >   antigua); cabecera `Idempotency-Key` obligatoria: **mueve dinero**) ->
> >   `201 {"data": {loan_id, application_id, transaction_id, status
> >   ("SETTLED"), disbursed_at, account_masked ("****dddd"), amount_minor,
> >   currency}, "meta": {"request_id", "replayed"}}` con el prestamo creado
> >   y acreditado, o `200` con la misma forma si es replay (misma clave +
> >   mismo cuerpo, o reintento con otra clave sobre un expediente ya
> >   desembolsado: un prestamo, un asiento, un evento). El dinero viaja
> >   por el motor (`Transaction LOAN_DISBURSEMENT`, un asiento
> >   `Debe 5000-Cartera / Haber 2000-Cuenta`, `docs/04#7.5`); monto =
> >   `requested_amount_minor` del expediente; sin holds ni comision.
> > - `GET /credits/loans` (solo lectura, sin `Idempotency-Key`) ->
> >   `200 {"data": [{id, application_id, account_id, principal_minor,
> >   outstanding_principal_minor, tcea (string), term_months, status,
> >   disbursed_at, auto_debit_enabled, created_at}], "meta": {total,
> >   request_id}}` con los prestamos del titular (`data: []` si no tiene).
> > - `GET /credits/loans/{id}/schedule` (solo lectura, sin
> >   `Idempotency-Key`) -> `200 {"data": {loan, schedule:
> >   [{installment_no, due_date, principal_minor, interest_minor,
> >   insurance_minor, total_minor, status, paid_at}]}, "meta":
> >   {"request_id"}}` con el cronograma definitivo.
> > - Codigos: `404 NOT_FOUND` (expediente/prestamo/cuenta ajeno o
> >   inexistente, neutro; tambien sin cuenta `ACTIVE` cuando se omite
> >   `account_id`), `409 CONFLICT` (expediente no `APPROVED`, juego no
> >   `SIGNED`) / `409 IDEMPOTENCY_CONFLICT` (misma clave con cuerpo
> >   distinto), `422 VALIDATION_ERROR` (sin `Idempotency-Key`, cuenta no
> >   `ACTIVE`, moneda incoherente o cuerpo invalido). Evento por `outbox`:
> >   `credit.disbursed` (`loan_id`, `application_id`, `user_id`,
> >   `transaction_id`, `amount_minor`, `currency`, sin PII).
> >
> > > Detalle del pago de cuota (E3-T20 `Hecho`, 2026-10-13; HU12 CA-02
> > > parte de cuotas; gap G62 cerrado para esta ruta; MVP: pago por
> > > cuota **total**, sin abonos parciales ni prepago libre).
> > > Auth: `Authorization: Bearer <jwt>` (401 `NOT_AUTHENTICATED`
> > > sin token valido). Envoltorio `05#4` (`{"data", "meta"}`) en JSON.
> > > - `POST /credits/loans/{id}/pay` (cuerpo `{schedule_id?}` (UUID;
> > >   omitido = proxima cuota exigible `PENDING`/`PARTIAL`/`OVERDUE` por
> > >   `installment_no`); cabecera `Idempotency-Key` obligatoria: **mueve
> > >   dinero**) -> `200 {"data": {loan_id, schedule_id, transaction_id,
> > >   status ("SETTLED"), amount_minor, outstanding_principal_minor,
> > >   paid_at}, "meta": {"request_id", "replayed"}}` con la cuota cobrada
> > >   por el motor (`Transaction LOAN_INSTALLMENT`, un asiento
> > >   `Debe 2000-Cuenta / Haber 5000-Cartera + Haber 4000-Intereses +
> > >   Haber 4200-Seguro`, `docs/04#7.6`; las patas con monto 0 se
> > >   omiten), `loan_payments` con el desglose, cuota `PAID` y saldo de
> > >   capital reducido (ultima cuota -> `loans.status=PAID`). Sin fondos
> > >   -> `200` con `status=REJECTED` + `failure_code=INSUFFICIENT_FUNDS`
> > >   (sin tocar saldos ni la cuota; reintentable con la misma clave,
> > >   patron `E2-T07`). Replay (misma clave + mismo cuerpo) -> `200`
> > >   con `meta.replayed=true` sin segundo debito.
> > > - Codigos: `404 NOT_FOUND` (prestamo/cuenta ajeno o inexistente,
> > >   neutro), `409 CONFLICT` (cuota ya `PAID`, prestamo no `ACTIVE`,
> > >   sin cuotas pendientes) / `409 IDEMPOTENCY_CONFLICT` (misma clave
> > >   con cuerpo distinto), `422 VALIDATION_ERROR` (sin
> > >   `Idempotency-Key`, `schedule_id` malformado o de otro prestamo).
> > >   Evento por `outbox`: `loan.installment.paid` (`loan_id`,
> > >   `schedule_id`, `user_id`, `transaction_id`, `amount_minor`,
> > >   `currency`, sin PII).
> > >
> > > Detalle del debito automatico + recordatorios (E3-T19 `Hecho`,
> > > 2026-10-13; HU12 CA-03/CA-04; gaps G61/G64 aplicados).
> > > Auth: `Authorization: Bearer <jwt>` (401 `NOT_AUTHENTICATED`
> > > sin token valido). Envoltorio `05#4` (`{"data", "meta"}`) en JSON.
> > > - `PUT /credits/loans/{id}/auto-debit` (cuerpo `{enabled: bool}`
> > >   estricto —ni `1`/`0` ni texto—; **sin** `Idempotency-Key`: no
> > >   mueve dinero, solo fija el flag) -> `200 {"data": {loan_id,
> > >   auto_debit_enabled}, "meta": {"request_id"}}`.
> > > - Codigos: `404 NOT_FOUND` (prestamo ajeno o inexistente, neutro),
> > >   `422 VALIDATION_ERROR` (`enabled` ausente o no booleano, Pydantic
> > >   `StrictBool`).
> > > - Jobs (sin endpoints; registrables via
> > >   `app.composition.run_credit_loan_jobs_once`, env
> > >   `CREDIT_LOAN_JOBS_ENABLED` / `CREDIT_LOANS_AUTO_DEBIT_ENABLED` /
> > >   `CREDIT_LOANS_REMINDERS_ENABLED`, periodicidad
> > >   `CREDIT_LOANS_JOBS_INTERVAL_SECONDS` defecto 3600 s):
> > >   `run_auto_debit_once` cobra las cuotas `PENDING`/`PARTIAL`/
> > >   `OVERDUE` con `due_date <= hoy` de prestamos `ACTIVE` con
> > >   `auto_debit_enabled` reutilizando el caso de uso de `E3-T20`
> > >   (clave HTTP `auto-debit:<schedule>` + motor anclado a la cuota;
> > >   `commit` por item); sin fondos -> cuota y saldos intactos +
> > >   push `loan_auto_debit_failed` (una vez por cuota y dia).
> > >   `run_installment_reminders_once` avisa por push
> > >   (`loan_installment_reminder`) las cuotas `PENDING`/`PARTIAL`
> > >   con `due_date == hoy + loan.reminder_days_before` (parametro
> > >   `config.parameters`, default `3`, semilla `0036`; una vez por
> > >   cuota y dia via `processed_events`). Sin evento nuevo
> > >   (basta el push best-effort; decision 3 del brief).

### 6.5 Billetera, QR y servicios (`wallet`) - HU13-HU16

| Metodo | Ruta | Proposito |
|---|---|---|
| GET | `/wallet` | Billetera y vinculos. |
| POST | `/wallet/links` | Vincular cuenta/tarjeta. |
| DELETE | `/wallet/links/{id}` | Desvincular fuente (baja logica a `INACTIVE`, solo titular). |
| POST | `/merchants/qr` | Generar QR de cobro (HU14). |
| GET | `/merchants/qr/{id}` | Consultar estado del QR. |
| GET | `/services/catalog` | Catalogo de servicios y operadores. |
| POST | `/services/subscriptions` | Registrar un servicio a recordar. |
| POST | `/services/payments` | Pago de servicio. |
| POST | `/topups` | Recarga de celular. |

> Detalle de billetera y vinculos (E4-T01 `Hecho`, 2026-10-13; HU13 CA-01;
> cierra `docs/18` G66 en lo que toca a `/wallet*`, y G67 con el proveedor
> `mock` + interfaz desacoplada).
> Auth: `Authorization: Bearer <jwt>` en ambas rutas (401 `NOT_AUTHENTICATED`
> sin token valido). Envoltorio `05#4` (`{"data", "meta": {"request_id"}}`).
> El PAN, el numero completo y el `token_ref` jamas viajan en respuestas
> (solo `account_masked`/`last4`/`brand`).
> - `GET /wallet` (solo lectura, aislada por `sub` del JWT) ->
>   `200 {"data": {wallet_id, status, links[]}, "meta": {"request_id"}}`.
>   Sin billetera aun -> `200` con `wallet_id: null`, `status: "NONE"` y
>   `links: []` (la fila se crea on-demand en la primera vinculacion).
>   Vinculo = `{id, type (ACCOUNT|CARD), account_masked? (****1234, solo
>   cuentas), last4?/brand? (solo tarjetas), status, linked_at}`.
> - `POST /wallet/links` (cuerpo `{type: ACCOUNT|CARD, account_id?,
>   card?}`; exactamente uno de `account_id`/`card`; `type=ACCOUNT` exige
>   `account_id` UUID y prohibe `card`; `type=CARD` exige `card` y prohibe
>   `account_id`; `card` acepta el PAN crudo (`pan`, tokenizado server-side
>   con el proveedor vigente y descartado) o un `token_ref` pre-tokenizado
>   con `last4?`/`brand?` opcionales; cabecera `Idempotency-Key` opcional)
>   -> `201 {"data": {vinculo}, "meta": {"request_id", "replayed"}}`.
>   La cuenta se valida por la fachada de `accounts` (titular + `ACTIVE`).
>   Re-vincular una fuente `INACTIVE` la reactiva (sin borrar filas);
>   un `token_ref` de otro titular nunca se reactiva (se responde
>   `409 DUPLICATE_LINK` generico sin tocar la fila ajena).
>   Emite `wallet.link.created` por `outbox` (payload sin PII:
>   `wallet_id`, `link_id`, `type`).
> - `DELETE /wallet/links/{id}` (solo titular, sin `Idempotency-Key`:
>   no mueve dinero, solo cambia estado) ->
>   `200 {"data": {vinculo con `status: "INACTIVE"`}, "meta":
>   {"request_id"}}` (baja logica; sin borrar filas). Vinculo
>   ajeno/inexistente/id malformado o ya `INACTIVE` ->
>   `404 NOT_FOUND` neutro (mismo cuerpo, sin oraculo).
> - Codigos: `404 NOT_FOUND` neutro (cuenta ajena/inexistente/inactiva, sin
>   oraculo), `409 DUPLICATE_LINK` (fuente ya vinculada `ACTIVE`),
>   `409 IDEMPOTENCY_CONFLICT` (misma clave + cuerpo distinto),
>   `422 VALIDATION_ERROR` (`type` invalido, falta dato, `card` con
>   ambas/ninguna fuente, PAN malformado) + `422` de esquema Pydantic.
>
> Detalle del QR de comercio (E4-T08 `Hecho`, 2026-10-13; HU14
> CA-01/CA-02 parcial/CA-04 parcial; cierra `docs/18` G66 en lo que
> toca a `/merchants/qr*`).
> Auth: `Authorization: Bearer <jwt>` en ambas rutas (401
> `NOT_AUTHENTICATED` sin token valido). El rol de comercio es la
> fila propia `ACTIVE` en `wallet.merchant_accounts` (sin ella ->
> `403 NOT_AUTHORIZED`; decision 3 del brief). Envoltorio `05#4`
> (`{"data", "meta": {"request_id"}}`). El secreto HMAC y el RUC
> jamas viajan en respuestas (solo la firma verificable, el
> `payload` canonico y el sobre `BO-QR1`).
> - `POST /merchants/qr` (cuerpo `{amount_minor (entero > 0, sin
>   `float`), currency (ISO-4217, default `PEN`), concept? (<= 120),
>   mode? (`DYNAMIC` default | `STATIC`)}`; cabecera `Idempotency-Key`
>   opcional —decision 1 del brief, patron `E2-T13`: no mueve dinero—)
>   -> `201 {"data": {charge_id, qr_content, payload, payload_hash,
>   signature, amount_minor, currency, concept, mode, status
>   ("ACTIVE"), expires_at, created_at}, "meta": {"request_id",
>   "replayed"}}`.
>   `qr_content` (`String`, decision del dueno sprint-3) es el
>   contenido escaneable e interoperable: el token/sobre canonico
>   `BO-QR1` que produce el propio backend
>   (`wallet.domain.qr.to_interop_token`, adaptador `E4-T03`:
>   `issuer`/`peer` = `CUYCASH`, `ref` = `charge_id`,
>   `amount_minor`/`currency`/`exp` del cargo, `sig` = `signature`);
>   es lo que se renderiza como QR y lo que un lector conforme
>   (HU15) decodifica (`decode_payload`) y verifica contra
>   `payload`/`signature`. `payload` sigue siendo objeto (dict) y no
>   sustituye al string.
>   Misma clave + mismo cuerpo = replay (un solo cargo,
>   `replayed: true`, `payload` y `qr_content` reconstruidos
>   deterministicamente); cuerpo distinto = `409
>   IDEMPOTENCY_CONFLICT`. Emite `qr.charge.created` por `outbox`
>   (payload sin PII).
> - `GET /merchants/qr/{id}` (solo el comercio dueno; id malformado,
>   ajeno o inexistente -> `404 NOT_FOUND` neutro, sin montos,
>   anti-enumeracion) -> `200 {"data": {charge_id, qr_content,
>   status, amount_minor, currency, expires_at, paid_at?}, "meta":
>   {"request_id"}}` (`qr_content`: mismo sobre `BO-QR1` del `POST`,
>   reconstruido deterministicamente; sin `payload`/`signature` en
>   esta ruta). El `status` es efectivo on-read (`E4-T07`): un
>   `ACTIVE` con `expires_at <= now` se reporta `EXPIRED` aunque el
>   job aun no lo haya marcado (solo lectura, sin mutar la fila).
> - Aviso de abono (HU14 CA-04 parcial, `docs/18` G72): plantilla
>   `qr_charge_paid` (push, sin PII ni cifras: solo referencia del
>   cargo e instante) invocada por la fachada
>   `notifications.send(...)` via el helper
>   `wallet.service.qr.notify_merchant_charge_paid` cuando el pago de
>   `HU15` (`qr.payment.confirmed`) se confirma (< 5 s); `E4-T10` la
>   valida con pago simulado y el E2E queda para HU15.
> - Codigos: `403 NOT_AUTHORIZED` (sin cuenta de comercio `ACTIVE`),
>   `404 NOT_FOUND` neutro (cargo ajeno/inexistente),
>   `409 IDEMPOTENCY_CONFLICT`, `422 VALIDATION_ERROR`
>   (`amount_minor <= 0`, moneda invalida, `mode` fuera de
>   `DYNAMIC`/`STATIC`, `concept` > 120) + `422` de esquema Pydantic.
>
> Detalle del catalogo de servicios (E4-T16 `Hecho`, 2026-10-13; HU16
> CA-01/CA-03 base; cierra `docs/18` G77 en lo que toca a
> `/services/catalog` y `/services/subscriptions`).
> Auth: `Authorization: Bearer <jwt>` en ambas rutas (401
> `NOT_AUTHENTICATED` sin token valido). Envoltorio `05#4`
> (`{"data", "meta": {"request_id"}}`). **No mueve dinero** (el pago
> y la recarga son `E4-T17`).
> - `GET /services/catalog` (filtros opcionales `category?`/`q?`,
>   solo lectura, < 2 s) -> `200 {"data": [{biller_id, category,
>   name, required_fields}], "meta": {"request_id"}}` con los
>   `billers` habilitados (`docs/03b#95`, migracion
>   `0042_wallet_services` con 6 demos: `ELECTRICIDAD`/`AGUA`/`GAS`/
>   `INTERNET` con `required_fields: ["supply_ref"]` y `TELEFONIA`
>   con `["phone"]`, decision 3 del brief). El catalogo es
>   **extensible sin cambios estructurales** (columnas fijas +
>   `required_fields` JSONB; agregar empresas es insertar filas).
>   `category` malformada o `q` > 80 -> `422 VALIDATION_ERROR`;
>   categoria bien formada pero desconocida -> `200` vacio.
> - `POST /services/subscriptions` (cuerpo `{biller_id (UUID),
>   supply_ref (1..80), notify_days_before? (1..30, default `3`,
>   coherente con HU16)}`, sin `Idempotency-Key`: no mueve dinero)
>   -> `201 {"data": {subscription_id, biller_id, supply_ref,
>   notify_days_before, status}, "meta": {"request_id", "replayed"}}`
>   (registro para los recordatorios que consume el job de `E4-T18`;
>   decision 1 del brief, `docs/18` G77). Valida el suministro contra
>   el proveedor vigente (`mock` determinista por defecto) antes de
>   persistir. Idempotente por naturaleza: re-registrar el mismo
>   `biller`+suministro devuelve la fila `ACTIVE` existente
>   (`replayed: true`, sin duplicar).
> - Adaptador `ServiceProviderGateway`
>   (`backend/app/adapters/service_provider.py`, decision 2 del
>   brief, `docs/18` G77): interfaz (`lookup_biller` por
>   nombre/suministro + `validate_supply`) + `mock` determinista sin
>   red (modos `success`/`down`/`timeout`; deuda simulada derivada
>   del hash) + `http` (`POST
>   {base_url}/services/supply/validate`, timeout/reintentos con
>   backoff/circuit breaker) + fabrica `create_service_provider`
>   (`SERVICE_PROVIDER=mock|http`, default `mock`, valor no
>   reconocido -> `mock`). Sin PII/secretos en logs (solo hash del
>   suministro).
> - Codigos: `404 NOT_FOUND` neutro (`biller`
>   inexistente/deshabilitado), `422 VALIDATION_ERROR`
>   (filtro/`supply_ref`/`notify_days_before`/suministro invalido) +
>   `422` de esquema Pydantic, `503
>   SERVICE_PROVIDER_UNAVAILABLE` (proveedor caido), `502
>   SERVICE_PROVIDER_TIMEOUT` (timeout del proveedor).
> - Pruebas: `backend/tests/test_e4_t16_services_catalog.py`
>   (catalogo/busqueda, suscripcion + replay, adaptador
>   determinista/seleccion/`http` simulado, errores, sin PII,
>   cadena `0042`).
> - Job de recordatorios de servicios (`E4-T18` `Hecho`, 2026-10-13;
>   HU16 CA-03; cierra `docs/18` G77 en lo que toca al aviso): sin
>   endpoint (job best-effort, **no mueve dinero**). Cada pasada
>   (`wallet/jobs/service_reminders.py::run_service_reminders_once`,
>   `commit` por item, patron `E3-T19`) recorre las
>   `service_subscriptions` `ACTIVE` y avisa por push (plantilla
>   `service_reminder`: solo referencia del servicio y fecha, sin PII
>   ni cifras) las que vencen en la ventana exacta. Vencimiento
>   derivado determinista (decision 1 del brief: `service_subscriptions`
>   no tiene columna de fecha y el proveedor no expone vencimiento;
>   dia del mes `1..28` del `subscription_id` + proxima ocurrencia;
>   el proveedor real lo reemplazara sin tocar el job). Ventana
>   efectiva = minimo entre el parametro global
>   `wallet.service_reminder_days_before` (default `3`,
>   `shared/config`, semilla `0043`, `PUT /admin/parameters` sin
>   redespliegue) y el `notify_days_before` de la fila (1..30,
>   default `3`). Idempotencia por `(subscription_id, dia)` en
>   `processed_events` (consumidor `wallet-service-reminders`; la marca
>   se confirma al reclamarla, como en `E3-T19`: el fallo posterior
>   queda `failed` sin duplicar). Wiring en ambos runtimes (patron
>   M-2): runner `app.composition.run_wallet_service_jobs_once` /
>   `start_wallet_service_jobs_worker` (flags
>   `WALLET_SERVICE_JOBS_ENABLED` /
>   `WALLET_SERVICE_REMINDERS_ENABLED`, intervalo
>   `WALLET_SERVICE_JOBS_INTERVAL_SECONDS` defecto 3600 s), en el
>   `lifespan` y en `worker_entrypoint.py`; `/health` intacto.
> - Pruebas: `backend/tests/test_e4_t18_service_reminders.py`
>   (ventana exacta, idempotencia, parametro, `notify` por fila, sin
>   PII, wiring, cadena `0043`).
>
> - Pago de servicio y recarga (`E4-T17` `Hecho`, 2026-10-13; HU16
>   CA-02/CA-04, cierra `docs/18` G73): el dinero se mueve **solo** por
>   el motor (`pay_express`, asiento `docs/04#7.10` sin comision).
>   Auth: `Authorization: Bearer <jwt>` en ambas rutas (401
>   `NOT_AUTHENTICATED` sin token valido); **solo el titular** (el
>   vinculo debe ser suyo: ajeno/inexistente/inactivo -> `404
>   NOT_FOUND` neutro; `CARD` -> `422`, no debitable).
>   - `POST /services/payments` (cuerpo `{biller_id (UUID),
>     supply_ref (1..80), amount_minor (entero > 0, sin `float`),
>     currency? (ISO-4217, default `PEN`), link_id (UUID del vinculo
>     `ACCOUNT` a debitar)}` con cabecera `Idempotency-Key`
>     **obligatoria** (mueve dinero; sin ella -> `422
>     VALIDATION_ERROR`)) -> `201 {"data": {payment_id,
>     transaction_id, status
>     (SETTLED/PENDING_AUTHORIZATION/REJECTED), amount_minor,
>     currency, receipt?, failure_code?, authorization?}, "meta":
>     {"request_id", "replayed"}}`. Valida el suministro contra el
>     proveedor vigente **antes de cobrar** (invalido -> `422`;
>     caido -> `503 SERVICE_PROVIDER_UNAVAILABLE`; timeout -> `502
>     SERVICE_PROVIDER_TIMEOUT`). `<=` base -> `SETTLED` con
>     comprobante `receipt {receipt_no (`OP-` + 12 hex, convencion
>     `E2-T10`), transaction_id, concept (nombre del `biller`),
>     supply_masked, amount_minor, currency, paid_at, status}`;
>     `>` base -> `PENDING_AUTHORIZATION` + `authorization {method:
>     "BIOMETRIC", nonce, expires_at}` (se confirma con
>     `POST /transactions/{id}/authorize`, firma o PIN; el reintento
>     con la misma clave finaliza el pago a `PAID`); `REJECTED`
>     (diario/fondos) con `failure_code`
>     (`DAILY_LIMIT_EXCEEDED`/`INSUFFICIENT_FUNDS`, patron `E2-T07`)
>     sin tocar saldos. Emite `service.paid` por `outbox` (payload
>     sin PII). Misma clave + mismo cuerpo = replay (un solo pago);
>     cuerpo distinto = `409 CONFLICT`.
>   - `POST /topups` (cuerpo `{operator (nombre del operador
>     `TELEFONIA` o su UUID), phone (digitos 6..15), amount_minor,
>     currency? (default `PEN`), link_id}`, `Idempotency-Key`
>     obligatoria) -> `201 {"data": {topup_id, transaction_id,
>     status, amount_minor, currency, receipt?, failure_code?,
>     authorization?}, "meta": {"request_id", "replayed"}}` (misma
>     semantica: valida el telefono con el adaptador `E4-T16` antes
>     de cobrar; operador inexistente/no-`TELEFONIA` -> `404`
>     neutro; emite `topup.completed` por `outbox`, sin telefono en
>     el payload; el comprobante lleva `phone_masked`).
>   - Codigos: `404 NOT_FOUND` neutro (`biller`/operador/vinculo
>     ajeno o inexistente), `409 CONFLICT` (clave con cuerpo
>     distinto), `422 VALIDATION_ERROR` (sin clave, suministro/
>     telefono/monto/moneda invalidos, liquidacion sin configurar)
>     + `422` de esquema Pydantic, `502`/`503` del proveedor.
>   - Pruebas: `backend/tests/test_e4_t17_service_payments.py`
>   (feliz con asiento cuadrado + evento + aviso de saldos,
>   pendiente + autorizacion/finalizacion, diario/fondos sin tocar
>   saldos, idempotencia/`CONFLICT`, comprobante `OP-…`, errores,
>   sin PII, frontera, OpenAPI, cadena `0042`).
>
> Otros servicios internos (sin endpoint publico): el **servicio de generacion** del QR (`E4-T06`
> `Hecho`, 2026-10-13) ya existe como servicio interno sin endpoint
> publico: `wallet.service.qr.create_qr_charge(session, *,
> merchant_id, amount_minor, currency, concept?, mode?,
> ...)` (firma HMAC-SHA256 + `expires_at = now +
> wallet.qr_ttl_seconds` en `DYNAMIC`, `NULL` en `STATIC`; persiste
> `ACTIVE` y emite `qr.charge.created` por `outbox`; **no mueve
> dinero**). El pago express de billetera (`E4-T02` `Hecho`,
> 2026-10-13) quedo como **servicio interno sin endpoint publico**
> (decision G66, opcion recomendada del brief): `wallet.service.express.
> pay_express(session, *, initiator_user_id, link_id, target_account_id,
> amount_minor, currency, idempotency_key, ...)` (reutilizable por HU15
> `POST /payments/qr` y HU16; `Idempotency-Key` obligatoria; limites
> `wallet.express_limit_minor`/`wallet.daily_limit_minor` via
> `shared/config`, semillas `0038`; `CARD` no debitable; errores
> `NOT_FOUND` neutro/`VALIDATION_ERROR`/`CONFLICT`; `REJECTED` con
> `DAILY_LIMIT_EXCEEDED` o fondos insuficientes sin tocar saldos).
> - Servicio interno de cobro QR (`E4-T11` `Hecho`, 2026-10-13; HU15
>   CA-01/CA-04, cierra `docs/18` G72 lado backend): `wallet.service.
>   qr_payment.pay_qr_charge(session, *, payer_user_id, qr_content,
>   link_id, idempotency_key, ...)` (sin endpoint publico: la ruta
>   `POST /payments/qr` la fija `E4-T12`, `docs/18` G74). Decodifica el
>   `qr_content` (`BO-QR1`, adaptador `E4-T03`), verifica firma/hash
>   + estado `ACTIVE`/no vencido **antes de operar** (vencido/
>   corrupto/manipulado -> `VALIDATION_ERROR` sin retencion ni asiento;
>   ya cobrado -> `CONFLICT`) y mueve el dinero **solo** por
>   `pay_express` (`docs/04#7.4` sin comision; `Idempotency-Key`
>   obligatoria). La cuenta de abono es la primera cuenta `ACTIVE`
>   del titular comercio con la moneda del cargo (fachada `accounts`,
>   sin columna nueva). `SETTLED` -> `qr_payments PAID` + cargo `PAID`/
>   `paid_at` + `qr.payment.confirmed` por `outbox` (payload sin PII:
>   `qr_payment_id`, `charge_id`, `merchant_id`, `amount_minor`,
>   `currency`, `status`) + aviso `qr_charge_paid` (< 5 s);
>   `PENDING_AUTHORIZATION` (sobre el limite base) -> pago pendiente
>   (sin marcar `PAID`; `E4-T12` autoriza con
>   `daily_limit_reader=wallet_daily_limit_reader`, G75, y finaliza
>   con `finalize_qr_payment`); `REJECTED` (diario/fondos) sin tocar
>   el cargo ni notificar.
> - `POST /payments/qr` (`E4-T12` `Hecho`, 2026-10-13; HU15 CA-01..CA-04,
>   cierra `docs/18` G74): el endpoint del pagador sobre el servicio
>   de `E4-T11` (sin duplicar logica). Auth: `Authorization: Bearer
>   <jwt>` (401 `NOT_AUTHENTICATED` sin token valido); **solo el
>   titular pagador** (el vinculo debe ser suyo: ajeno/inexistente/
>   inactivo -> `404 NOT_FOUND` neutro). Request `{qr_content
>   (`String` `BO-QR1` escaneado), link_id (UUID del vinculo `ACCOUNT`
>   a debitar)}` con cabecera `Idempotency-Key` **obligatoria** (mueve
>   dinero; sin ella -> `422 VALIDATION_ERROR`) -> `201 {"data":
>   {transaction_id, qr_payment_id, charge_id, merchant {merchant_id,
>   business_name}, amount_minor (entero), currency, status
>   (SETTLED/PENDING_AUTHORIZATION/REJECTED), failure_code?,
>   authorization?}, "meta": {"request_id", "replayed"}}`.
>   `<=` limite base -> `SETTLED` en un toque (comercio y monto en la
>   respuesta, < 2 s; cargo `PAID` + `qr.payment.confirmed` + aviso
>   `qr_charge_paid` unico, < 5 s); `>` base ->
>   `PENDING_AUTHORIZATION` + `authorization {method: "BIOMETRIC",
>   nonce, expires_at}` (patron `_challenge_fields`, `E2-T19`; el
>   cliente confirma con `POST /transactions/{id}/authorize`, firma o
>   PIN, `E2-T31`) sin marcar `PAID` aun; `REJECTED` (diario/fondos)
>   con `failure_code` (`DAILY_LIMIT_EXCEEDED`/`INSUFFICIENT_FUNDS`,
>   patron `E2-T07`) sin tocar saldos ni el cargo. Tras autorizar, el
>   reintento con la misma clave finaliza el diferido
>   (`finalize_qr_payment`: cargo `PAID` + aviso unico; si la
>   operacion quedo `REJECTED`, el pago se espeja a `REJECTED`;
>   cierra `docs/18` G72 lado endpoint). Misma clave + mismo cuerpo =
>   replay (un solo pago, `replayed: true`); cuerpo distinto = `409
>   CONFLICT`. Codigos: `422 VALIDATION_ERROR` (QR corrupto/
>   manipulado/vencido/ajeno, monto/moneda, esquema), `404 NOT_FOUND`
>   neutro (vinculo ajeno/inexistente), `409 CONFLICT` (QR ya pagado /
>   `IDEMPOTENCY_CONFLICT`), `403 NOT_AUTHORIZED` ajeno en el
>   `authorize`. Sin PII/RUC/secretos en respuestas ni logs; montos
>   enteros. **Ownership** (`docs/18` G74, decision 1 del brief
>   `E4-T12`): la ruta vive en `wallet` (`api/payments.py`,
>   `schemas/payment.py`) aunque `docs/05#6.3` la liste junto a
>   `transactions` por descubrimiento; el detalle vive aqui (`#6.5`).
> - `POST /payments/qr/preview` (fix MEDIO-3, 2026-10-13; HU15 CA-01,
>   consentimiento informado): previsualiza **comercio y monto antes de
>   confirmar**. Auth: `Authorization: Bearer <jwt>` (401
>   `NOT_AUTHENTICATED` sin token valido). Request `{qr_content
>   (`String` `BO-QR1` escaneado)}`, **sin** cabecera `Idempotency-Key`
>   (no mueve dinero) -> `200 {"data": {charge_id, merchant
>   {merchant_id, business_name}, amount_minor (entero), currency,
>   expires_at (`None` en `STATIC`), status (`ACTIVE`)}, "meta":
>   {"request_id"}}`. Decodifica y verifica el `qr_content` (adaptador
>   `E4-T03` + `verify_charge_signature` + caducidad/estado) en **solo
>   lectura**: sin retencion, sin asiento, sin pagos ni eventos.
>   Vencido/corrupto/manipulado/ajeno o comercio sin cuenta de abono ->
>   `422 VALIDATION_ERROR` sin efectos; ya cobrado -> `409 CONFLICT`.
>   La respuesta nunca incluye el `qr_content` crudo, la firma, el
>   secreto ni el RUC (solo el nombre publico del comercio, como `POST
>   /payments/qr`). Se elige `POST` sobre `GET`: el `qr_content` (hasta
>   8192 caracteres) no cabe bien en una URL y no debe quedar en access
>   logs. Servicio `wallet.service.qr_payment.preview_qr_charge`;
>   regresion en `backend/tests/test_e4_t11b_qr_payment_guard.py`.

### 6.6 Divisas y ahorro (`fx`) - HU24, HU25

| Metodo | Ruta | Proposito |
|---|---|---|
| GET | `/fx/rates` | Cotizacion vigente. |
| POST | `/fx/quotes` | Congela cotizacion 30 s. |
| POST | `/fx/operations` | Ejecuta compra/venta. |
| GET/POST | `/pockets` | Bolsillos y metas de ahorro. |
| POST | `/pockets/{id}/transfer` | Traspaso entre bolsillos/cuentas. |

### 6.7 Riesgo, cumplimiento y auditoria (panel interno)

| Metodo | Ruta | Rol |
|---|---|---|
| GET | `/risk/alerts` | Analista de fraude. |
| POST | `/risk/alerts/{id}/resolve` | Analista de fraude. |
| GET | `/compliance/screenings` | Oficial de cumplimiento. |
| POST | `/compliance/ros` | Oficial de cumplimiento. |
| GET | `/audit/logs` | Auditor (solo lectura). |
| GET | `/reconciliation/exceptions` | Analista de operaciones. |
| POST | `/reconciliation/runs` | Analista de operaciones. |

### 6.8 Administracion

| Metodo | Ruta | Proposito |
|---|---|---|
| GET/PUT | `/admin/parameters` | Parametros configurables. |
| GET/POST | `/admin/users/{id}/roles` | Roles y permisos (Admin de seguridad). |

> Contrato E2-T26 `GET/PUT /api/v1/admin/parameters` (Bearer, **rol admin**,
> `05#6.8`/G18): **fachada** auditada sobre `shared/config` (unico dueno del
> esquema de `config.parameters`; `admin` nunca escribe directo).
> - `GET /admin/parameters` con filtros opcionales `module`/`prefix` -> `200
>   {"data": [ {"key", "value_json", "module", "description", "updated_by",
>   "updated_at"} ], "meta": {"page", "page_size", "total", "request_id"}}`
>   (lista vacia -> `200`, no `404`).
> - `PUT /admin/parameters/{key}` body `{value_json}` -> `200 {"data":
>   {"key", "value_json", "updated_at"}, "meta": {...}}`; valida el tipo segun
>   la clave (enteros `>= 0` para `*_minor`/`auth.*`; las decimales
>   `loan.max_dti_ratio`/`loan.review_margin` (G26) aceptan numero o texto
>   numerico y se responden como texto exacto, sin `float`), es idempotente
>   por naturaleza (misma clave + mismo valor = mismo estado) y audita cada
>   cambio (`updated_by` del JWT + `updated_at`, sin PII). No mueve dinero
>   (sin `Idempotency-Key`) y **no** emite `parameter.updated` (decision
>   E2-T18; la trazabilidad la dan las columnas).
> - Errores: `401 NOT_AUTHENTICATED`; `403 NOT_AUTHORIZED` (sin rol admin);
>   `404 NOT_FOUND` (clave inexistente al escribir); `422 VALIDATION_ERROR`
>   (valor de tipo/formato invalido). Cambiar el umbral/topes/limites se
>   refleja en el motor sin despliegue (fachada `shared/config`, regla de oro
>   6).

### 6.9 Centro de notificaciones (`notifications`) - HU22

| Metodo | Ruta | Proposito |
|---|---|---|
| GET | `/notifications` | Centro del titular: lista paginada de SUS notificaciones (metadatos). |
| GET | `/notifications/{id}` | Detalle read-only de UNA notificacion propia (metadatos acotados). |
| POST | `/notifications/{id}/read` | Marca una notificacion propia como leida (idempotente). |
| POST | `/notifications/{id}/report` | Reporta "no reconozco esta operacion" (idempotente; bloquea + escala). |
| GET | `/notifications/preferences` | Preferencias de canal del titular. |
| PUT | `/notifications/preferences` | Actualiza las preferencias de canal (idempotente). |

> Contrato E6-T12 `GET /api/v1/notifications` (Bearer, solo el titular) ->
> `200 {"data": [ {"id", "channel", "template_code", "status", "created_at",
> "read_at", "device", "location", "reference"} ], "meta": {"page",
> "page_size", "total", "request_id"}}`
> (sin `payload_json` crudo ni PII; mas recientes primero; `device`/
> `location`/`reference` son los campos de render acotados de la alerta —
> etiquetas saneadas sin PII, `null` en filas que no los traen, fix A2).
> Filtros opcionales `channel` (`push`/`email`/`sms`) y `unread=true`
> (solo no leidas); `page >= 1`, `1 <= page_size <= 100`.
> - `GET /api/v1/notifications/{id}` (Bearer, solo el titular; fix A2) ->
>   `200 {"data": {...misma vista acotada...}, "meta": {"request_id"}}`
>   (detalle read-only de UNA notificacion propia; ajena/inexistente ->
>   `404 NOT_FOUND` neutro; id malformado -> `422 VALIDATION_ERROR`).
> - `POST /api/v1/notifications/{id}/read` (Bearer, solo el titular) ->
>   `200 {"data": {...}, "meta": {"request_id"}}` (fija `read_at`;
>   idempotente: repetir devuelve el mismo `read_at`).
> - `POST /api/v1/notifications/{id}/report` (E6-T14, HU22; Bearer, solo el
>   titular; body opcional `{reason?}`, max 280) -> `200 {"data":
>   {"reported": true, "replayed": bool, "notification_id",
>   "confirmation_id", "reference"}, "meta": {"request_id"}}` (registra el
>   reporte "no reconozco": fija `read_at` de la alerta, crea el acuse push
>   `security_report_received` y emite `security.operation_reported` por
>   `outbox` para que `risk` escale y `identity` bloquee; idempotente por
>   estado: repetir devuelve `replayed: true` sin nuevos efectos).
> - `GET /api/v1/notifications/preferences` (Bearer) -> `200 {"data":
>   {"push", "email", "sms"}, "meta": {"request_id"}}` (defaults
>   `true`/`true`/`false` si el titular aun no tiene fila).
> - `PUT /api/v1/notifications/preferences` (Bearer) body `{push, email,
>   sms}` (los tres booleanos obligatorios) -> `200` con las preferencias
>   guardadas (idempotente por `user_id` PK: repetir no duplica).
> - Nota de seguridad (decision del dueno, E6-T12): desactivar `email` NO
>   desactiva el respaldo de alertas de seguridad (el correo de respaldo se
>   garantiza; las preferencias rigen el resto de notificaciones).
> - Fachada interna (sin ruta publica): `notify_security_event(...)` que los
>   emisores llaman en el momento sensible (push inmediato + correo de
>   respaldo en paralelo, ventana `< 5 s`; plantillas
>   `security_operation_alert`/`security_operation_email` con dispositivo,
>   hora y ubicacion aproximada, sin PII).
> - Errores: `401 NOT_AUTHENTICATED` (sin JWT valido); `404 NOT_FOUND`
>   neutro (ajeno/inexistente); `422 VALIDATION_ERROR` (filtros/paginado/
>   cuerpo invalidos).

## 7. API de integracion interbancaria (a definir con el equipo par)

Contrato propuesto (servira de simulador hasta que exista el real):

| Metodo | Ruta | Proposito |
|---|---|---|
| POST | `/interbank/transfers` | Enviar transferencia a la contraparte. |
| GET | `/interbank/transfers/{reference}` | Consultar estado. |
| POST | `/interbank/transfers/{reference}/confirm` | Confirmacion/reverso (callback). |

Reglas: `Idempotency-Key` obligatoria, `reference` unica, timeouts cortos, estado final
`SETTLED`/`REVERSED`, y toda operacion pendiente entra a `2200-Transito` hasta conciliar.

## 8. Integracion con el microservicio KYC (consumo)

El modulo `identity` consume (no reimplementa):

| Metodo | Ruta | Uso |
|---|---|---|
| POST | `/api/v1/liveness/challenge` | Obtiene token y secuencia de pasos. |
| POST | `/api/v1/liveness/evaluate` | Valida una tarea por vez. |
| POST | `/api/v1/identity/verify-full` | Resultado integral del KYC. |
| POST | `/api/v1/document/validate` | Valida la legibilidad del documento (`file` multipart). |

Reglas de integracion:
- **El microservicio KYC se consume solo durante la creacion de cuenta (HU01).** No se usa para
  login, recuperacion, pagos ni firma; esas operaciones usan biometria del dispositivo.
- Enviar `X-API-Key`; nunca exponer la key al cliente movil (el backend hace de proxy).
- Guardar solo el resultado (`overall_result`, distancias), nunca los frames.
- Mapear `overall_result = true` a `kyc_verifications.overall_result` y crear el usuario/cuenta.

## 9. Rate limiting y abuso

- Limites por usuario y por IP en login y OTP (anti fuerza bruta).
- Limites diarios de operaciones express/QR (HU13, HU14).
- Bloqueo temporal tras 5 intentos fallidos (HU03).

## 10. Versionado y compatibilidad

- Cambios incompatibles -> nueva version (`/api/v2`).
- Campos nuevos -> opcionales y documentados.
- Deprecaciones -> cabecera `Deprecation` y aviso en OpenAPI.
