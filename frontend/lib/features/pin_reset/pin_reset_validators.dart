// Validadores UI del restablecimiento de PIN (F-T43).
//
// Presentación pura (cliente delgado, docs/19): solo verifican formato local
// (email con forma de correo, DNI de 8 dígitos). El backend es quien decide
// si el email existe, si el DNI coincide y si el OTP/PIN son válidos
// (`POST /auth/pin-reset`).
//
// El PIN reutiliza los validadores de `pin_setup` (F-T39, solo lectura):
// [kPinLength], [pinCreateError], [pinConfirmError], [isWeakPin].
//
// Seguridad: los mensajes NUNCA devuelven el email/DNI/OTP/PIN (docs/16
// reglas 7 y 10); el DNI solo se muestra enmascarado.
library;

/// El email debe tener forma de correo (no decide existencia).
bool isPinResetEmailValid(String email) {
  final clean = email.trim();
  // Límite alineado al contrato E1-T31 (`max_length=320`): no ser más
  // estricto que el backend.
  if (clean.isEmpty || clean.length > 320) return false;
  return RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$').hasMatch(clean);
}

/// El documento son solo digitos con longitud segun tipo (F-T50, paridad
/// KYC): DNI 8 / RUC 11. `docType` por defecto `DNI` preserva F-T43.
bool isDocNumberValid(String docNumber, {String docType = 'DNI'}) {
  final clean = docNumber.trim();
  final expected = docType.toUpperCase() == 'RUC' ? 11 : 8;
  if (clean.length != expected) return false;
  return int.tryParse(clean) != null;
}

/// Error de formato del email (validación UI, no de negocio).
const String pinResetInvalidEmailMessage =
    'Ingresa un correo válido para continuar.';

/// Error de formato del DNI (validación UI, no de negocio; sin revelar
/// nada del backend).
const String pinResetInvalidDocMessage =
    'Ingresa los 8 dígitos de tu DNI.';

/// Error de formato del documento segun tipo (F-T50): DNI 8 / RUC 11.
String pinResetInvalidDocMessageFor(String docType) =>
    docType.toUpperCase() == 'RUC'
        ? 'Ingresa los 11 dígitos de tu RUC.'
        : pinResetInvalidDocMessage;

/// Ayuda dinamica del documento segun tipo (F-T50).
String pinResetDocHelpFor(String docType) =>
    docType.toUpperCase() == 'RUC'
        ? '11 dígitos, como figura en tu RUC.'
        : '8 dígitos, como figura en tu DNI.';

/// Enmascara el DNI para la UI: `12345678` -> `****5678` (docs/20 §8).
String maskDocNumber(String docNumber) {
  final clean = docNumber.trim();
  if (clean.length < 4) return 'tu DNI';
  return '****${clean.substring(clean.length - 4)}';
}

/// Enmascara el email para la UI (sin filtrar existencia): `j***@dominio`.
String maskPinResetEmail(String email) {
  final clean = email.trim();
  final at = clean.indexOf('@');
  if (at <= 0) return 'tu correo';
  final local = clean.substring(0, at);
  final domain = clean.substring(at + 1);
  if (domain.isEmpty) return 'tu correo';
  final head = local.isEmpty ? '*' : local[0];
  return '$head***@$domain';
}
