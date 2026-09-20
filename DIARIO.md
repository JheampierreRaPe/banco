# DIARIO - Lecciones aprendidas (Banca Online Integral)

> Documento vivo de sintomas, causa raiz, solucion y regla para no repetir errores.
> Registrar aqui cada incidente relevante (bug dificil, falso positivo, diagnostico
> fallido) para que el siguiente agente no repita el camino.

## Como usarlo

- Cada incidente es UNA entrada con fecha en su titulo: `## <fecha> - <titulo>`.
- Las entradas se agregan **al final**, en orden cronologico. **No se reescribe historia**:
  si algo cambia, se agrega una entrada nueva que referencia la anterior.
- Campos por entrada: Contexto, Sintoma, Causa raiz, Solucion, Evidencia, Leccion/regla, Referencias.
- Sin PII, sin frames biometricos, sin secretos en el contenido.

---

## 2026-09-20 - Viewfinder del liveness en NEGRO (camara frontal no pinta imagen)

**Contexto**
- Feature `kyc` de Flutter (HU01). Flujo: paso 1 captura del documento con camara TRASERA
  (`kyc_document_page.dart`), luego `context.push('/kyc/task')` -> paso 2 challenge de
  liveness con camara FRONTAL (`kyc_task_page.dart`).
- Idea central: un unico `CameraController` compartido por lente en `CameraFrameSource`.

**Sintoma**
- En el challenge de liveness, la camara frontal NO mostraba imagen (viewfinder negro),
  aunque la captura (rafaga) y la validacion seguian funcionando.
- El preview del DOCUMENTO (camara trasera) SI se veia correctamente.

**Causa raiz**
- En `CameraFrameSource._open` el orden era abrir-la-nueva-antes-de-cerrar-la-anterior: se
  publicaba el controller nuevo (`_controller = controller`) y DESPUES se retiraba el viejo
  (`_retire(previous)`).
- Al cambiar de lente (trasera del documento -> frontal del liveness) coexistian DOS sesiones
  de camara simultaneas. La mayoria de telefonos (gama media) NO soporta camara concurrente:
  la segunda sesion bindea muerta, `initialize()` resuelve, el estado queda `ready`, el
  `CameraPreview` se monta, pero nunca llegan frames -> viewfinder NEGRO.
- Aclaracion: la rotacion (`handlesCropAndRotation=false`, sensor 270) es un tema DISTINTO;
  no era la causa del negro.

**Diagnostico (varios intentos, ninguno resolvio el defecto)**
- `F-T28` (auto-religado del preview por generacion): logro que la camara abriera, pero
  introdujo un bucle de rebuilds/churn y no elimino el negro.
- `F-T31` ataco rotacion/rebuild (clave estable, `RepaintBoundary`, `Center` de la frontal,
  `handlesCropAndRotation=false`, sensor 270) y NO resolvio el defecto; sus CA quedaron sin
  efecto porque atacaban el churn del re-ligado, no la concurrencia de sesiones.
- El defecto solo aparecia al abrir la SEGUNDA lente; el paso 1 (una sola sesion) siempre
  funcionaba. Eso apuntaba a concurrencia, no a render/rotacion.

**Solucion (F-T32)**
- Fix real **close-before-open** en `frontend/lib/features/kyc/camera_frame_source.dart`:
  al cambiar de lente, con `_activeCaptures == 0`, se cierra (`await _close(previous)`) el
  controller anterior ANTES de crear/`initialize()` el nuevo. Si hay una captura en curso
  (`_activeCaptures > 0`), la apertura espera a drenar sin solapar.
- Se conservan `_openChain`, `_activeCaptures`, `_closed`, `_retired`, `_drainRetired` y los
  presets por lente, y las garantias de F-T24 (no cerrar un controller con captura en curso,
  sin used-after-dispose).
- Se revirtio el andamiaje inefectivo de `F-T28`/`F-T31` (listener de generacion, `_binding`,
  `ValueKey`, `RepaintBoundary`, `Center`) para dejar el codigo MINIMO.

**Evidencia**
- Logcat pre-fix: `OPENING -> CLOSING -> CLOSED`, `CameraDevice error code 3` (segunda sesion
  bindea muerta por concurrencia).
- Post-fix: en dispositivo fisico el viewfinder frontal muestra imagen en vivo; logcat con
  `CameraDevice error` = 0.
- Automatizadas: tests de orden `close-before-open` y de "sin dos `initialize()` activos a la
  vez"; `flutter analyze` sin issues y `flutter test` verde.
- Commit: `947d570`.

**Leccion/regla**
- NUNCA tener dos sesiones de camara concurrentes: en un cambio de lente, CERRAR antes de ABRIR.
- Verificar SIEMPRE en dispositivo fisico la camara real (no es testeable en CI ni con dobles).
- Ante un defecto asi, DOCUMENTAR el diagnostico (logcat + causa raiz) ANTES de codificar; no
  encadenar intentos a ciegas.
- No dejar sondas/`debugPrint` ni codigo de intentos fallidos: si un intento no resuelve el
  defecto, revertirlo y dejar el codigo minimo.

**Referencias**
- Fix: `docs/tasks/F-T32.md`
- Intentos: `docs/tasks/F-T31.md`, `docs/tasks/F-T28.md`
- Codigo: `frontend/lib/features/kyc/camera_frame_source.dart`
- Commit: `947d570`

---

## 2026-09-20 - ABIERTO: validacion del challenge reporta "el rostro no se mantuvo consistente" (face_match)

**Contexto**
- Validacion del challenge de liveness contra el microservicio KYC real.

**Sintoma**
- La validacion reporta "el rostro no se mantuvo consistente" en el paso `face_match`.

**Causa raiz**
- **SIN DETERMINAR.** No inventar causas; queda abierto para que el dueno lo valide mas
  adelante (posible linea a explorar: `best_frame`/frontalidad y umbrales del microservicio,
  ver `PENDIENTES.md` P5 y `docs/17#6`).

**Solucion**
- Pendiente.

**Evidencia**
- Reporte de validacion del challenge (mensaje `face_match`).

**Leccion/regla**
- Registrar el sintoma tal como se observa y no fijar causa raiz hasta tener evidencia.

**Referencias**
- `PENDIENTES.md` (item abierto - a validar por el dueno)
- `docs/17-riesgos-y-decisiones-abiertas.md#6` (P5 / P-S2-01)
