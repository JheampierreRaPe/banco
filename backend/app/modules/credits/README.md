# Modulo credits

Contrato y fronteras: ver `docs/modules/README.md#credits`.

Estado: catalogo `loan_products` (E3-T02, HU09) y simulador (E3-T03)
implementados — modelos, repositorios, servicios, endpoints y migracion
`0029`. El motor de reglas (E3-T07) y el expediente + solicitud +
revision del analista (E3-T08, HU10) llegan en tareas posteriores.

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
