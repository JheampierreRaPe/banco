# Modulo credits

Contrato y fronteras: ver `docs/modules/README.md#credits`.

Estado: catalogo `loan_products` (E3-T02, HU09), simulador (E3-T03),
motor de reglas (E3-T07), expediente + solicitud + revision del
analista (E3-T08, HU10), juego contractual (E3-T12, HU11) y servicio
de firma (E3-T13, HU11) implementados — modelos, repositorios,
servicios, endpoints, eventos por `outbox`, notificacion y auditoria
(migraciones `0029`/`0030`/`0031`/`0032`/`0033`; E3-T13 sin migracion:
sello derivado). `loans`/cronogramas definitivos son del resto del
Sprint 3 (HU12).

## Motor financiero puro (E3-T01, HU09)

`domain/financial.py`: cuota francesa, cronograma y TCEA. Puro (sin BD/I/O),
deterministico; dinero en centimos `int`, `Decimal` interno, jamas `float`.

- Tasa mensual equivalente: `i = (1 + TEA) ** (1/12) - 1`.
- Cuota: `P * i / (1 - (1 + i) ** -n)`; tasa 0 -> `P / n`.
- Redondeo `ROUND_HALF_UP` a centimo por componente; la ultima cuota absorbe
  el residuo: `sum(principal) == P` y saldo final 0.
- Desgravamen: `insurance_rate` MENSUAL sobre el saldo insoluto al inicio del
  periodo; `0` no altera la cuota. Suma a `total_minor` de cada cuota.
- TCEA por TIR mensual del flujo `[-P, total_1..total_n]` (incluye seguro):
  `TCEA = (1 + irr) ** 12 - 1`. Se exponen TEA (interes) y TCEA (costo total).
- Errores de validacion: `FinancialValidationError(ValueError)`.

## Endpoints del simulador (E3-T03, HU09 CA-01..CA-04)

`api/` (`GET /credits/products`, `POST /credits/simulate`,
`POST /credits/simulate/export`), `schemas/simulation.py` (Pydantic),
`service/simulation.py` (orquesta catalogo + motor) y
`service/simulation_pdf.py` (PDF con patron `pdf_available` + `501`).

- Cliente delgado: cuota/TEA/TCEA/cronograma se calculan en el backend.
- Efimero: solo lectura de `loan_products`; sin `loan_applications`,
  sin `transactions`/`ledger`, sin eventos. `POST` con `200` (no crea
  recurso) y sin `Idempotency-Key`.
- Con `product_id` mandan tasa y topes del catalogo (`tea`, o su `tcea`
  si `tea` es `NULL`); sin el, `tcea` explicita en `0..5`, monto
  `1..10^12`, plazo `1..360`, desgravamen `0`. Tasas como strings
  decimales exactos en JSON (sin `float`).
- Contrato detallado en `docs/05-contratos-api.md#64-creditos-credits---hu09-hu12`.

## Motor de reglas de credito (E3-T07, HU10 CA-03 parte reglas)

`domain/rules.py` (`evaluate(...) -> CreditDecision`) puro (sin BD/I/O) y
determinista: mismas entradas + mismos parametros -> mismo dictamen y
mismo `reason` (listo para `loan_applications.decision_reason`, E3-T08).

- DTI exacto `total_debt / total_income` (`Decimal`; salida cuantizada a 4
  decimales para `NUMERIC(12,4)`, acotada por el repositorio si es
  extrema sin cambiar el dictamen); `> loan.max_dti_ratio` -> `REJECTED`.
- Tope por perfil (`requested > loan.max_amount[_PERFIL]_minor`, override
  opcional `loan.max_amount_{PERFIL}_minor`) y tope de deuda total
  (`total_debt > loan.max_debt_minor`) -> `REJECTED`.
- Score del bureau (contrato `BureauScore` de E3-T06): `< loan.min_score`
  -> `REJECTED`; banda `[min, min + review_margin)` o bureau caido
  (`None`) -> `IN_REVIEW`; resto -> `APPROVED`.
- Validacion (`CreditRulesValidationError` -> `VALIDATION_ERROR`):
  ingreso/monto/plazo `<= 0`, deuda negativa, score corrupto.
- Parametros `loan.*` via `shared/config` (dueno del esquema):
  `service/evaluation.py::evaluate_application` lee la foto vigente sin
  cache (cambio efectivo sin despliegue) y acepta `params_override` para
  previsualizar. Clave ausente o valor invalido -> fallback documentado
  (`DEFAULT_*` = semillas `0001`/`0030`); la forma texto `"0.4"`
  (semilla `0001`) se acepta igual que el numero.
- La `morosidad` del bureau no altera el dictamen (se conserva en el
  expediente, E3-T08).

## Expediente + solicitud + revision del analista (E3-T08, HU10 CA-01..CA-04)

`models/` (`LoanApplication`, `docs/03b#82`; migracion
`0031_credits_loan_applications`), `repository/applications.py` (CRUD +
transiciones con `CONFLICT`), `schemas/applications.py` (Pydantic),
`service/applications.py` (casos de uso) y `api/applications.py`
(`POST/GET /credits/applications`, bandeja y dictamen del analista).

- Consentimiento explicito obligatorio (`bureau_authorized=true`; el
  servidor estampa `bureau_authorized_at`): sin el -> `422` sin evaluar
  ni consultar al bureau.
- Flujo `SUBMITTED -> IN_EVALUATION -> {APPROVED|REJECTED|IN_REVIEW}`;
  `POST` -> `201` con dictamen o `202` si se difiere; bureau caido
  (`BUREAU_UNAVAILABLE`/`BUREAU_TIMEOUT`) -> `IN_REVIEW`, nunca 500.
- Deuda efectiva = declarada + externa del bureau (`deudas_minor`).
- `Idempotency-Key` obligatoria en el `POST`: snapshot minimo
  (`application_id` + `status`, sin PII); replay sin re-evaluar ni
  re-notificar (una sola notificacion).
- Eventos por `outbox` (`events/`: `credit.application.submitted`,
  `credit.approved`, `credit.rejected`, `credit.review.required`,
  payload sin PII); auditoria best-effort (`credit.application.decided`
  / `...reviewed`); notificacion push best-effort por la fachada
  `notifications.send` (plantillas `credit_*`).
- Revision separada: solo `IN_REVIEW -> APPROVED|REJECTED` con
  justificacion y rol analista (`analyst`/`credit_analyst`); decidido no
  se reevalua (`409`); `GET` ajeno -> `404` neutro.

## Juego contractual (E3-T12, HU11 CA-01)

`models/` (`Contract`, `docs/03b#86` + `application_id`/`doc_type`;
migracion `0033_credits_contracts`), `repository/contracts.py` (una
fila por documento, UQ `(application_id, doc_type)`),
`schemas/contracts.py` (Pydantic), `service/documents.py` (contenido
canonico + hash + PDF) y `api/contracts.py` (`POST` genera /
`GET` metadatos o descarga).

- Tres documentos separados (decision del dueno): `CONTRATO` +
  `PAGARE` + `HOJA RESUMEN`, cada uno con su `doc_ref` (ruta logica
  `credits/contracts/<app>/<slug>.pdf`, sin PII) y su `doc_hash`
  (SHA-256 del contenido canonico: mismas condiciones -> mismo hash).
- Condiciones copiadas del expediente `APPROVED` + producto + motor
  E3-T01 (tasa = `tea` o su `tcea`; desgravamen = `insurance_rate` si
  > 0; cronograma sin fechas: el desembolso es HU12). Otro estado ->
  `409`; ajeno/inexistente -> `404` neutro.
- `POST` con `Idempotency-Key` obligatoria (`201` / `200` replay) e
  idempotente por naturaleza (la UQ absorbe carreras y reintentos con
  otra clave); un solo evento `credit.contract.generated` por `outbox`
  + auditoria best-effort (`credit.contract.generated`).
- `GET ?format=json` (metadatos) o `?format=pdf&doc=contrato|pagare|
  resumen` (descarga, re-render determinista); sin backend PDF ->
  `501 EXPORT_FORMAT_NOT_SUPPORTED` sin romper la generacion
  (patron E3-T03/E2-T10).
- `loan_id` UUID logico sin FK (`NULL` en HU11; lo puebla HU12).
- **No mueve dinero**: sin `loans`/`loan_schedules`, sin
  `transactions`/`ledger`, sin asientos.
- Contrato detallado en `docs/05-contratos-api.md#64-creditos-credits---hu09-hu12`;
  gaps en `docs/18` (G53/G54/G55).

## Servicio de firma (E3-T13, HU11 CA-02/CA-03)

`domain/signing.py` (sello puro y determinista) + `service/signing.py`
(`request_signature_challenge`, `sign_contract` por biometria del
dispositivo, `sign_contract_with_pin` como fallback) +
`repository/contracts.py::find_by_token_hash` (no reutilizacion).

- Reto: `nonce` de un solo uso via `identity.domain.nonce` (TTL 120 s,
  sin persistir; decision 4 del brief); sin juego -> `404` neutro;
  juego ya firmado -> `409`.
- Via dispositivo: consume el `nonce`, lo liga al titular y verifica la
  firma contra el binding **vigente** (`ACTIVE`) por fachada de
  `identity` (`device_login.verify_signature` + `get_device_binding`,
  D-03). Via PIN (fallback, patron E2-T31): verifica por la fachada
  `pin_authorization.verify_holder_pin` (comparacion constante, rama
  ciega); el PIN jamas se persiste ni se loguea.
- Cualquier desajuste responde el generico `NOT_AUTHORIZED` (sin
  oraculo); solo el TTL agotado distingue (`EXPIRED_NONCE`).
- Sellado (no repudio, sello **derivado**, sin migracion): las 3 filas
  pasan a `SIGNED` con `signed_at` del servidor +
  `signature_token_hash` (SHA-256; en dispositivo liga
  firma+nonce+device+expediente, en PIN liga expediente+instante+sal
  aleatoria); el `seal` (`doc_hash` + `signed_at` +
  `signature_token_hash`) se expone en la respuesta para verificar
  integridad. Token repetido en otra firma -> `CONFLICT`.
- Evento `credit.contract.signed` por `outbox` (payload sin PII:
  `contract_ids`, `application_id`, `user_id`, `status`, `signed_at`) +
  auditoria best-effort (`credit.contract.signed`). Sin router propio
  (los endpoints van en E3-T14).
- **No mueve dinero**: sin `loans`/`loan_schedules`, sin
  `transactions`/`ledger`, sin asientos.
- Gaps en `docs/18` (G56).

## Endpoints de firma + copia por correo (E3-T14, HU11 CA-02/CA-03/CA-04)

`api/signing.py` (`POST /credits/applications/{id}/sign/challenge` y
`POST .../sign`), `schemas/signing.py` (Pydantic) y
`service/contract_signing.py` (orquesta `signing` de E3-T13 +
`notifications.send` best-effort con `Idempotency-Key` obligatoria).

- Reto: `POST .../sign/challenge` (cuerpo opcional `{device_id?}`;
  **sin** `Idempotency-Key`: solo rota el `nonce` de un solo uso, TTL
  120 s, patron G39) -> `200 {application_id, nonce, expires_in,
  status}`. Repetir reemite; sin juego -> `404` neutro; ya `SIGNED` ->
  `409`.
- Firma: `POST .../sign` (exactamente un factor: `{device_id,
  signature[, nonce]}` o `{pin}`; `422` si ambos o ninguno, sin eco
  del cuerpo) -> `200 {application_id, contract_id, status
  ("SIGNED"), signed_at, via, documents[3 con seal]}` (+
  `meta.replayed`). Misma clave + mismo cuerpo = replay (una firma y
  **un** correo); misma clave + cuerpo distinto -> `409
  IDEMPOTENCY_CONFLICT`; fallo del factor (`403 NOT_AUTHORIZED`
  generico / `400 EXPIRED_NONCE`) snapshotteado (reintento real con
  reto rotado + clave nueva).
- Correo: tras el exito, copia por `notifications.send(channel="email",
  template_code="credit_contract_signed")` al `users.email` del titular
  (resuelto por la fachada de solo lectura
  `identity.service.holder_contact.get_holder_email`, D-03), con el
  sender vigente: real `GmailNotificationSender` (SMTP) con
  `EMAIL_PROVIDER=gmail` + `GMAIL_USER`/`GMAIL_APP_PASSWORD` solo en
  `.env` (ver `backend/.env.example`), mock sin red por defecto.
  **Best-effort**: sin correo o con el proveedor caido la firma queda
  `SIGNED` igual (notificacion `FAILED`/omitida, sin PII en logs). El
  aviso remite al expediente para descargar el PDF (`GET
  .../contract?format=pdf`): adjuntar el PDF exigiria ampliar el
  adaptador (fuera de `credits`, `docs/18` G57).
- Snapshot de idempotencia minimo y sin PII (`application_id` +
  `status`/`documents`; del PIN/firma solo digests en el
  `request_hash`, patron E2-T31).
- **No mueve dinero**: sin `loans`/`loan_schedules`, sin
  `transactions`/`ledger`, sin asientos.
- Contrato detallado en `docs/05-contratos-api.md#64-creditos-credits---hu09-hu12`;
  gaps en `docs/18` (G57).
