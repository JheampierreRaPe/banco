# Modulo credits

Contrato y fronteras: ver `docs/modules/README.md#credits`.

Estado: catalogo `loan_products` (E3-T02, HU09), simulador (E3-T03),
motor de reglas (E3-T07) y expediente + solicitud + revision del
analista (E3-T08, HU10) implementados — modelos, repositorios,
servicios, endpoints, eventos por `outbox`, notificacion y auditoria
(migraciones `0029`/`0030`/`0031`). `loans`/cronogramas/contratos son
del Sprint 3.

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
