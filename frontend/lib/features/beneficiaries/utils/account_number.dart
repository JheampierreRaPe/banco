// Utilidades de formato de cuenta/CCI (E2-T29, decision del dueno 2026-10-03).
//
// Formato real del numero de cuenta CuyCash (E2-T28, docs/05 §6.2):
// 14 digitos con digito verificador — `CCC CCCCCCCCCC D` — o CCI de 20
// digitos. El DV es modulo 11 con pesos ciclicos 2..7 de derecha a
// izquierda sobre los 13 primeros digitos: `dv = 11 - (suma mod 11)`,
// mapeando `10`/`11` -> `0`.
//
// Cliente delgado con matiz del dueno (docs/19 §4 + E2-T29): el DV se
// valida localmente como FORMATO PURO (no verifica existencia); el
// backend revalida y es la autoridad final (`VALIDATION_ERROR`).
// La cuenta en claro solo viaja en el cuerpo del request; jamas se
// muestra completa ni se loguea (sin PII).
library;

/// Normaliza lo capturado: recorta y quita separadores visuales
/// (espacios/guiones). NO elimina letras: un valor con letras es
/// invalido y el `validator` debe rechazarlo (ya no hay letras en un
/// numero valido, E2-T29 quita el `_digits()` laxo).
String normalizeAccountOrCci(String value) =>
    value.trim().replaceAll(RegExp(r'[\s\-]+'), '');

/// DV (modulo 11, pesos 2..7 ciclicos de derecha a izquierda) de los
/// 13 primeros digitos. `-1` si la base no son 13 digitos.
int accountCheckDigit(String first13) {
  if (!RegExp(r'^[0-9]{13}$').hasMatch(first13)) return -1;
  var sum = 0;
  var weight = 2;
  for (var i = first13.length - 1; i >= 0; i--) {
    sum += (first13.codeUnitAt(i) - 0x30) * weight;
    weight++;
    if (weight > 7) weight = 2;
  }
  final dv = 11 - (sum % 11);
  if (dv == 10 || dv == 11) return 0;
  return dv;
}

/// `true` si la cuenta de 14 digitos trae prefijo `001` y DV valido
/// (alineado con el backend E2-T28: `^001[0-9]{11}$` + DV modulo 11).
bool isValidAccountCheckDigit(String account14) {
  if (!RegExp(r'^[0-9]{14}$').hasMatch(account14)) return false;
  if (!account14.startsWith('001')) return false;
  final expected = accountCheckDigit(account14.substring(0, 13));
  return expected == account14.codeUnitAt(13) - 0x30;
}

/// `true` si el valor es cuenta de 14 digitos con DV valido o CCI
/// de 20 digitos (solo digitos tras normalizar).
bool isValidAccountOrCciFormat(String value) {
  final normalized = normalizeAccountOrCci(value);
  if (RegExp(r'^[0-9]{20}$').hasMatch(normalized)) return true;
  return isValidAccountCheckDigit(normalized);
}

/// Mensaje de formato para el campo, o `null` si es valido.
///
/// Distingue el DV invalido (accionable) del resto de formatos; el
/// mensaje nunca incluye el numero capturado (sin PII en UI).
String? accountFormatError(String value) {
  final normalized = normalizeAccountOrCci(value);
  if (normalized.isEmpty) {
    return 'Ingresa 14 dígitos (cuenta) o 20 (CCI).';
  }
  if (RegExp(r'^[0-9]{20}$').hasMatch(normalized)) return null;
  if (RegExp(r'^[0-9]{14}$').hasMatch(normalized)) {
    if (!normalized.startsWith('001')) {
      return 'Ingresa una cuenta de 14 dígitos que empieza en 001, '
          'o CCI de 20.';
    }
    if (!isValidAccountCheckDigit(normalized)) {
      return 'Dígito verificador de cuenta incorrecto. '
          'Verifica los 14 dígitos.';
    }
    return null;
  }
  return 'Ingresa 14 dígitos (cuenta) o 20 (CCI).';
}
