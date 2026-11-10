// Destino por DNI/celular con lookup previo (E2-T29, contrato E2-T27).
//
// `POST /transfers/third-party/resolve-target` (solo lectura, sin
// `Idempotency-Key`) recibe exactamente UNO de `target_document`
// (`{type, number}`) o `target_phone` y devuelve `{target_account_id,
// holder_name_masked, account_number_masked, currency}`.
// El cliente muestra el titular enmascarado y luego ejecuta
// `POST /transfers/third-party` con ese `target_account_id`
// (el POST de dinero no cambia).
//
// Cliente delgado (docs/19 §4): solo formato local (DNI 8/9 digitos,
// celular 9 digitos con prefijo `9`); la existencia y la resolucion
// real las decide el backend. Sin PII en logs ni en UI completa:
// solo mascaras del servidor.
library;

/// Destino resuelto por el lookup previo (solo mascaras + UUID).
class ResolvedTarget {
  const ResolvedTarget({
    required this.targetAccountId,
    required this.holderNameMasked,
    required this.accountNumberMasked,
    required this.currency,
  });

  final String targetAccountId;
  final String holderNameMasked;
  final String accountNumberMasked;
  final String currency;

  factory ResolvedTarget.fromJson(Map<String, dynamic> json) {
    return ResolvedTarget(
      targetAccountId: json['target_account_id']?.toString() ?? '',
      holderNameMasked: json['holder_name_masked']?.toString() ?? '',
      accountNumberMasked: json['account_number_masked']?.toString() ?? '',
      currency: json['currency']?.toString() ?? 'PEN',
    );
  }
}

/// Normaliza documento/celular: recorta y quita separadores visuales
/// (espacios/guiones). NO elimina letras (un valor con letras es
/// invalido y el validador lo rechaza).
String normalizeLookupDigits(String value) =>
    value.trim().replaceAll(RegExp(r'[\s\-]+'), '');

/// `true` si el DNI tiene formato valido (8 o 9 digitos, solo digitos).
bool isValidDniFormat(String value) =>
    RegExp(r'^[0-9]{8}$|^[0-9]{9}$').hasMatch(normalizeLookupDigits(value));

/// `true` si el celular tiene formato valido (9 digitos, prefijo `9`
/// para Peru, solo digitos).
bool isValidPhoneFormat(String value) {
  final digits = normalizeLookupDigits(value);
  return RegExp(r'^9[0-9]{8}$').hasMatch(digits);
}

/// Error de formato para DNI, o `null` si es valido (sin eco del valor).
String? dniFormatError(String value) {
  final digits = normalizeLookupDigits(value);
  if (digits.isEmpty) return 'Ingresa el DNI del destinatario.';
  if (!RegExp(r'^[0-9]+$').hasMatch(digits)) {
    return 'El DNI solo admite dígitos (8 o 9).';
  }
  if (digits.length != 8 && digits.length != 9) {
    return 'El DNI tiene 8 o 9 dígitos.';
  }
  return null;
}

/// Error de formato para celular, o `null` si es valido (sin eco).
String? phoneFormatError(String value) {
  final digits = normalizeLookupDigits(value);
  if (digits.isEmpty) return 'Ingresa el celular del destinatario.';
  if (!RegExp(r'^[0-9]+$').hasMatch(digits)) {
    return 'El celular solo admite dígitos (9, empieza con 9).';
  }
  if (!isValidPhoneFormat(digits)) {
    return 'Ingresa un celular válido de 9 dígitos (empieza con 9).';
  }
  return null;
}

/// Mensaje neutro para `TARGET_NOT_FOUND` (404): identico en todos los
/// casos, sin pistas de existencia (anti-enumeracion).
const String kTargetNotFoundMessage =
    'No pudimos ubicar esa cuenta o titular. Verifica los datos.';

/// Mensaje accionable para `TARGET_AMBIGUOUS` (409): no se elige una
/// cuenta automaticamente; se pide usar el numero de cuenta.
const String kTargetAmbiguousMessage =
    'El titular tiene más de una cuenta activa. Ingresa el número de cuenta.';
