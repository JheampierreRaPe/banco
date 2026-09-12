# 08 - Epica 3: Creditos Digitales (HU09-HU12)

**Modulo responsable:** `credits`, apoyado en `transactions`, `ledger`, `identity` (firma),
`risk`, `notifications`.
**Resultado de la epica:** el cliente simula, solicita, obtiene decision, firma y recibe el
desembolso con cronograma de cuotas.

## HU09 - Simulador Inteligente de Creditos (Should, 5 SP, Sprint 2)

**Logica**
1. El cliente ingresa monto, plazo y tasa TCEA (o elige un producto con tasa configurada).
2. El sistema calcula la cuota con **amortizacion francesa** (cuota fija) y muestra el
   cronograma provisional (capital, interes y saldo).
3. Opcionalmente incluye seguro de desgravamen (recalcula la cuota).
4. Se puede descargar/compartir el resultado.

**Reglas**: calculo < 1 s; formulas y tasas configurables por producto; el simulador **no**
genera ningun registro crediticio real.
**CA**: CA-01 (cuota), CA-02 (cronograma), CA-03 (desgravamen), CA-04 (descarga).

**Tareas**
- `E3-T01` Motor de calculo financiero puro y testeable (cuota, cronograma, TCEA). *Backend.*
- `E3-T02` Catalogo `loan_products` configurable. *Datos.*
- `E3-T03` Endpoint `/credits/simulate` + export PDF. *Backend.*
- `E3-T04` UI Flutter con sliders y tabla de amortizacion. *Frontend.*
- `E3-T05` Pruebas de exactitud contra casos de referencia. *QA.*

## HU10 - Solicitud y Evaluacion Crediticia Automatica (Must, 8 SP, Sprint 3)

**Logica**
1. Solo se puede enviar la solicitud si el cliente **autoriza** la consulta en centrales de
   riesgo (consentimiento guardado).
2. Se validan campos obligatorios; si falta algo, no pasa al motor.
3. El motor de reglas consulta el **bureau simulado** y el score interno y calcula ratio
   deuda/ingreso (DTI), tope por perfil e historial interno.
4. Emite dictamen: `APROBADO`, `RECHAZADO` o `EN_REVISION`, con justificacion.
5. Se notifica al cliente y se guarda el expediente con ID unico.

**Reglas**: pre-calificacion <= 5 min; reintentos automaticos si el bureau falla; el resultado y
su justificacion se conservan para auditoria; la aprobacion queda **separada** de la solicitud.
**CA**: CA-01 (autorizacion bureau), CA-02 (validacion de campos), CA-03 (dictamen), CA-04
(estado visible).

**Tareas**
- `E3-T06` Adaptador `CreditBureau` (mock + interfaz real) con reintentos. *Backend.*
- `E3-T07` Motor de reglas de credito configurable (DTI, topes, score). *Backend/Datos.*
- `E3-T08` Caso de uso de solicitud + expediente + notificacion. *Backend.*
- `E3-T09` UI Flutter: formulario, consentimiento y pantalla de estado. *Frontend.*
- `E3-T10` Pantalla de revision para el analista de credito (panel web). *Frontend.*
- `E3-T11` Pruebas: aprobado, rechazado, en revision, bureau caido. *QA.*

## HU11 - Firma Digital de Contrato con Biometria Facial (Must, 8 SP, Sprint 3)

**Logica**
1. Con el credito aprobado, se genera el **contrato + pagare + hoja resumen** con las
   condiciones aprobadas.
2. El cliente lee el contrato y firma validando con la **biometria del dispositivo**
   (consentimiento explicito). No se usa liveness del servidor.
3. Se vincula el **token biometrico del dispositivo** (firma del `nonce` + `device_id`) a la firma
   y se sella el documento con hash.
4. Se envia copia certificada al correo del cliente.

**Reglas**: firma con no repudio; el token biometrico del dispositivo se guarda cifrado y **no se
reutiliza**; documento disponible < 1 min.
**CA**: CA-01 (contrato generado), CA-02 (consentimiento biometrico), CA-03 (firma certificada),
CA-04 (copia remitida).

**Tareas**
- `E3-T12` Generacion de documentos (contrato, pagare, resumen). *Backend.*
- `E3-T13` Servicio de firma: hash del documento + token biometrico del dispositivo + sello.
  *Backend/Seguridad.*
- `E3-T14` Endpoint de firma y envio por correo. *Backend.*
- `E3-T15` UI Flutter de lectura y firma con biometria. *Frontend.*
- `E3-T16` Pruebas de integridad del documento y no reutilizacion del token. *QA.*

## HU12 - Desembolso Inmediato y Programacion de Cuotas (Must, 5 SP, Sprint 3)

**Logica**
1. Con el contrato firmado, se ordena el desembolso a la cuenta elegida (asiento 7.5).
2. Se genera el **cronograma definitivo** (fechas y montos).
3. El cliente puede habilitar **debito automatico** de cuotas.
4. Se notifica cada cuota proxima a vencer (2-3 dias antes).

**Reglas**: desembolso < 30 s tras la firma; operacion **todo o nada** (sin acreditaciones
parciales); el pago de cuota tambien pasa por el motor (asiento 7.6).
**CA**: CA-01 (desembolso), CA-02 (cronograma), CA-03 (debito automatico), CA-04 (recordatorio).

**Tareas**
- `E3-T17` Caso de uso de desembolso transaccional (credits -> transactions -> ledger). *Backend.*
- `E3-T18` Generacion y persistencia de `loan_schedules`. *Datos.*
- `E3-T19` Job de debito automatico y recordatorios. *Backend.*
- `E3-T20` Endpoint de pago de cuota y aplicacion contable. *Backend.*
- `E3-T21` UI Flutter: cronograma y activacion de debito automatico. *Frontend.*
- `E3-T22` Pruebas: desembolso atomico, cuota vencida, debito automatico. *QA.*

## Criterios de salida de la epica

- El ciclo completo (simular -> solicitar -> dictamen -> firmar -> desembolsar -> pagar) corre
  de punta a punta.
- El desembolso y el pago de cuotas quedan contabilizados en partida doble.
- Ninguna solicitud se evalua sin consentimiento explicito del cliente.
