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
`DOC_LOOKUP_UNAVAILABLE`, `DOCUMENT_NOT_FOUND`, `DUPLICATE_BENEFICIARY` (409),
`DUPLICATE_DOCUMENT`, `DUPLICATE_EMAIL`, `EXPIRED_NONCE`, `EXPIRED_OTP`,
`EXPORT_FORMAT_NOT_SUPPORTED` (501), `IDEMPOTENCY_CONFLICT` (409),
`INSUFFICIENT_FUNDS` (solo como `data.failure_code` con `data.status=REJECTED` en
`POST /transfers/*`, E2-T07; el HTTP sigue `201` para no romper la idempotencia),
`INVALID_CREDENTIALS`, `INVALID_LOGIN`, `INVALID_OTP`, `INVALID_PIN_FORMAT`,
`INVALID_PIN_RESET`, `INVALID_REFRESH`, `INVALID_SETUP_CODE`, `KYC_UNAVAILABLE`,
`NOT_AUTHENTICATED`, `NOT_AUTHORIZED`, `NOT_FOUND`, `PIN_ALREADY_SET`, `PIN_REQUIRED`,
`RATE_LIMITED`, `RECEIPT_FORMAT_NOT_SUPPORTED` (501), `REFRESH_EXPIRED`, `REFRESH_REUSED`,
`RESEND_LIMIT`, `SESSION_INACTIVE`, `VALIDATION_ERROR`.
Fuentes: `backend/app/modules/identity/api/*` (+ `service/`), `backend/app/modules/accounts/api/__init__.py`
(`EXPORT_FORMAT_NOT_SUPPORTED`, `NOT_AUTHENTICATED`, `NOT_AUTHORIZED`, `NOT_FOUND`,
`VALIDATION_ERROR`), `backend/app/modules/accounts/api/beneficiaries.py`
(`DUPLICATE_BENEFICIARY`, `IDEMPOTENCY_CONFLICT`), `backend/app/modules/transactions/api/__init__.py`
(`RECEIPT_FORMAT_NOT_SUPPORTED`). Sin `INVALID_RECOVERY_CODE` (solo lo emitia el retirado `verify`, E1-T41).

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
| GET | `/accounts/{id}/movements` | Movimientos paginados de `movements_view` (filtros: `date_from`/`date_to` fecha valor ISO y `direction` `DEBIT`/`CREDIT`, opcionales). |
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
> [holder_name], [holder_doc], [preapproved_limit_minor], [currency]}` con `Idempotency-Key`
> opcional (recomendada; misma clave + mismo cuerpo = replay sin duplicar) -> `201 {"data":
> Beneficiary, "meta": {"replayed"}}`. `PUT /beneficiaries/{id}` (edicion parcial, incl.
> `preapproved_limit_minor`) y `DELETE /beneficiaries/{id}?confirm=true` (soft-delete, exige
> `confirm=true`) -> `200 {"data": Beneficiary, "meta": {}}`. `Beneficiary` expone `id`, `alias`,
> `photo_url`, `bank_code`, `account_masked` (`****` + ultimos 4), `holder_name`,
> `preapproved_limit_minor`, `currency`, `status` (`ACTIVE`/`DELETED`), `created_at`/`updated_at`;
> **nunca** devuelve la cuenta/CCI en claro ni el documento (solo hash en BD). El
> `owner_user_id` sale del JWT, nunca del cuerpo. Errores: `401 NOT_AUTHENTICATED`; `404
> NOT_FOUND` (inexistente/baja, id malformado o beneficiario ajeno no listado); `403
> NOT_AUTHORIZED` (beneficiario de otro titular en `PUT`/`DELETE`); `409 DUPLICATE_BENEFICIARY`
> (mismo alias o misma cuenta/CCI `ACTIVE` del titular); `409 IDEMPOTENCY_CONFLICT` (misma
> `Idempotency-Key` con cuerpo distinto); `400 VALIDATION_ERROR` (alias vacio, CCI con formato
> distinto de 8-20 digitos, moneda desconocida, `preapproved_limit_minor < 0`, o falta
> `confirm=true`). Dinero entero en centimos; datos sensibles cifrados en reposo (AES-256-GCM)
> e indexados para busqueda sin descifrar.

### 6.3 Transacciones (`transactions`) - HU06, HU08, HU15, HU17

| Metodo | Ruta | Proposito |
|---|---|---|
| POST | `/transfers/own` | Transferencia entre cuentas propias. |
| POST | `/transfers/third-party` | Transferencia a tercero (mismo banco). |
| POST | `/transfers/interbank` | Transferencia interbancaria (202 + estado). |
| POST | `/payments/qr` | Pago de QR (HU15). |
| POST | `/transactions/{id}/authorize` | Autorizacion biometrica de pago sensible (HU08). |
| POST | `/transactions/{id}/authorize/challenge` | Rotacion del reto tras `EXPIRED_NONCE` (HU08). |
| GET | `/transactions/{id}` | Estado y detalle. |
| GET | `/transactions` | Historial filtrable. |
| GET | `/transactions/{id}/receipt` | Comprobante. |
| POST | `/transactions/{id}/reverse` | Reverso autorizado (operaciones). |

> Contrato E2-T07 `POST /transfers/own` y `POST /transfers/third-party` (Bearer, HU06):
> request `{source_account_id, target_account_id, amount_minor (> 0), currency (ISO-4217),
> [fee_minor (>= 0, solo tercero; `None` = `transfer.third_party_fee_minor` de
> `config.parameters`, default `0`)], [external_ref], [metadata]}` con cabecera
> `Idempotency-Key` obligatoria -> `201 {"data": {"transaction_id", "type"
> (`OWN_TRANSFER`/`THIRD_PARTY_TRANSFER`), "status" (`SETTLED`/`REJECTED`),
> "source_account_id", "target_account_id", "amount_minor", "currency", "fee_minor",
> "failure_code" (`INSUFFICIENT_FUNDS` solo si `REJECTED`)}, "meta": {"request_id",
> "replayed"}}`. La propia es atomica sin hold (un asiento `2000-S -> 2000-D`, sin
> comision); la de tercero es hold+settle con la comision a `4100`. Errores:
> `401 NOT_AUTHENTICATED`, `422` (falta `Idempotency-Key`, esquema invalido o
> `amount_minor <= 0` / `fee_minor < 0`, que el esquema rechaza con `ge=1`/`ge=0`),
> `400 VALIDATION_ERROR` (misma cuenta, moneda incoherente),
> `404 NOT_FOUND` (cuenta inexistente), `403 NOT_AUTHORIZED` (origen ajeno o
> cuenta no `ACTIVE`), `409 CONFLICT` (misma clave + cuerpo distinto). Saldo
> insuficiente = `201` con `status=REJECTED` (sin tocar saldos; reintentable con la
> misma clave). Solo UUIDs en respuestas (regla de oro 9).
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
> Contrato E2-T14 tope pre-aprobado (HU07 `CA-03`, integrado al motor de
> `E2-T07`): cuando la transferencia (`POST /transfers/own`/`third-party`,
> `beneficiary_id` explicito o `metadata["beneficiary_id"]`) apunta a un
> beneficiario del titular cuyo `preapproved_limit_minor >= amount_minor +
> fee_minor` y misma moneda, la operacion queda **exenta del reto biometrico**
> (se omite `PENDING_AUTHORIZATION`; se autoriza directo). El evento
> `transaction.initiated` viaja con `exemption_reason="PREAPPROVED_LIMIT"` y
> `beneficiary_id` (UUID; sin PII) cuando aplica; en otro caso ambos son
> `null`. Moneda distinta al tope -> rechazo de validacion
> (`PreapprovedCurrencyMismatchError`, `400 VALIDATION_ERROR`); la exencion no
> ignora el limite diario del perfil (`E2-T18`) ni los bloqueos de `risk`. Sin
> endpoint nuevo (reusan las transferencias existentes).

### 6.4 Creditos (`credits`) - HU09-HU12

| Metodo | Ruta | Proposito |
|---|---|---|
| GET | `/credits/products` | Productos y tasas. |
| POST | `/credits/simulate` | Simulacion de cuota/cronograma. |
| POST | `/credits/applications` | Solicitud + autorizacion de bureau. |
| GET | `/credits/applications/{id}` | Estado del expediente. |
| POST | `/credits/applications/{id}/contract` | Genera contrato. |
| POST | `/credits/applications/{id}/sign` | Firma con biometria. |
| GET | `/credits/loans` | Prestamos del cliente. |
| GET | `/credits/loans/{id}/schedule` | Cronograma definitivo. |
| POST | `/credits/loans/{id}/pay` | Pago de cuota. |

### 6.5 Billetera, QR y servicios (`wallet`) - HU13-HU16

| Metodo | Ruta | Proposito |
|---|---|---|
| GET | `/wallet` | Billetera y vinculos. |
| POST | `/wallet/links` | Vincular cuenta/tarjeta. |
| POST | `/merchants/qr` | Generar QR de cobro (HU14). |
| GET | `/merchants/qr/{id}` | Consultar estado del QR. |
| GET | `/services/catalog` | Catalogo de servicios y operadores. |
| POST | `/services/payments` | Pago de servicio. |
| POST | `/topups` | Recarga de celular. |

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
>   la clave (entero `>= 0`, etc.), es idempotente por naturaleza (misma clave
>   + mismo valor = mismo estado) y audita cada cambio (`updated_by` del JWT +
>   `updated_at`, sin PII). No mueve dinero (sin `Idempotency-Key`) y **no**
>   emite `parameter.updated` (decision E2-T18; la trazabilidad la dan las
>   columnas).
> - Errores: `401 NOT_AUTHENTICATED`; `403 NOT_AUTHORIZED` (sin rol admin);
>   `404 NOT_FOUND` (clave inexistente al escribir); `422 VALIDATION_ERROR`
>   (valor de tipo/formato invalido). Cambiar el umbral/topes/limites se
>   refleja en el motor sin despliegue (fachada `shared/config`, regla de oro
>   6).

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
