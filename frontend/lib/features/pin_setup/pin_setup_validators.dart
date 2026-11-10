// Validadores UI del PIN de alta (F-T39).
//
// Presentacion pura (cliente delgado, docs/19): solo verifican formato local
// (longitud, digitos, coincidencia y secuencias triviales del fig `0:497`).
// El backend es quien valida el PIN/OTP y activa (`POST /auth/pin/setup`).
//
// Seguridad: los mensajes NUNCA devuelven el PIN (docs/16 reglas 7 y 10).
library;

/// Longitud del PIN del fig (`0:497`/`0:604`: 6 casillas).
const int kPinLength = 6;

/// El PIN debe tener [kPinLength] digitos.
const String pinLengthMessage = 'El PIN debe tener 6 dígitos.';

/// El PIN solo admite digitos.
const String pinDigitsMessage = 'El PIN solo puede contener dígitos.';

/// El PIN evita secuencias triviales (fig `0:497`: "Sin secuencias como
/// 123456").
const String pinWeakMessage =
    'Evita secuencias como 123456 o dígitos repetidos.';

/// La confirmacion debe ser identica al PIN creado (fig `0:604`).
const String pinMismatchMessage = 'Los PIN no coinciden.';

/// Valida el PIN creado. Retorna el mensaje de error o `null` si es valido.
String? pinCreateError(String pin) {
  if (pin.length != kPinLength) return pinLengthMessage;
  if (int.tryParse(pin) == null) return pinDigitsMessage;
  if (isWeakPin(pin)) return pinWeakMessage;
  return null;
}

/// Valida la confirmacion contra el PIN creado. Retorna el mensaje de error
/// o `null` si coincide.
String? pinConfirmError(String pin, String confirm) {
  if (confirm.length != kPinLength) return pinLengthMessage;
  if (confirm != pin) return pinMismatchMessage;
  return null;
}

/// `true` si el PIN es trivial: todos los digitos iguales o una secuencia
/// consecutiva completa ascendente/descendente (`123456`, `654321`).
bool isWeakPin(String pin) {
  if (pin.isEmpty) return false;
  var allSame = true;
  for (var i = 1; i < pin.length; i++) {
    if (pin[i] != pin[0]) {
      allSame = false;
      break;
    }
  }
  if (allSame) return true;
  var ascending = true;
  var descending = true;
  for (var i = 1; i < pin.length; i++) {
    final diff = pin.codeUnitAt(i) - pin.codeUnitAt(i - 1);
    if (diff != 1) ascending = false;
    if (diff != -1) descending = false;
  }
  return ascending || descending;
}
