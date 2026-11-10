# 19 - Ejecucion en dos campos (cliente delgado / servidor central)

> Formaliza como se ejecuta el proyecto: la **logica y la resolucion de problemas** viven en un
> **PC/servidor**; los **clientes** (app Flutter y panel web) solo capturan, solicitan y muestran.

## 1. Principio

El proyecto se ejecuta en **dos campos**:

- **Campo servidor (PC)**: concentra toda la logica de negocio, validaciones, contabilidad,
  reglas de riesgo, integracion con el microservicio KYC y persistencia.
- **Campo cliente (movil / navegador)**: accede a la informacion, envia solicitudes y muestra
  respuestas. **Nunca decide.**

Regla clave: **el cliente nunca decide**. No calcula saldos, cuotas, limites ni comisiones; no
valida identidad ni liveness; no aplica reglas de riesgo; no crea asientos. Envia datos crudos y
recibe decisiones del servidor.

## 2. Topologia

| Nodo | Donde corre | Que ejecuta | Puerto |
|---|---|---|---|
| **PC / servidor** | Este equipo | `backend` (API FastAPI `/api/v1`) | 8000 |
| | | `postgres` (PostgreSQL 16) | 5432 |
| | | `redis` (cache/idempotencia) | 6379 |
| | | `worker` (outbox, holds, cierres, notificaciones) | interno 8100 |
| | | `admin-web` (panel interno, en navegador) | 8080 |
| | | `kyc-service` (microservicio KYC, perfil opcional) | 9000 |
| **Movil / cliente** | Telefono del usuario | App Flutter (`frontend/`) | - |
| **Navegador / cliente** | Navegador del equipo interno | Panel web (`admin-web/`) | 8080 |

El entorno del servidor se levanta con `docker-compose.yml` (Q-T06). El microservicio KYC se activa
con el perfil `kyc` cuando este disponible la imagen.

## 3. Reparto de responsabilidades

| Actividad | Cliente (movil / panel) | PC / servidor |
|---|---|---|
| Capturar documento y frames de KYC | Si (solo captura y sube) | No |
| OCR, liveness y match facial | No | Si (via microservicio KYC) |
| Decidir aprobacion de KYC | No | Si |
| Mostrar saldos y movimientos | Si (lo que responde el servidor) | Si (calcula desde el ledger) |
| Validar saldo, limites, riesgo | No | Si |
| Contabilizar (partida doble) | No | Si |
| Guardar tokens/clave de dispositivo | Si (`flutter_secure_storage`) | No |
| Biometria del dispositivo | Si (gate local con `local_auth`) | Verifica el `nonce` firmado |
| Aplicar reglas de negocio | No | Si (`config.parameters`) |
| Emitir comprobantes | Muestra/descarga | Genera |
| Auditoria | No | Si (`audit`) |

## 4. Cliente delgado estricto

- **Sin logica offline**: sin conexion al servidor **no se opera** (no hay saldos ni operaciones
  locales). La unica funcion local es la biometria del dispositivo como *gate* y el almacenamiento
  seguro de tokens.
- El cliente **envia datos crudos** (imagenes, segmentos, parametros como monto/cuenta destino) y
  **recibe decisiones** (aprobado/rechazado, estado de la operacion, saldo resultante).
- Toda accion que mueve dinero incluye `Idempotency-Key` generada en el cliente, pero la
  idempotencia y la validacion las aplica el servidor.

## 5. Secuencias (ejemplos del Sprint 1)

### 5.1 KYC (HU01)

```
Movil                                    PC / servidor
  |  POST /auth/kyc/challenge  ---------->  backend pide desafio al microservicio KYC
  |  <---------- {token, steps}             |
  |  captura frames por tarea             |
  |  POST /auth/kyc/evaluate  ---------->  backend valida la tarea (MediaPipe, en el PC)
  |  <---------- {passed}                  |
  |  POST /auth/kyc/submit (doc + segs) ->  OCR + liveness + match (DeepFace, en el PC)
  |  <---------- {overall_result}          |  crea usuario/cuenta; decide aprobado/rechazado
```

El movil nunca evalua la imagen: solo la sube. Toda la validacion ocurre en el PC.

### 5.2 Transferencia (HU06/HU17)

```
Movil                                    PC / servidor
  |  POST /transfers/third-party ------>  valida saldo, limites y riesgo
  |    {origen, destino, monto,           retiene fondos (hold)
  |     Idempotency-Key}                  registra asiento (partida doble)
  |  <---------- {transaction_id, estado} actualiza proyecciones de saldo
```

El movil solo elige que transaccion hacer; el PC valida y contabiliza.

## 6. Red local (LAN)

- El backend se publica en el host en `${BACKEND_PORT}` (8000) via `docker-compose`.
- Desde un **telefono fisico**: `http://<IP-del-PC>:8000` (obtener la IP con `ipconfig`).
- Desde el **emulador Android**: `http://10.0.2.2:8000`.
- Abrir el puerto 8000 en el firewall de Windows para la red privada.
- Para desarrollar sin Docker: `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- Referencia del microservicio KYC: `RED_LOCAL.md` (repo `validacion-id-liveness-selfie`).

## 7. Como levantar los dos campos

**Servidor (PC):**

```powershell
cd "C:\Users\Jheampierre\Desktop\proyecto integrador"
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
# API:   http://localhost:8000/docs
# Panel: http://localhost:8080
```

**Cliente (movil):**

```powershell
cd frontend
flutter pub get
flutter run --dart-define=API_BASE_URL=http://<IP-del-PC>:8000
```

**Modo desarrollo (sin contenedores):**

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## 8. Que NO hace el cliente

- No calcula saldos, cuotas, intereses, comisiones ni limites.
- No valida identidad, liveness, firma ni reglas de riesgo.
- No decide aprobaciones (KYC, credito, fraude).
- No contabiliza ni mantiene un ledger local.
- No guarda PII sensible salvo lo estrictamente necesario y cifrado.
- No opera sin conexion al servidor.

## 9. Seguridad

- La **API key del microservicio KYC** vive solo en el backend (nunca en el movil).
- Tokens y clave de dispositivo en almacenamiento seguro del movil.
- En produccion: TLS en transito (el trafico LAN de la demo puede ser HTTP, documentado como
  limitacion academica).
- CORS restringido a los origenes del panel y la app.
