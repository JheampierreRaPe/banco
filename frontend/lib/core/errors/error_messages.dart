/// Mensajes UI en espanol para cada `error.code` de docs/05 §4.
///
/// El backend envia `{ "error": { "code": ..., "message": ..., ... } }`.
/// Esta tabla traduce el codigo a un mensaje claro y accionable; el mensaje
/// del servidor se conserva en [ApiException.serverMessage] para depuracion.
const Map<String, String> apiErrorMessagesEs = {
  'VALIDATION_ERROR': 'Revisa los datos ingresados e intentalo de nuevo.',
  'INSUFFICIENT_FUNDS': 'Saldo insuficiente para completar la operacion.',
  'LIMIT_EXCEEDED': 'Superaste el limite permitido. Intentalo mas tarde.',
  'DUPLICATE_REQUEST':
      'Esta operacion ya fue registrada. Revisa tus movimientos.',
  'ACCOUNT_BLOCKED': 'Tu cuenta esta bloqueada. Contacta a tu banco.',
  'BENEFICIARY_NOT_FOUND': 'No encontramos ese beneficiario.',
  'KYC_REQUIRED': 'Debes completar la validacion de identidad primero.',
  'BIOMETRIC_REQUIRED': 'Esta operacion requiere autenticacion biometrica.',
  'BIOMETRIC_FAILED':
      'No pudimos verificar tu identidad. Intentalo de nuevo.',
  'QR_EXPIRED': 'El codigo QR vencio. Solicita uno nuevo.',
  'QR_TAMPERED': 'El codigo QR no es valido. Verifica el origen.',
  'RATE_EXPIRED': 'La cotizacion vencio. Solicita una nueva.',
  'RISK_BLOCKED':
      'Operacion retenida por seguridad. Te contactaremos pronto.',
  'SCREENING_HIT':
      'La operacion requiere revision adicional de cumplimiento.',
  'NOT_AUTHORIZED': 'No tienes permiso para esta operacion.',
  'NOT_FOUND': 'No encontramos lo que buscas.',
  'NETWORK_ERROR':
      'Sin conexion. Revisa tu internet e intentalo de nuevo.',
  'TIMEOUT_ERROR': 'El servidor esta tardando. Intentalo de nuevo.',
  'SERVER_ERROR': 'Ocurrio un error inesperado. Intentalo mas tarde.',
  'UNKNOWN': 'Ocurrio un error inesperado. Intentalo mas tarde.',
};

/// Mensaje UI para un `error.code` (o [fallback] generico).
String messageForCode(String code) =>
    apiErrorMessagesEs[code] ?? apiErrorMessagesEs['UNKNOWN']!;
