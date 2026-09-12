# packages

Librerias transversales puras y publicables (reutilizables en otras aplicaciones).

Candidatos:

- `money`: objeto de valor de dinero.
- `security`: utilidades de tokens.
- `events`: contrato de eventos de dominio y outbox.
- `ledger-core`: motor contable de partida doble (puro y testeable).

Cada paquete con su `pyproject.toml` y version independiente para poder publicarse o extraerse a
su propio repositorio. Regla: si un paquete deja de importar codigo de otro dominio, esta listo
para separarse. Ver `docs/02-arquitectura.md` seccion 6.
