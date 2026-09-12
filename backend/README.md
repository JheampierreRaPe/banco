# Backend - Banca Online Integral

Monolito modular en Python/FastAPI. Un paquete por *bounded context* en `app/modules/`.

## Requisitos

- Python 3.11+
- PostgreSQL 16 (y Redis para cache/idempotencia)

## Puesta en marcha

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

- App: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`

## Pruebas

```bash
pytest
```

## Estructura

```
backend/
  app/
    main.py            # ensambla routers
    core/              # config, db, logging, errores, seguridad, health, eventos
    shared/            # Money, ids, utilidades transversales
    modules/<contexto> # api / schemas / domain / service / repository / models / events / jobs
    adapters/          # integraciones externas (KYC, bureau, FX, etc.)
  migrations/          # Alembic (multi-esquema)
  tests/
```

## Reglas

- El dinero va en enteros (`*_minor`) + `currency`; nunca `float`.
- Ningun modulo accede a tablas de otro; solo fachadas o eventos.
- Toda operacion de dinero pasa por `transactions` y `ledger`.
- Los parametros configurables viven en `config.parameters`, no en el codigo.
