# Migraciones (Alembic multi-esquema)

- Un **esquema por modulo** (`identity`, `accounts`, ...); el nombre coincide con el modulo.
- **Sin FK entre esquemas**: las referencias cruzadas son logicas (`REF`).
- Toda migracion debe ser reversible (`upgrade` / `downgrade`).
- Las tablas de negocio las crea **cada modulo** en su tarea; aqui solo vive `0001` con los
  esquemas base y `config.parameters`.

## Comandos

```bash
cd backend
alembic upgrade head          # aplicar
alembic downgrade base        # revertir todo
alembic revision -m "..."     # nueva revision (autogenerate opcional)
alembic upgrade head --sql    # modo offline
```

## Convenciones

- Mensaje en `snake_case` y descriptivo.
- `compare_type=True` e `include_schemas=True` ya configurados en `env.py`.
- Revisar toda migracion autogenerada antes de confirmarla.
