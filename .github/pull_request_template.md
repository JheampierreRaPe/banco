## Que

<!-- 1-3 lineas. Si necesitas "y", son dos PRs. -->

## Brief / corte

- Brief: `docs/tasks/<ID>.md`
- Corte: <ej. "dominio + pruebas" / "API + pruebas" / "wiring">
- Base: `<rama>` (debe ser la ultima entrega integrada)

## Alcance

- Modulos tocados: <uno>
- Archivos principales: <lista corta>
- **No incluye**: <explicito>

## Tamano

- `git diff --shortstat <base>...HEAD`: <N archivos, +X/-Y>
- Lineas semanticas (sin tests/docs/lock): <N>
- Dentro del presupuesto (`docs/21-politica-prs.md`): [ ] Si [ ] No
- Si no: justificacion y por que no se puede cortar:

## Evidencia

- [ ] `pytest` / `flutter test` / `npm test` en verde
- [ ] `ruff` + `black` / `flutter analyze` / `tsc` limpios
- [ ] `alembic check` + un solo head (si aplica)
- [ ] CI del PR en verde

## Riesgo y rollback

- Riesgo: <bajo/medio/alto>
- Rollback: revert del PR (es independiente? si/no)
- Migracion reversible: si/no/No aplica

## Que NO se toco

- Otros modulos, `kyc-service/`, secretos, PII en logs.
