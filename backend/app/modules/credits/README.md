# Modulo credits

Contrato y fronteras: ver `docs/modules/README.md#credits`.

Estado: catalogo `loan_products` (E3-T02, HU09) implementado — modelo,
repositorio, servicio, semilla y migracion `0029`. El simulador
(E3-T03), el motor de reglas (E3-T07) y el expediente + solicitud +
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
