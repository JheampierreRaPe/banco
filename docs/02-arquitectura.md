# 02 - Arquitectura

## 1. Principios rectores

1. **El dinero nunca se sobrescribe**: todo movimiento deja rastro contable (partida doble).
2. **Modularidad estricta**: cada dominio es un modulo con frontera clara; nada accede a las
   tablas de otro modulo.
3. **Extraible por diseno**: cualquier modulo debe poder convertirse en microservicio y
   publicarse en su propio repositorio sin reescribir su logica.
4. **Configurable, no cableado**: umbrales, tasas, limites y reglas viven en base de datos.
5. **Trazable**: toda accion sensible se audita; nunca se borra, se revierte.
6. **Simulado pero realista**: los terceros reales se sustituyen por adaptadores con el mismo
   contrato que tendria el proveedor real.

## 2. Stack tecnologico

| Capa | Tecnologia | Motivo |
|---|---|---|
| Backend | Python 3.12 + FastAPI | Coincide con el microservicio KYC. |
| ORM / migraciones | SQLAlchemy 2 + Alembic | Migraciones versionadas por modulo. |
| Base de datos | PostgreSQL 16 | ACID, esquemas, tipos `uuid`, `numeric`. |
| Cache / idempotencia / colas | Redis 7 | Idempotency keys, cache, broker de tareas. |
| Tareas asincronas | Celery (o APScheduler para el MVP) | Cierres, conciliacion, intereses, notificaciones. |
| Auth | JWT (access + refresh) + API Key entre servicios | Estateless para el backend. |
| App cliente | Flutter | Biometria de dispositivo y movilidad. |
| Panel interno | Web (Vite + TS) | Tableros y grillas de datos; alternativa: Flutter Web. |
| Observabilidad | Logs estructurados (JSON) + healthchecks + metricas basicas | Diagnostico en curso. |
| Contenedores | Docker + docker-compose (local) / VPS gratuito | Portabilidad. |
| CI/CD | GitHub Actions | Build, tests, lint, migraciones. |

## 3. Vista de contexto (quien consume el sistema)

```
Cliente (app Flutter) ──┐
Comercio (app/QR)  ─────┼──► API Gateway / Backend (monolito modular) ──► PostgreSQL
Analista/Compliance/Auditor (panel web) ─┘         │
                                                   ├──► Microservicio KYC (externo, ya existe)
                                                   ├──► Adaptador Central de riesgo (simulado)
                                                   ├──► Adaptador Tipo de cambio (API/publica/simulada)
                                                   ├──► Adaptador Clearing/Interbancario (simulado)
                                                   ├──► Adaptador Listas restrictivas (simulado)
                                                   └──► Adaptador Notificaciones push/email/SMS (simulado)
```

## 4. Bounded contexts (modulos)

| Modulo | Responsabilidad | HU que cubre |
|---|---|---|
| `identity` | Registro, KYC, OTP, autenticacion, recuperacion, usuarios y sesiones. | HU01-HU04 |
| `accounts` | Cuentas, saldos, beneficiarios, consolidados. | HU05, HU07 |
| `transactions` | Motor transaccional: validacion, holds, idempotencia, estados. | HU06, HU08, HU15, HU17 |
| `ledger` | Asientos de partida doble, balances, cierres. | HU18 |
| `credits` | Simulacion, solicitud, evaluacion, contrato, desembolso, cuotas. | HU09-HU12 |
| `wallet` | Billetera, QR, pagos de servicios y recargas. | HU13-HU16 |
| `fx` | Multidivisa, bolsillos, meta de ahorro, cambio de divisas. | HU24, HU25 |
| `risk` | Antifraude, reglas de riesgo, listas restrictivas, ROS. | HU20, HU21 |
| `reconciliation` | Clearing, cruce y excepciones. | HU19 |
| `notifications` | Push, correo, SMS, plantillas y preferencias. | HU22 |
| `audit` | Bitacora append-only con hash encadenado. | HU23 |
| `admin` | Roles, permisos, parametros configurables. | Transversal |

## 5. Estructura del repositorio (monorepo)

```
banca-online/
  backend/
    app/
      main.py                 # ensambla los modulos
      core/                   # config, db, seguridad, eventos, outbox, errores
      shared/                 # Money, UUID, paginacion, reloj, ids
      modules/                # un paquete por bounded context
      adapters/               # clientes a servicios externos/simulados
    migrations/               # alembic (un historial por schema)
    tests/
  packages/                   # librerias puras publicables (p.ej. ledger-core)
  frontend/                   # app Flutter (cliente)
  admin-web/                  # panel interno
  infra/                      # docker, CI, scripts
  docs/                       # este plan
```

### 5.1 Estructura interna de cada modulo

```
modules/<contexto>/
  api/          # routers FastAPI (solo validacion y traduccion HTTP)
  schemas/      # Pydantic de entrada/salida
  domain/       # entidades, value objects y reglas de negocio puras
  service/      # casos de uso (orquestan dominio + repositorios)
  repository/   # acceso a datos del propio schema
  models/       # modelos ORM del propio schema
  events/       # eventos que publica y consume
  jobs/         # tareas programadas del modulo
  README.md     # contrato interno y responsables
```

### 5.2 Reglas de modularidad (obligatorias)

- Un modulo **solo** puede ser importado a traves de su fachada `service/` o de sus eventos.
- Prohibido importar `models/` o `repository/` de otro modulo.
- Prohibido hacer `JOIN` entre tablas de distintos schemas; se resuelve con IDs y eventos.
- Cada modulo es dueno de su esquema Postgres con el mismo nombre.
- La comunicacion sincrona se define con interfaces (Protocol); la asincrona con eventos.

## 6. Como se vuelve un modulo un microservicio reutilizable

Objetivo del usuario: **monolito modular en produccion, pero cada modulo publicable en git para
reutilizarlo en otras aplicaciones.**

1. La logica del modulo vive en `domain/` y `service/`, sin depender de FastAPI.
2. La frontera se expone con una **interfaz** (Protocol) que implementa tanto la version
   in-process como la version HTTP.
3. Cada modulo incluye un `standalone.py` que levanta un FastAPI con solo ese modulo, para
   correrlo solo.
4. Cada modulo tiene su `pyproject.toml` y su version; el monorepo puede publicarlo como paquete
   o extraerlo a un repositorio propio (`banca-<modulo>-service`).
5. `packages/` contiene lo genuinamente transversal: `money`, `security`, `events`,
   `ledger-core` (motor de asientos puro y testeable).
6. Regla de extraccion: si un modulo deja de importar (directa o indirectamente) codigo de otro
   dominio, esta listo para separarse.

> El microservicio KYC es el **primer ejemplo ya extraido** de este patron; sirve de plantilla
> para publicar los demas (Dockerfile, README, contrato, API key).

## 7. Comunicacion entre modulos

- **Sincrona (in-process en el MVP)**: el modulo `transactions` llama a `accounts` para
  verificar/actualizar saldos dentro de la misma transaccion ACID.
- **Asincrona (eventos)**: efectos secundarios (notificar, auditar, evaluar fraude, conciliar)
  se publican como eventos de dominio y se procesan fuera de la transaccion principal.
- **Patron Outbox**: el evento se guarda en la tabla `outbox` dentro de la misma transaccion
  que el cambio de negocio; un worker lo publica. Garantiza "no se pierde ni se duplica".
- **Idempotencia de consumidores**: cada consumidor registra el `event_id` procesado.

### Eventos de dominio iniciales

| Evento | Emisor | Consumidores |
|---|---|---|
| `kyc.completed` | identity | accounts, risk, audit, notifications |
| `user.activated` | identity | accounts, notifications |
| `account.created` | accounts | ledger, audit |
| `transaction.initiated` | transactions | risk, audit |
| `funds.held` / `funds.released` | transactions | accounts, audit |
| `ledger.entry.posted` | ledger | audit, reconciliation |
| `transfer.settled` | transactions | notifications, reconciliation |
| `fraud.alert.raised` | risk | transactions (bloqueo), notifications, audit |
| `credit.disbursed` | credits | transactions, ledger, notifications |
| `qr.payment.confirmed` | wallet | transactions, notifications |
| `reconciliation.exception` | reconciliation | risk, audit, operations |

## 8. Patrones obligatorios

| Patron | Donde | Regla |
|---|---|---|
| **Idempotencia** | Todo endpoint que mueve dinero | Header `Idempotency-Key`; misma clave + mismo cuerpo = mismo resultado. |
| **ACID** | transactions + ledger | El debito, el credito y el asiento se confirman o fallan juntos. |
| **Blqueo pesimista** | Saldos | `SELECT ... FOR UPDATE` sobre la fila de saldo de las cuentas implicadas, siempre en orden estable de IDs para evitar deadlocks. |
| **Optimistic locking** | Entidades de estado | Columna `version` para detectar carreras. |
| **Outbox** | Eventos | Nunca publicar directo en la transaccion de negocio. |
| **Saga / orquestacion** | Interbancario y credito | Estados explicitos con compensacion (reverso) ante fallo. |
| **CQRS ligero** | Consultas de saldos/movimientos | Vistas o cache de lectura; la escritura siempre por el motor. |
| **Circuit breaker + timeouts** | Adaptadores externos (KYC, buro, FX, interbancario) | Reintentos con backoff; nunca bloquear el hilo indefinidamente. |

## 9. Adaptadores externos simulados

Todos viven en `app/adapters/` con una interfaz comun y una implementacion `mock` activable por
configuracion. Cuando exista el proveedor real, se agrega una implementacion nueva sin tocar el
dominio.

| Adaptador | Simula | Contrato esperado |
|---|---|---|
| `KycProvider` | RENIEC + OCR + liveness | **Real**: microservicio FastAPI existente. |
| `CreditBureau` | Central de riesgo | `POST /score` -> {score, deudas, morosidad}. |
| `FxRateProvider` | API de tipo de cambio | `GET /rates?base=PEN` -> {quote, ttl}. |
| `InterbankGateway` | Red interbancaria / equipo par | `POST /transfers`, `GET /transfers/{id}`, estado final. |
| `ClearingSource` | Archivos de compensacion | Lote con operaciones; puede ser CSV/JSON simulado. |
| `SanctionsLists` | OFAC / World-Check / PEP | Cotejo difuso por nombre/documento. |
| `Notifications` | Push / correo / SMS | `send(canal, destinatario, plantilla, datos)`. |

## 10. Seguridad arquitectonica

- **Autenticacion**: JWT de acceso corto (minutos) + refresh; el access token lleva `sub`,
  `roles`, `session_id`. Revocacion por lista negra en Redis.
- **Biometria**: el servicio KYC (liveness) se usa **solo en la creacion de cuenta (HU01)**. El
  login se resuelve con un `nonce` firmado por el dispositivo y biometria local; recuperacion,
  pagos sensibles y firma de contrato usan tambien biometria del dispositivo (`device_bindings`).
- **Autorizacion**: RBAC segun la matriz de accesos (doc fuente hoja "Matriz de acceso").
  El rol `Cliente` solo ve lo propio; los analistas ven enmascarado.
- **Enmascaramiento**: numeros de cuenta, documento y datos personales se devuelven
  enmascarados por defecto; el detalle completo exige permiso y queda auditado.
- **Cifrado**: TLS en transito; AES-256 en reposo para datos sensibles/biometricos; hash para
  la bitacora. Los frames crudos de liveness **no se persisten**.
- **Secretos**: variables de entorno; nunca en el repositorio. `.env.example` como plantilla.
- **Auditoria**: todo cambio administrativo, login, verificacion facial y movimiento de dinero
  genera registro en `audit` (append-only con hash encadenado).
- **API entre servicios**: `X-API-Key` (como el microservicio KYC) o token de servicio + mTLS
  en una fase posterior.

## 11. Entornos

| Entorno | Uso | Datos |
|---|---|---|
| Local | Desarrollo con docker-compose (Postgres, Redis, backend, worker, admin). | Semilla simulada. |
| Demo/Staging | Exposicion y pruebas integrales. | Semilla simulada. |
| (No productivo) | Alta disponibilidad fuera de alcance. | - |

## 12. Observabilidad

- Logs JSON con `trace_id`, `user_id`, `module`, `event`.
- Healthcheck por modulo (`/health`) y global (`/health/ready`).
- Metricas minimas: latencia por endpoint, tasa de error, transacciones por estado, alertas de
  riesgo, saldo del ledger (debitos = creditos).
- Panel simple de estado para la sustentacion.

## 13. Convenciones de nombres

- Schemas Postgres = nombre del modulo (`identity`, `ledger`, ...).
- Tablas en plural, `snake_case`; claves primarias `uuid`; timestamps `created_at`/`updated_at`.
- Endpoints REST `snake_case`; eventos `dominio.sustantivo.accion` en pasado.
- Dinero: columna `amount_minor` (entero de centimos) + `currency` (ISO 4217).
- IDs de operacion: `idempotency_key` unico + `transaction_id` (UUID).
