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

Codigos de error de negocio (ejemplos): `VALIDATION_ERROR`, `INSUFFICIENT_FUNDS`,
`LIMIT_EXCEEDED`, `DUPLICATE_REQUEST`, `ACCOUNT_BLOCKED`, `BENEFICIARY_NOT_FOUND`,
`KYC_REQUIRED`, `BIOMETRIC_REQUIRED`, `BIOMETRIC_FAILED`, `QR_EXPIRED`, `QR_TAMPERED`,
`RATE_EXPIRED`, `RISK_BLOCKED`, `SCREENING_HIT`, `NOT_AUTHORIZED`, `NOT_FOUND`.

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
| POST | `/auth/kyc/submit` | Envia documento + segmentos de liveness y obtiene resultado. |
| POST | `/auth/activate` | Valida OTP y activa la cuenta (HU02). |
| POST | `/auth/otp/resend` | Reenvia OTP con control de intentos. |
| POST | `/auth/login/challenge` | Emite `nonce` para firmar con biometria del dispositivo (HU03). |
| POST | `/auth/login/facial` | Valida el `nonce` firmado y abre sesion (HU03, biometria local). |
| POST | `/auth/login/pin` | Login alterno con PIN. |
| POST | `/auth/refresh` | Renueva tokens. |
| POST | `/auth/logout` | Revoca la sesion. |
| POST | `/auth/recover` | Recuperacion con dispositivo confiable + OTP (HU04). |
| POST | `/auth/recovery/request` | Solicita OTP de recuperacion por email, siempre 200 sin enumerar (E1-T31; consume `F-T29`). |
| POST | `/auth/recovery/verify` | Valida el OTP, abre sesion y liga el dispositivo nuevo (E1-T31; consume `F-T29`). |
| GET | `/me` | Perfil y productos del usuario. |

> Decision E1-T31: el `/auth/recover` canonico de HU04 (dispositivo confiable +
> `nonce` firmado + cambio de credencial) queda como esta. Los dos endpoints nuevos
> son pre-sesion para el caso "sin `user_ref` local" (reinstalar/borrar datos):
> `request {email}` -> `data: {accepted: true, ttl_seconds, resend_wait_seconds}`
> (constantes globales, identico exista o no el email; `429 RATE_LIMITED` por
> `email+IP`); `verify {email, code[, device_id/device_public_key/platform/
> biometric_type]}` -> `data: {access_token, refresh_token, token_type: "Bearer",
> session_id, expires_in, user_ref, device_bound}` (`user_ref = str(user.id)`, lo
> persiste `F-T29`; `device_bound=true` solo si se REGISTRO un binding nuevo).
> Errores: `401 INVALID_RECOVERY_CODE` (mismo cuerpo para email no registrado, no
> elegible, sin OTP, codigo incorrecto, OTP vencido y OTP bloqueado por intentos
> agotados: vencido/bloqueado colapsan al generico para no filtrar existencia;
> ya NO existe `400 EXPIRED_OTP` en este flujo —fix anti-oraculo E1-T31, ver
> `docs/tasks/E1-T31.md` §6.1; `F-T29` debe tratar el vencimiento como generico
> con reintento), `429 RATE_LIMITED` (solo ventana de solicitud/verificacion por
> `email+IP`). OTP `RECOVERY` solo por
> email (plantilla `otp_code_email`; nunca SMS), cooldown reutilizando el `PENDING`
> vigente, auditoria `auth.recovery_requested` / `auth.access_recovered` (sin PII).

Detalle del flujo KYC (`identity`, E1-T29):

- `POST /auth/kyc/challenge` -> `data: {token, steps: [str], expires_in: int}`.
- `POST /auth/kyc/evaluate` -> request `{challenge_token, step, frames_b64: [str]}`
  y `data: {step, passed, reason, frames_analyzed, details}`.
- `POST /auth/kyc/submit` -> cada segmento acepta `frames_b64: [str]` (rafaga; fuente
  unica) **o** `image_b64: str` (compatibilidad, un solo frame); `document` lleva
  `{type, number, image_b64}` y `applicant` los datos del titular. La respuesta amplia
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

### 6.2 Cuentas y beneficiarios (`accounts`) - HU05, HU07

| Metodo | Ruta | Proposito |
|---|---|---|
| GET | `/accounts` | Consolidado de cuentas y saldos. |
| GET | `/accounts/{id}` | Detalle con disponible/retenido/contable. |
| GET | `/accounts/{id}/movements` | Movimientos paginados. |
| GET | `/accounts/{id}/movements/export` | Export PDF/Excel. |
| GET/POST/PUT/DELETE | `/beneficiaries` | Gestion de beneficiarios frecuentes. |

### 6.3 Transacciones (`transactions`) - HU06, HU08, HU15, HU17

| Metodo | Ruta | Proposito |
|---|---|---|
| POST | `/transfers/own` | Transferencia entre cuentas propias. |
| POST | `/transfers/third-party` | Transferencia a tercero (mismo banco). |
| POST | `/transfers/interbank` | Transferencia interbancaria (202 + estado). |
| POST | `/payments/qr` | Pago de QR (HU15). |
| POST | `/transactions/{id}/authorize` | Autorizacion biometrica de pago sensible (HU08). |
| GET | `/transactions/{id}` | Estado y detalle. |
| GET | `/transactions` | Historial filtrable. |
| GET | `/transactions/{id}/receipt` | Comprobante. |
| POST | `/transactions/{id}/reverse` | Reverso autorizado (operaciones). |

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
