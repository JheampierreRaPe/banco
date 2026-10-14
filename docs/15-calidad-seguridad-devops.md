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

### 1.3 QA de HU12 (E3-T22 `Hecho`, 2026-10-13)

Suite de integracion/API sobre el app real
(`backend/tests/test_e3_t22_loans_qa.py`, 13 pruebas en verde; SQLite en memoria +
`TestClient`, sin red, sin `float`), complementaria —sin solapar— a las suites de
modulo `E3-T17` (desembolso), `E3-T18` (cronograma), `E3-T19` (debito/recordatorios)
y `E3-T20` (pago). Verifica los criterios de salida de HU12 (`docs/08#hu12...`):

- **CA-01 (desembolso):** latencia < 30 s; fallo DESPUES del asiento -> rollback total
  (0 tx, 0 asientos, saldo intacto) + recuperacion hacia adelante con una sola
  acreditacion; guarda G60 (`5000` es `asset` o la QA falla); cuadre global
  `sum(debitos)=sum(creditos)` tras desembolso + pago.
- **CA-02 (cronograma/cuota):** el `GET .../schedule` refleja `PAID`/`OVERDUE` y sigue
  cuadrado (`p+i+s=total`, `sum(capital)=principal`); vencida + sin fondos ->
  `REJECTED` intacta (sigue `OVERDUE`) y reintentable con la misma clave.
- **CA-03 (debito automatico):** el job cobra la cuota que vence HOY; ciclo completo
  desembolso -> debito automatico -> pagos manuales -> `PAID` (`outstanding=0`,
  ambos eventos, sin doble cobro al re-ejecutar).
- **CA-04 (recordatorio):** ventana exacta (solo `hoy+3`; 4 y 1 dia no avisan);
  unico por ventana aunque avance el dia; la cuota `PAID` en ventana no avisa.
- **Seguridad (`#3`):** respuestas sin PII (cuenta `****dddd`, sin UUID del titular),
  sin PII en logs del ciclo completo; `404` neutro (ajeno === inexistente, sin montos);
  aislamiento de los jobs y del motor de desembolso (sin escrituras directas ajenas).

### 1.4 QA de HU13 (E4-T05 `Hecho`, 2026-10-13)

Suite de integracion/API sobre el app real
(`backend/tests/test_e4_t05_wallet_qa.py`, 10 pruebas en verde; SQLite en memoria +
`TestClient`, sin red, sin `float`), complementaria —sin solapar— a las suites de
modulo `E4-T01` (vinculos/tokenizacion), `E4-T02` (express/limites) y `E4-T03`
(adaptador interop). Verifica los criterios de salida de HU13 (`docs/09#hu13...`):

- **CA-01 (vincular):** jornada cuenta + tarjeta mock -> `GET /wallet` refleja ambos
  enmascarados (`account_masked` `****dddd`, `last4`/`brand`); el PAN nunca aparece en
  `wallets`/`wallet_links`, respuestas, snapshots de idempotencia, `outbox` ni logs.
- **CA-02 (express sin clave / refuerzo):** pago <= base liquida sin reto (`SETTLED`,
  disponible baja, asiento cuadrado, eventos del motor + auditoria, latencia < 3 s);
  sobre el base queda `PENDING_AUTHORIZATION` y N PIN fallidos -> `REJECTED` con
  `BIOMETRIC_PREVENTIVE_BLOCK`, hold liberado y saldos intactos.
- **CA-03 (interoperabilidad):** cadena `encode -> decode -> validate_counterpart`
  determinista (mismo input -> mismo sobre `BO-QR1`); payload manipulado/formato
  invalido -> `INTEROP_VALIDATION_ERROR`; par caido -> `INTEROP_UNAVAILABLE`; sin red
  real y sin la referencia en logs.
- **CA-04 (limite diario):** acumulado + total > `wallet.daily_limit_minor` ->
  `REJECTED`/`DAILY_LIMIT_EXCEEDED` sin tocar saldos ni asientos; el limite es por
  titular (el otro titular sigue liquidando).
- **Seguridad (`#3`):** vinculo ajeno -> `NOT_FOUND` neutro sin efectos; `GET /wallet`
  no ve vinculos ajenos; cuenta ajena en `POST /wallet/links` -> `404` sin el numero
  en claro; `outbox` + auditoria sin PII; cuadre global `sum(debitos)=sum(creditos)`.

### 1.5 QA de HU14 (E4-T10 `Hecho`, 2026-10-13)

Suite de integracion/API sobre el app real
(`backend/tests/test_e4_t10_qr_qa.py`, 10 pruebas en verde; SQLite en memoria +
`TestClient`, sin red, sin `float`), complementaria —sin solapar— a las suites de
modulo `E4-T06` (firma/`qr_charges`), `E4-T07` (caducidad on-read + job) y `E4-T08`
(endpoints/aviso). Verifica los criterios de salida de HU14 (`docs/09#hu14...`):

- **CA-01 (QR generado):** `POST /merchants/qr` deja el cargo `ACTIVE` con firma
  verificable contra el secreto vigente y `payload_hash` determinista;
  generacion < 1 s; un solo evento `qr.charge.created` por `outbox`.
- **CA-02 (caducidad):** ciclo E2E vigente (`ACTIVE`) -> vencido on-read (`EXPIRED`
  sin mutar la fila) -> job que lo persiste; el vencido se rechaza por el gate
  (`is_charge_valid=False`) antes de operar, sin `PAID`/`paid_at` ni eventos
  nuevos (`docs/04#7.4`: sin retencion ni descuento).
- **CA-03 (interoperable):** el sobre `BO-QR1` del QR generado es (de)codificable
  por el adaptador de `E4-T03` (mismos `ref`/`amount_minor`/`currency`/`exp`/`sig`,
  token determinista); sobre truncado/manipulado -> `InteropValidationError`;
  manipular monto/comercio/vencimiento invalida la firma; firma corrupta
  (truncada/no-hex/vacia/nula/invertida) -> `False` sin romper el verificador.
- **CA-04 (notificacion de abono, pago simulado):** tras `mark_paid` (simula la
  confirmacion de HU15) se invoca `notifications.send(...)` una sola vez con la
  plantilla `qr_charge_paid` (push, latencia < 5 s); el `data` lleva solo
  `{charge_ref, at}` (sin cifras, sin RUC, sin razon social, sin firma) y el
  texto renderizado tampoco los expone. El E2E con el pago real
  (`POST /payments/qr`, `qr.payment.confirmed`) queda para la QA de HU15
  (`docs/18` G72).
- **Seguridad (`#3`):** respuestas del ciclo sin RUC en claro; logs de
  `app.modules.wallet` sin RUC/razon social en el ciclo crear -> avisar.

### 1.6 QA de HU15 (E4-T15 `Hecho`, 2026-10-13)

Suite de integracion/API sobre el app real
(`backend/tests/test_e4_t15_qr_payment_qa.py`, 11 pruebas en verde; SQLite en memoria +
`TestClient`, sin red, sin `float`), complementaria —sin solapar— a las suites de
modulo `E4-T11` (servicio/integridad) y `E4-T12` (endpoint/autorizacion). Verifica los
criterios de salida de HU15 (`docs/09#hu15...`) con foco en seguridad del QR y
concurrencia del pago:

- **CA-01 (lectura):** el `qr_content` creado por el servidor se decodifica y verifica
  contra la fila del cargo (comercio+monto del servidor, enteros) en < 2 s.
- **CA-02 (un toque):** drenaje exacto hasta 0 sin sobregiro (el siguiente pago se
  rechaza `REJECTED` sin negativo); doble escaneo con claves distintas -> un solo
  cobro (`CONFLICT`) + cuadre global `sum(debitos)=sum(creditos)` (`docs/04#7.4`).
- **CA-03 (biometria sobre limite):** pendiente sobre el base que nunca se autoriza ->
  sin liquidar (`ACTIVE`, sin eventos ni aviso); `finalize_qr_payment` con operacion no
  liquidada se niega; el re-chequeo usa `wallet_daily_limit_reader` (`docs/18` G75).
- **CA-04 (QR protegido):** moneda/vencimiento del sobre manipulados y firma truncada
  (variantes no cubiertas por `E4-T11`/`E4-T12`) -> `VALIDATION_ERROR` sin mover fondos
  ni marcar el cargo; cadena job `E4-T07` -> pago (el `EXPIRED` persistido rechaza, el
  `STATIC` sobrevive al job y cobra).
- **Abono E2E real (`docs/18` G72, cierre):** el pago real emite una sola invocacion
  `push` con `data={charge_ref, at}` (< 5 s, sin cifras/PII) y `POST /payments/qr`
  deja el cargo `PAID` + aviso unico (< 5 s).
- **Matiz del cierre diferido (fix guarda, 2026-10-13):** sobre el limite base el
  pago queda `PENDING_AUTHORIZATION` y el cierre E2E se completa cuando la **UI
  reintenta** `POST /payments/qr` con la misma `Idempotency-Key` tras autorizar
  (el endpoint finaliza via `finalize_qr_payment`; ver `docs/18` G72). Ese
  reintento de UI va en encargo aparte (fix de UI, no backend).
- **Seguridad (`#3`):** `qr_payments` no persiste `qr_content`/firma/secreto (esquema +
  blobs); respuestas/logs/eventos/aviso sin `qr_content`, firma, cuentas ni razon
  social.

### 1.7 QA de HU16 (E4-T20 `Hecho`, 2026-10-13)

Suite de integracion/API sobre el app real
(`backend/tests/test_e4_t20_services_qa.py`, 14 pruebas en verde; SQLite en memoria +
`TestClient`, sin red, sin `float`), complementaria —sin solapar— a las suites de
modulo `E4-T16` (catalogo/adaptador), `E4-T17` (pago/recarga) y `E4-T18`
(recordatorios). Verifica los criterios de salida de HU16 (`docs/09#hu16...`)
cruzando piezas (jornadas buscar -> suscribir -> pagar, recordatorio desde la
suscripcion creada por API, alcance de la idempotencia por titular, barridos
globales de PII/cuadre):

- **CA-01 (busqueda):** el catalogo (API, `q` por nombre) y el adaptador
  (`lookup_biller` por nombre/suministro) resuelven el mismo `biller`; consulta
  < 2 s en catalogo completo y filtros; `TELEFONIA` expone los operadores de
  recarga.
- **CA-02 (recarga):** la recarga por API deja `topups.PAID` con su
  `transaction_id`, delta exacto pagador/liquidacion, asiento cuadrado
  (`docs/04#7.10`) y un solo `topup.completed` sin PII.
- **CA-03 (recordatorio):** suscripcion creada por API -> fuera de ventana no
  hay aviso; en ventana exacta (`hoy+3`) un solo push `service_reminder`
  (`data={service_ref, due_on}`); repetir la pasada no duplica y al dia
  siguiente la misma suscripcion queda en silencio (unico por ventana).
- **CA-04 (comprobante):** estructura exacta en pago y recarga
  (`receipt_no` oficial `OP-…` = `build_receipt_no(transaction_id)`,
  `supply_masked`/`phone_masked` con cola visible, el otro en `None`);
  estable entre replays.
- **Seguridad/concurrencia:** sin fondos -> `REJECTED`/`INSUFFICIENT_FUNDS`
  por API sin tocar saldos, sin asientos ni eventos; la misma
  `Idempotency-Key` tiene alcance por titular (dos titulares, dos
  movimientos; cada replay devuelve lo suyo); suministro invalido ->
  `422` antes de cobrar sin filas/asientos/eventos; barrido de PII en
  respuestas/logs/`outbox`/avisos/envios de la jornada completa; dinero
  solo en centimos enteros (respuestas, filas, patas + escaneo estatico
  de `float(` en `wallet/service` y el job); cuadre global
  `sum(debitos)=sum(creditos)` tras jornada mixta; un solo
  `service.paid`/`topup.completed` por operacion aunque se reintente.
- **Hallazgo QA (frontera de extensibilidad, fail-closed):** la fila nueva de
  `billers` se lista sin migracion, pero el proveedor `mock` solo conoce los
  `billers` demo (`get_demo_biller`): suscribir/pagar el biller nuevo se
  rechaza con `422` antes de cobrar, sin efectos. Hacer pagable un biller
  nuevo exige ampliar el proveedor (`DEMO_BILLERS`/proveedor real), no solo
  insertar la fila (ver reporte de `E4-T20`).

### 1.8 QA de HU22 (E6-T16 `Hecho`, 2026-10-13)

Suite de integracion/API sobre el app real
(`backend/tests/test_e6_t16_notifications_qa.py`, 10 pruebas en verde; SQLite en memoria +
`TestClient`, sin red, sin `float`, reloj y espera inyectables), complementaria —sin
solapar— a las suites de modulo `E6-T12` (fachada/preferencias/centro), `E6-T13`
(adaptadores push/correo) y `E6-T14` ("no reconozco"/bloqueo). Verifica los criterios de
salida de HU22 (`docs/11#hu22...`):

- **CA-01 (entrega < 5 s):** camino feliz push + correo de respaldo `SENT` con
  `elapsed < window <= 5` (reloj `now` y `sleep_fn` inyectables, cero esperas);
  `provider_ref` distintos por canal y `email_backup=True`.
- **CA-02 (contenido):** `data` del push == `{operation, at, device, location}` y el
  correo suma `reference` (no PII, solo ahi para correlacionar el reporte); ambos cuerpos
  renderizados llevan dispositivo/hora/ubicacion sin llaves sin resolver; sin
  dispositivo/ubicacion se usa la etiqueta aproximada (`no disponible`), jamas
  coordenadas ni vacio; sin PII en `payload_json`.
- **CA-04 (multi-canal tolerante, viceversa):** caida dura del correo (caso no cubierto
  por `E6-T12`) deja correo `FAILED` con `error` y push `SENT`; `retry_notification`
  recupera el correo sin tocar el push.
- **Preferencias E2E (HTTP + fachada):** `PUT /preferences` con `push=false` -> solo el
  respaldo `email`; con `email=false` el respaldo se garantiza igual (decision del
  dueno); `sms=true` nunca abre canal en la ruta de seguridad.
- **CA-03 ("no reconozco", jornada completa):** notificar -> centro (`GET` lista la
  alerta) -> `POST .../report` (`replayed=False`, `reference` correlacionada) -> cadena
  D-04: un solo `security.operation_reported`, un solo `fraud.alert.raised`, severidad
  `+10` en `risk_profiles`, auditoria triple, TODAS las sesiones `revoked_at` (refresh
  viejo muere con `RefreshReuseError`, login nuevo funciona) y acuse
  `security_report_received` visible en el centro; D-16 intacto (sin `RiskAlert`).
- **Idempotencia posterior a la cadena:** segundo reporte tras la cadena ->
  `replayed=True` sin segundo evento, sin subir el riesgo ni nuevo acuse.
- **Seguridad (`#3`):** ajena e inexistente comparten el mismo `404` `NOT_FOUND` neutro
  (sin reflejar el id, sin eventos, sin auditoria, sin bloqueo colateral); barrido de PII
  en la jornada completa (respuestas del centro/lectura/reporte, `payload_json`,
  ambos eventos, auditoria, logs).

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
