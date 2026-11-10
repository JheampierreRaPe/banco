# Iniciar agente - guia rapida para ejecutar una tarea del sprint

Este documento explica que levantar y como pasarle una tarea a un agente constructor.
El agente trabaja **una tarea a la vez** a partir de su brief en `docs/tasks/<ID>.md`.

---

## 1. Levantar el entorno

El proyecto corre en **dos campos**: el **PC** (servidor) y el **movil** (cliente). Detalle en
`docs/19-ejecucion-dos-campos.md`.

### 1.1 Servidor (PC) con Docker (recomendado)

```powershell
cd "C:\Users\Jheampierre\Desktop\proyecto integrador"
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
# API:   http://localhost:8000/docs
# Panel: http://localhost:8080
```

### 1.2 Servidor (PC) en modo desarrollo (sin contenedores)

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"      # solo si cambio pyproject
alembic upgrade head
pytest -q
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Comprobaciones rapidas:

```powershell
# Health
(Invoke-WebRequest http://localhost:8000/health).Content
# Base (si usas el venv local)
docker exec banca-postgres pg_isready -U banca
```

> Si Docker no responde, abre Docker Desktop y espera el motor.

### 1.3 Cliente (movil) contra el PC por LAN

```powershell
ipconfig                       # obtener la IP del PC
cd frontend
flutter pub get
flutter run --dart-define=API_BASE_URL=http://<IP-del-PC>:8000
```

- Emulador Android: usar `http://10.0.2.2:8000`.
- Permitir el puerto 8000 en el firewall de Windows (red privada).

---

## 2. Elegir la tarea

Mira el orden en `docs/tasks/SPRINT-1.md`. **Hecho:** `INIT-T01`, `INIT-T02`, `Q-T01`, `Q-T05`,
`Q-T06`. Fase actual:

1. `E5-T01`, `E5-T09`, `E5-T10`, `E5-T11` (motor y ledger puros).
2. `E5-T02`, `E5-T12`, `E5-T13`, `E5-T03`, `E5-T04`, `E5-T05`, `E5-T06`.
3. `E2-T01`..`E2-T04`, luego identidad, frontend y QA.

Toma el ID de la siguiente tarea pendiente.

---

## 3. Crear la rama

```powershell
git checkout main
git pull
git checkout -b feature/<tema>     # la rama sugerida esta dentro del brief
```

---

## 4. Pasarle la tarea al agente

Pegale **exactamente** este prompt (reemplaza `<ID>`):

```
Implementa la tarea <ID>. Lee docs/tasks/<ID>.md y sigue su "Context pack".
No leas el resto del repo. Respeta docs/16-guia-para-agentes.md (reglas de oro y DoD).
Al terminar reporta con el formato de docs/tasks/README.md sección 6.
```

Reglas del handoff:

- Se le pasa **solo el brief**. El brief ya indica que otros archivos leer (su "Context pack").
- No le pegues toda la documentacion ni le digas que explore el repositorio.
- Si el agente no tiene acceso al repositorio (chat sin archivos), entonces pega tambien las
  secciones que lista el "Context pack" del brief.

---

## 5. Verificar lo que hizo

```powershell
cd backend

ruff check . --fix
black .
pytest -q

# Si la tarea creo tablas:
alembic upgrade head
alembic current
```

Checklist de aceptacion (resumen de `docs/16` seccion 5):

- [ ] Cumple los CA del brief.
- [ ] Cubre camino feliz y de error con pruebas.
- [ ] Respeta la matriz de accesos y el enmascaramiento.
- [ ] Si mueve dinero: deja asiento contable y cuadra.
- [ ] Es idempotente si mueve dinero.
- [ ] Reglas configurables en `config.parameters` (no cableadas).
- [ ] OpenAPI actualizado; sin secretos ni PII en logs.
- [ ] `ruff`, `black` y `pytest` verdes.

---

## 6. Cerrar la tarea

1. Marca el `Estado` del brief como `Hecho` en `docs/tasks/<ID>.md`.
2. Actualiza el `README.md` del modulo si cambio su frontera.
3. Commit y PR:

```powershell
git add .
git commit -m "feat(<modulo>): <ID> <descripcion corta>"
git push -u origin feature/<tema>
```

4. Revisa el reporte del agente (que CA cubrio y con que evidencia) antes de hacer merge.

---

## 7. Comandos de referencia

| Accion | Comando |
|---|---|
| Levantar todo (PC) | `docker compose up --build -d` |
| Apagar todo | `docker compose down` |
| Ver servicios | `docker compose ps` |
| DB suelta (modo dev) | `docker start banca-postgres` |
| Activar venv | `.\.venv\Scripts\Activate.ps1` |
| Migrar | `alembic upgrade head` |
| Revertir migracion | `alembic downgrade -1` |
| Pruebas | `pytest -q` |
| Lint / formato | `ruff check . --fix` / `black .` |
| Correr API (LAN) | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload` |
| Frontend | `cd frontend` -> `flutter run --dart-define=API_BASE_URL=http://<IP-PC>:8000` |

---

## 8. Problemas comunes

| Sintoma | Solucion |
|---|---|
| `docker` no conecta | Abre Docker Desktop y espera el motor. |
| `alembic` no encuentra `app` | Ejecuta desde `backend/` con el venv activo. |
| `psycopg` no instala en Python 3.14 | Usa el venv de Python 3.11 que ya esta en `backend/.venv`. |
| El agente se pierde leyendo todo | Recuerdale: "solo tu brief y su Context pack". |
| Migracion falla | Revisa `docs/03b` (tipos y schemas) y que el schema exista (`0001`). |
