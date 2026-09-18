# HANDOFF — Orquestador Sprint 1 → Sprint 2 (2026-09-18)

Lee esto antes de continuar. Estado real del proyecto al cerrar la sesión.

## 1. Dónde estamos

- Rama: `pruebas-flutter-nuevoagente` (checkpoint completo; treas fases 2-6 + fixes).
- Rama anterior con PR pendiente: `feature/fase2-fase3-motor-ledger`
  (https://github.com/JheampierreRaPe/banco/pull/new/feature/fase2-fase3-motor-ledger).
- Backend: suite **350 passed + 1 skipped**, migraciones `0001→0015` lineales,
  `alembic check` limpio, BD local en head. Frontend: **150 tests**, `analyze` limpio.
- Regla vigente del dueño: los workers implementan; el orquestador JAMÁS corrige
  defectos de workers (deriva a fixer y re-verifica). Solo integración declarada
  (router, wiring) la hace el orquestador. Flujo se detiene ante hallazgos del
  validador hasta que el dueño decida. Preguntar antes de commitear (esta vez el
  dueño pidió checkpoint explícito en esta rama).

## 2. Qué funciona de verdad (verificado hoy)

- KYC con microservicio REAL del dueño (rama `eliminar`,
  https://github.com/JheampierreRaPe/validacion-id-liveness-selfie):
  contenedor `kyc-facial-service-8001` (:8001), backend Docker con
  `KYC_PROVIDER=http` → challenge real + submit sin cara rechazado 422.
  Clon permanente en `kyc-service/` (NO commiteado: contiene `.env` con API key;
  re-clonable de GitHub). Arranque: `docker compose up -d` + `docker network
  connect banca-demo_default kyc-facial-service-8001` + rebuild backend si cambia código.
- PIN: `POST /auth/pin/setup` + pantalla en app (activate → pin-setup → login).
- Cámara real + preview en vivo + fix anti-spinner-infinito (timeout 15 s).
- Twilio cableado (mock por defecto); SMS real BLOQUEADO por trial (error 572006).
  Decisión del dueño: OTP real por **Gmail pasa al Sprint 2** (ver `docs/17#6 P-S2-01`).

## 3. Problema ABIERTO (prioridad 1 mañana): cámara sigue en "Iniciando…"

Síntoma en físico con el último APK (`frontend/build/.../app-debug.apk`, 18/09 01:57,
construido DESPUÉS del fix): spinner infinito. El fix garantiza salida a los 15 s,
luego o el APK instalado es viejo (desinstalar y reinstalar) o el cuelgue está
antes del preview. Diagnóstico obligatorio con el celular por USB:
`flutter run --dart-define=API_BASE_URL=http://<IP-LAN>:8000` y leer el log — ahí
sale la excepción exacta (sospechosos: permiso, `availableCameras()` en ese modelo,
`initialize()` colgado). NO adivinar más a ciegas.

## 4. Deudas registradas (no reabrir sin motivo)

- R21 (`docs/17`): filtros de movimientos en memoria (aceptado, alcance académico).
- JWT local `change-me` 9 bytes (rotar en prod). Redis/argon2/cryptography/`local_auth`/
  `openpyxl`/`file_saver` pendientes de agregar cuando se necesiten.
- `facial-kyc-service_deepface_models` (volumen huérfano, GBs): borrar con
  `docker volume rm` cuando se confirme que no se usa.
- `kyc-service/` local sin commitear (a propósito, tiene secretos).

## 5. Siguientes sprints (después de resolver el APK)

- **Sprint 2 (transferencias HU06 + OTP Gmail P-S2-01):** briefs `E2-T07..` y crear
  `E1-T24`/`F-T19` según `GUIA-GENERAR-BRIEFS.md` (ver `docs/17#6`).
  Precondición: APK validado en físico (sección 3).
- **Fase 7 QA** (`E1-T07/T12/T18, E2-T06, E5-T07/T08/T15, Q-T02/T04`): suite motor,
  carga, contratos OpenAPI.
- **Frontend bonito** solo cuando la lógica esté validada (decisión del dueño).

## 6. Comandos de verificación rápida

```powershell
cd backend; .venv\Scripts\python.exe -m pytest tests/ -q        # 350+1
cd frontend; flutter test                                        # 150
docker ps --format "{{.Names}} {{.Status}}"                      # 6 Up
curl -X POST http://localhost:8000/api/v1/auth/kyc/challenge -H "Content-Type: application/json" -d "{}"  # token 32 + 5 pasos ES = real
```
APK: `frontend/build/app/outputs/flutter-apk/app-debug.apk` (IP `192.168.1.42`; si la
IP LAN cambió, reconstruir con la nueva). Backend para el celular:
`uvicorn app.main:app --host 0.0.0.0` o el contenedor Docker (mismo Wi-Fi).
