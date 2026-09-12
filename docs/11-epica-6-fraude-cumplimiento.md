# 11 - Epica 6: Fraude y Cumplimiento (HU20-HU23)

**Modulos responsables:** `risk`, `notifications`, `audit`, con `transactions` y `identity`.
**Resultado de la epica:** se detectan y bloquean operaciones de riesgo, se cumple PLDFT/AML
(simulado), se notifica al cliente y se conserva evidencia.

## HU20 - Motor Antifraude por Reglas y Anomalias Biometricas (Must, 8 SP, Sprint 4)

**Logica**
1. Cada evento de autenticacion y cada transaccion alimenta el motor de reglas.
2. Reglas tipicas: suplantacion/spoofing facial, geolocalizacion atipica, monto inusual,
   velocidad de operaciones, dispositivo nuevo.
3. El motor calcula un puntaje; si es critico, **bloquea preventivamente** la operacion.
4. Las alertas se consolidan en un tablero para el analista de fraude, que puede resolver o
   liberar.

**Reglas**: alertas < 1 s; reglas configurables y actualizables sin reiniciar; tasa de falsos
positivos controlada; cada regla tiene prioridad y accion (`alertar`, `retener`, `bloquear`).
**CA**: CA-01 (spoofing), CA-02 (anomalia), CA-03 (bloqueo), CA-04 (tablero).

**Tareas**
- `E6-T01` Modelo de reglas (`risk_rules`) + parametros por regla. *Backend/Datos.*
- `E6-T02` Motor de evaluacion sincrono (pre-transaccion) y asincrono (post-evento). *Backend.*
- `E6-T03` Perfil de comportamiento (`risk_profiles`) alimentado por historial. *Backend.*
- `E6-T04` Creacion de `alerts` y bloqueo preventivo integrado al motor. *Backend.*
- `E6-T05` Tablero del analista de fraude (panel web). *Frontend.*
- `E6-T06` Pruebas de calibracion: reglas, falsos positivos y bloqueo. *QA.*

## HU21 - Cumplimiento PLDFT / AML y Listas Restrictivas (Must, 8 SP, Sprint 4)

**Logica**
1. En el onboarding se coteja al cliente contra listas (OFAC, World-Check, PEP) **simuladas**.
2. En cada transaccion se cotejan emisor y receptor en tiempo real.
3. Ante coincidencia relevante se genera un **ROS** para el oficial de cumplimiento.
4. Cada validacion se registra para auditoria (fecha, resultado, lista consultada).

**Reglas**: cotejo < 2 s; listas versionadas y actualizables; umbrales de similitud calibrados
para evitar bloquear operaciones legitimas sin revision.
**CA**: CA-01 (onboarding), CA-02 (transaccional), CA-03 (ROS), CA-04 (auditoria).

**Tareas**
- `E6-T07` Adaptador `SanctionsLists` (mock + interfaz real) con matching difuso. *Backend.*
- `E6-T08` Versionado y actualizacion de listas. *Datos.*
- `E6-T09` Cotejo en onboarding y en transaccion; persistir `screening_results`. *Backend.*
- `E6-T10` Generacion de ROS y bandeja del oficial de cumplimiento. *Backend/Frontend.*
- `E6-T11` Pruebas: coincidencia exacta, difusa, falso positivo, registro de auditoria. *QA.*

## HU22 - Notificaciones de Seguridad Multicanal (Should, 3 SP, Sprint 4)

**Logica**
1. Cada transaccion o login biometrico dispara una alerta push inmediata.
2. En paralelo se envia un correo con el detalle de respaldo.
3. La alerta incluye dispositivo, hora y ubicacion aproximada.
4. El cliente puede reportar "no reconozco esta operacion" desde la notificacion, lo que bloquea
   los canales digitales y escala a seguridad.

**Reglas**: entrega < 5 s; tolerancia a caida de un proveedor (multi-canal); el reporte escala al
modulo `risk`.
**CA**: CA-01 a CA-04.

**Tareas**
- `E6-T12` Servicio de notificaciones con plantillas y preferencias. *Backend.*
- `E6-T13` Adaptadores push/correo (mock) desacoplados por canal. *Backend.*
- `E6-T14` Accion "no reconozco" -> bloqueo + alerta a `risk`. *Backend.*
- `E6-T15` UI de alertas y centro de notificaciones (Flutter). *Frontend.*
- `E6-T16` Pruebas de entrega y de reaccion a "no reconozco". *QA.*

## HU23 - Bitacora de Auditoria Inalterable / Audit Log (Could, 5 SP, Sprint 4 - FUERA DEL MVP)

> Prioridad `Could`, no incluida en el MVP. Se especifica para una fase posterior; aun asi, el
> **registro de auditoria basico** (sin hash encadenado) es obligatorio desde el Sprint 1 para
> las demas historias.

**Logica (fase posterior)**
1. Todo cambio administrativo y verificacion facial se registra en un log **append-only**.
2. Cada registro incluye un **hash encadenado** al anterior (no repudio).
3. El auditor filtra por fecha, usuario o tipo de evento.
4. Ningun usuario, ni el superadministrador, puede modificar o borrar registros.

**CA**: CA-01 (append-only), CA-02 (hash), CA-03 (consulta filtrada), CA-04 (inmutabilidad).

**Tareas (post-MVP)**
- `E6-T17` Tabla `audit_log` con `prev_hash`/`hash` y permisos de solo insercion. *Datos.*
- `E6-T18` Verificador de cadena y alerta ante manipulacion. *Seguridad.*
- `E6-T19` Pantalla de consulta para auditor. *Frontend.*
- `E6-T20` Pruebas de inmutabilidad. *QA.*

## Criterios de salida de la epica

- Operaciones de riesgo se bloquean y quedan con alerta trazable.
- El onboarding y las transacciones pasan por cotejo de listas (simulado) y se auditan.
- El cliente recibe notificacion por cada movimiento sensible.
- Aunque HU23 no este en el MVP, todo evento critico ya se registra en auditoria.
