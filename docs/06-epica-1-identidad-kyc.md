# 06 - Epica 1: Identidad, Biometria y KYC (HU01-HU04)

**Modulo responsable:** `identity`.
**Dependencias externas:** microservicio KYC (real), `notifications` (OTP), `risk`, `audit`.
**Resultado de la epica:** un cliente se registra validando identidad, activa su cuenta por OTP,
inicia sesion con biometria/PIN y puede recuperar el acceso.

> Antes de empezar, leer `04-motor-transaccional-y-ledger.md` (aunque esta epica casi no mueve
> dinero, la apertura de cuenta crea cuentas contables) y `16-guia-para-agentes.md`.

## HU01 - Registro y KYC Biometrico (Must, 8 SP, Sprint 1)

**Logica**
1. Cliente inicia registro indicando tipo y numero de documento (preliminar).
2. El backend pide al microservicio KYC un **desafio de liveness** (token + pasos aleatorios).
3. La app muestra cada tarea ("mueve la cabeza arriba", "parpadea"...), captura 8-10 frames y
   el backend valida **una tarea por vez**; la siguiente se habilita solo con `passed: true`.
4. Al completar las tareas, la app envia la foto del documento + los segmentos validados; el
   backend llama a `verify-full` y recibe `overall_result`.
5. Si `overall_result = true`: se crea `users` (estado `PENDING_ACTIVATION`), `credentials`,
   la cuenta digital y su cuenta contable; se emite `kyc.completed`.
6. Si falla: se informa el motivo, se permiten reintentos limitados y, si persiste, se deriva a
   revision manual.

**Reglas configurables**: precision OCR minima, umbral de match facial, maximo de reintentos,
TTL del desafio, tolerancia de documentos.
**Guardar**: solo el resultado (`kyc_verifications`), nunca los frames crudos.
**CA**: CA-01 (OCR >=95%), CA-02 (liveness), CA-03 (match), CA-04 (manejo de excepcion).

**Tareas**
- `E1-T01` Adaptador `KycProvider` (interfaz + implementacion real + mock) con reintentos y
  circuit breaker. *Backend.*
- `E1-T02` Endpoint proxy `/auth/kyc/challenge` y `/auth/kyc/submit`; nunca exponer la API key
  del KYC al cliente. *Backend.*
- `E1-T03` Caso de uso "alta de cliente": crear usuario, credenciales, cuenta y cuenta contable
  en una sola transaccion; emitir `kyc.completed`. *Backend/Datos.*
- `E1-T04` Persistir `kyc_verifications` y registrar en `audit`. *Datos.*
- `E1-T05` Pantalla Flutter de captura de documento + guia de liveness por pasos (usar
  `INTEGRACION_FLUTTER.md` del microservicio). *Frontend.*
- `E1-T06` Manejo de reintentos y mensajes de error por motivo. *Frontend.*
- `E1-T07` Pruebas: exito, liveness fallido, match fallido, servicio KYC caido. *QA.*

## HU02 - Verificacion y Activacion de Cuenta (Must, 3 SP, Sprint 1)

**Logica**
1. Al completar KYC, se genera un **OTP** de un solo uso (hash, no en claro) y se envia por
   correo/SMS via adaptador de notificaciones.
2. El cliente lo ingresa; se valida que coincida y no haya expirado (10 min).
3. Al validar, `users.status = ACTIVE`; se emite `user.activated` (crea/activa cuenta).
4. Se permite reenviar hasta 3 veces con espera minima entre envios.

**Reglas**: OTP no reutilizable; SLA de entrega < 5 s; expira a los 10 min.
**CA**: CA-01 (envio), CA-02 (validacion 10 min), CA-03 (reenvio), CA-04 (acceso inicial).

**Tareas**
- `E1-T08` Servicio de OTP: generacion, hash, expiracion, intentos. *Backend.*
- `E1-T09` Adaptador de notificaciones (correo/SMS) con mock y registro de entrega. *Backend.*
- `E1-T10` Endpoint `/auth/activate` y `/auth/otp/resend`. *Backend.*
- `E1-T11` Pantalla de ingreso de codigo con contador y reenvio. *Frontend.*
- `E1-T12` Pruebas: OTP correcto, expirado, reenvio, limite de intentos. *QA.*

## HU03 - Autenticacion BioFacial y Multi-factor (Must, 5 SP, Sprint 1)

> El liveness del servidor **no** se usa aqui (decision D07): la validacion es local al
> dispositivo; el servidor solo verifica el `nonce` firmado con la clave del `device_binding`.

**Logica**
1. Login principal con **biometria del dispositivo**: el servidor emite un `nonce`; la app
   exige Face ID/huella del telefono y firma el `nonce` con una clave guardada en
   almacenamiento seguro; el servidor valida la firma y abre sesion.
2. Alternativa: **PIN** (hash) cuando la biometria falla.
3. Tras 5 intentos fallidos consecutivos: bloqueo temporal + notificacion.
4. Inactividad configurable (p. ej. 3 min): cierre de sesion automatico.

**Reglas**: validacion < 1.5 s; disponibilidad 99.9% (objetivo de diseno); sesiones revocables.
**CA**: CA-01 (login <1.5 s), CA-02 (contingencia PIN), CA-03 (bloqueo), CA-04 (inactividad).

**Tareas**
- `E1-T13` Emision de `nonce` + verificacion de firma de dispositivo + JWT/refresh. *Backend.*
- `E1-T14` Login con PIN, contador de intentos y bloqueo temporal. *Backend.*
- `E1-T15` Gestion de sesiones (`sessions`) y revocacion. *Backend.*
- `E1-T16` Flujo Flutter de biometria local + PIN de contingencia + timer de inactividad.
  *Frontend.*
- `E1-T17` Registro en `audit` de cada login y de cada intento fallido. *Datos/QA.*
- `E1-T18` Pruebas de fuerza bruta, bloqueo y expiracion de sesion. *QA.*

## HU04 - Recuperacion de Cuenta con Biometria (Should, 5 SP, Sprint 1)

**Logica**
1. Desde el login, el cliente solicita recuperar acceso solo con su documento (sin clave).
2. Se re-valida al cliente con la **biometria del dispositivo registrado** (`device_bindings`)
   mas un **OTP** enviado al canal registrado (doble evidencia). **No se usa el liveness del
   servidor.**
3. Solo si ambas evidencias son validas, se permite definir nuevo PIN/contrasena.
4. Se notifica por canal distinto, se revocan sesiones previas y se audita el evento.

**CA**: CA-01 (inicio sin clave), CA-02 (re-validacion), CA-03 (nuevas credenciales), CA-04
(notificacion y revocacion).

**Tareas**
- `E1-T19` Flujo `/auth/recover` (dispositivo confiable con `nonce` firmado + OTP + cambio de
  credencial). *Backend.*
- `E1-T20` Politica de recuperacion (registrar dispositivo confiable; OTP obligatorio).
  *Backend/Datos.*
- `E1-T21` Notificacion multicanal + revocacion de sesiones. *Backend.*
- `E1-T22` Pantalla Flutter de recuperacion. *Frontend.*
- `E1-T23` Pruebas: recuperacion exitosa, biometria fallida, revocacion efectiva. *QA.*

## Criterios de salida de la epica

- Un usuario nuevo completa KYC -> OTP -> activacion -> primer login, con datos persistidos.
- No se almacena ningun frame biometrico.
- Todos los intentos y resultados quedan en auditoria.
- La API key del microservicio KYC nunca llega al dispositivo.
