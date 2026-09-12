# 15 - Calidad, seguridad y DevOps

## 1. Estrategia de pruebas

| Nivel | Que cubre | Herramienta sugerida |
|---|---|---|
| Unitarias | Dominio puro: calculo de cuotas, cuadre contable, maquina de estados, reglas de riesgo. | `pytest`. |
| Integracion | Modulo + base de datos: casos de uso del motor, idempotencia, holds. | `pytest` + Postgres de prueba. |
| Contrato | Respeto de los contratos OpenAPI internos y del microservicio KYC. | `schemathesis`/`pytest`. |
| E2E | Flujos completos: KYC -> cuenta -> transferencia; credito -> desembolso; QR. | Flutter integration tests + API. |
| Carga | Motor < 200 ms; picos de fin de mes. | `locust`/`k6`. |
| Seguridad | Fuerza bruta, permisos, enmascaramiento, inyeccion, idempotencia maliciosa. | Revision + `bandit` + `zap` basico. |
| Regresion | Al cierre de cada sprint, sobre lo ya entregado. | Suite automatizada. |

### 1.1 Casos de prueba obligatorios del motor

Ver seccion 12 de `04-motor-transaccional-y-ledger.md`. Son **bloqueantes** para cerrar HU17/HU18.

### 1.2 Datos de prueba (Q-T01 implementado en `feature/qa-bootstrap`)

- **BD de prueba:** `banca_test` (override con `TEST_DATABASE_URL`). Se crea si falta y se
  migra con `alembic upgrade head`. El loader se niega a operar sobre BDs que no contengan
  `test` en el nombre, salvo flag explicito.
- **Fuente de verdad:** `backend/tests/factories.py` (`build_seed_dataset(seed)`). Determinista:
  UUID v5 por nombre, `random.Random(seed)` (defecto `SEED=42`), JSON canonico + huella
  `sha256`. Misma semilla => mismo dataset en cualquier entorno.
- **Contenido simulado (cero PII real):** 4 usuarios demo (`@example.com`, docs `000...`),
  4 cuentas PEN/USD con saldos en centimos, catalogo contable minimo (8 cuentas), 2 comercios
  QR, 3 billers, 2 asientos cuadrados y llaves de idempotencia de ejemplo.
- **Script versionado:** `backend/scripts/seed_demo.py` con `check` (reproducibilidad),
  `dump` (snapshot canonico) y `load` (crea BD, migra, persiste en `qa.seed_snapshots` con
  upsert idempotente y verifica el baseline: 13 esquemas + 13 parametros de `config`).
- **Fixtures (`backend/tests/conftest.py`):** `client` (sin BD), `test_engine` (1 vez/sesion),
  `db_session` (transaccion + rollback: cada prueba arranca en estado conocido), `db_client`
  (override de `get_db`), `seeded_snapshot`. Sin Postgres, las pruebas de integracion hacen
  `skip`; las unitarias siempre corren.
- **Comandos (desde `backend/`):**
  `python scripts/seed_demo.py check|load` y `pytest` (15 pruebas Q-T01 en verde).
- **Extension:** cuando cada modulo cree sus tablas, se agregan loaders con upsert por `id`
  sobre estos mismos diccionarios, sin cambiar el contrato. Nunca usar datos personales reales;
  los scripts de seed estan versionados.

## 2. Trabajo de desarrollo

- Ramas: `main` protegida; trabajo en `feature/<modulo>-<tema>`; PR obligatorio con revision.
- Commits pequenos y descriptivos.
- Un PR no se aprueba sin: pruebas verdes, sin secretos, sin `TODO` bloqueantes, documentado.
- Migraciones Alembic revisadas por el responsable de datos.
- Sin cambios directos en `main`.

## 3. Seguridad - checklist

| Tema | Regla |
|---|---|
| Autenticacion | JWT corto + refresh rotativo; bloqueo tras 5 intentos; revocacion de sesiones. |
| Autorizacion | RBAC segun matriz de accesos; principio de minimo privilegio. |
| PII | Enmascarar documento/cuenta; cifrar datos sensibles; no loggear PII ni frames. |
| Cifrado | TLS en transito; AES-256 en reposo; hash para bitacora. |
| Secretos | Variables de entorno; `.env` ignorado; rotacion; nunca en el repo. |
| Entrada | Validacion estricta en la frontera; rechazo de tipos y montos invalidos. |
| Idempotencia | Obligatoria en endpoints de dinero; evita doble cobro por reintento o ataque. |
| Abuso | Rate limiting, captchas progresivos, limites diarios. |
| Integridad | Hash encadenado en asientos y auditoria; control de cuadre diario. |
| Dependencias | Escaneo de vulnerabilidades en CI. |

## 4. CI/CD

Pipeline (GitHub Actions):

1. **Lint + formato** (`ruff`, `black`; `dart analyze`; ESLint).
2. **Pruebas unitarias** e **integracion** (levanta Postgres/Redis como servicios).
3. **Chequeo de migraciones** (Alembic up/down en base limpia).
4. **Build** de imagenes Docker (backend, worker, panel).
5. **Escaneo** de dependencias.
6. **Despliegue** a entorno demo (manual o por tag).

## 5. Entornos y contenedores

`docker-compose` local con: `postgres`, `redis`, `backend`, `worker`, `admin-web`, y opcional el
`kyc-service` (o apuntar al ya desplegado). Variables en `.env` (plantilla `.env.example`).
Un solo comando debe levantar el entorno demo para la sustentacion.

## 6. Observabilidad

- Logs JSON con `request_id`, `user_id`, `module`, `event`.
- Healthchecks por modulo y global.
- Metricas: latencia, errores, transacciones por estado, alertas, cuadre del ledger.
- Panel minimo de estado para la demo.

## 7. Respaldos y datos

- Respaldo periodico de Postgres (script) y del volumen de modelos del KYC.
- Posibilidad de reconstruir la semilla en cualquier entorno.
- Poligono de recuperacion basico (no productivo) para la exposicion.

## 8. Tareas

- `Q-T01` Armar el esqueleto de pruebas y la semilla. *QA/Datos.*
- `Q-T02` Suite del motor (12 casos obligatorios). *QA.*
- `Q-T03` Suite de seguridad (fuerza bruta, permisos, enmascaramiento). *Seguridad.*
- `Q-T04` Pruebas de contrato KYC y OpenAPI. *QA.*
- `Q-T05` Pipeline CI completo. *DevOps.*
- `Q-T06` docker-compose de demo + healthchecks. *DevOps.*
- `Q-T07` Logs estructurados y tablero de estado. *DevOps.*
- `Q-T08` Escaneo de dependencias y secretos. *Seguridad.*
- `Q-T09` Guion y datos para la demo final (con video de respaldo). *Todo el equipo.*

## 9. Criterios de salida

- CI verde en `main`.
- Cero secretos en el repositorio.
- Suite del motor y de permisos al 100% de los casos criticos.
- Entorno demo levanta con un comando y pasa el guion de la exposicion.
