# 13 - Frontend: App Flutter y Panel Web

## 1. App Flutter (cliente y comercio)

### 1.1 Stack y decisiones

| Tema | Decision |
|---|---|
| Lenguaje | Dart / Flutter (Android + iOS). |
| Estado | Riverpod (o Bloc); estado del servidor con cache. |
| Navegacion | `go_router` con guardas por sesion/rol. |
| HTTP | `dio` con interceptores (token, `X-Request-Id`, idempotencia). |
| Almacenamiento seguro | `flutter_secure_storage` (clave de dispositivo, tokens). |
| Biometria | `local_auth` (Face ID/huella del telefono). |
| Camara | `camera` para KYC y lectura de QR. |
| PDF | `pdf`/`printing` para estados y comprobantes. |
| QR | `qr_flutter` (generar) + `mobile_scanner` (leer). |

### 1.2 Reglas de frontend

- El dispositivo **no** ejecuta modelos de IA; solo captura y envia (ver
  `INTEGRACION_FLUTTER.md` del microservicio KYC).
- Nunca se guardan frames biometricos en disco.
- La API key de servicios nunca se embebe en la app.
- Toda accion que mueve dinero usa `Idempotency-Key` generada en el cliente.
- Los datos sensibles se muestran enmascarados; el detalle exige autenticacion.

### 1.3 Pantallas por modulo

| Modulo | Pantallas |
|---|---|
| Onboarding | Bienvenida, captura de documento, guia de liveness por pasos, confirmacion de datos, OTP, creacion de PIN. |
| Sesion | Login biometrico, login PIN, recuperar acceso, cuenta bloqueada, cerrar sesion. |
| Inicio | Dashboard de cuentas, detalle de cuenta, movimientos, filtros, exportar. |
| Transferencias | Seleccion de origen/destino, monto/concepto, agenda de beneficiarios, confirmacion, autorizacion biometrica, comprobante. |
| Creditos | Simulador, solicitud/consentimiento, estado del expediente, contrato, firma biometrica, cronograma, pago de cuota. |
| Billetera/QR | Billetera, vinculos, escaner QR, resumen y pago, generar QR (comercio), cobros recibidos, servicios, recargas, comprobantes. |
| Divisas/Ahorro | Cotizacion, confirmacion con temporizador, bolsillos y metas. |
| Seguridad | Centro de notificaciones, "no reconozco esta operacion", dispositivos/sesiones. |
| Perfil | Datos, preferencias de canal, parametros de seguridad. |

### 1.4 Flujo KYC en la app (resumen operativo)

1. `POST /auth/kyc/challenge` (backend) -> pasos aleatorios.
2. Por cada paso: mostrar instruccion, capturar 8-10 frames, `POST /auth/kyc/evaluate`.
3. Si `passed:false`, reintentar la misma tarea; si `true`, avanzar.
4. Al terminar, enviar documento + segmentos a `POST /auth/kyc/submit`.
5. Mostrar resultado; si falla, motivo y reintento/revision.

### 1.5 Tareas

- `F-T01` Estructura del proyecto, tema, navegacion con guardas y capa HTTP. *Frontend.*
- `F-T02` Almacenamiento seguro y gestion de tokens/sesion. *Frontend.*
- `F-T03` Modulo de biometria local y nonce firmado. *Frontend.*
- `F-T04` Flujo KYC completo con camara y liveness guiado. *Frontend.*
- `F-T05` Dashboard, cuentas, movimientos y export. *Frontend.*
- `F-T06` Transferencias y agenda de beneficiarios. *Frontend.*
- `F-T07` Modulo de creditos. *Frontend.*
- `F-T08` Billetera, QR y servicios. *Frontend.*
- `F-T09` Divisas y bolsillos. *Frontend.*
- `F-T10` Centro de notificaciones y reporte de fraude. *Frontend.*
- `F-T11` Manejo de errores, estados vacios/carga y accesibilidad. *Frontend/QA.*

## 2. Panel Web Interno

### 2.1 Stack

Vite + React + TypeScript; consultas con TanStack Query; componentes MUI/Ant Design; graficos
con ECharts; despliegue estatico servido por el backend o Nginx. Alternativa: Flutter Web si el
equipo prefiere un solo lenguaje (menos adecuado para grillas densas).

### 2.2 Vistas por rol (matriz de accesos)

| Rol | Vistas |
|---|---|
| Analista de operaciones | Corridas de conciliacion, excepciones, reclamos, ajustes. |
| Analista de fraude | Tablero de alertas, detalle, resolver/liberar/bloquear. |
| Oficial de cumplimiento | Cotejos, casos, generacion de ROS, listas. |
| Analista de credito | Solicitudes asignadas, evaluacion, dictamen. |
| Administrador de seguridad | Usuarios, roles, permisos, parametros. |
| Auditor | Consulta de auditoria y ledger, **solo lectura**. |

### 2.3 Reglas del panel

- Todas las acciones quedan auditadas con usuario, hora y antes/despues.
- Los datos personales y cuentas se muestran enmascarados salvo permiso.
- Ninguna vista permite editar asientos contables; los ajustes son asientos nuevos autorizados.
- El panel no comparte tokens con la app movil; sus sesiones son separadas.

### 2.4 Tareas

- `F-T12` Base del panel, autenticacion y RBAC por rol. *Frontend/Backend.*
- `F-T13` Pantallas de operaciones/conciliacion. *Frontend.*
- `F-T14` Pantallas de fraude. *Frontend.*
- `F-T15` Pantallas de cumplimiento. *Frontend.*
- `F-T16` Pantallas de credito. *Frontend.*
- `F-T17` Administracion de parametros, usuarios y roles. *Frontend.*
- `F-T18` Vista de auditoria y ledger (solo lectura). *Frontend.*

## 3. Criterios de salida

- La app cubre el ciclo completo del cliente sin salir a otras herramientas.
- El panel cubre las tareas de cada rol interno segun la matriz de accesos.
- Ningun flujo sensible funciona sin autenticacion ni biometria cuando corresponde.
- Los errores del backend se muestran con mensajes claros y accionables.
