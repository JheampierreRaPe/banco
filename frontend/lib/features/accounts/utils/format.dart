/// Formato de dinero y fechas del feature `accounts` (E2-T05).
///
/// Vive aqui (no en `core/`) por la regla de convivencia: prohibido modificar
/// `lib/core/`. El dinero es entero en centimos (docs/16 §2.3); este helper
/// solo formatea para UI, nunca opera con `float` sobre saldos.
library;

/// `120000` minor + `PEN` -> `S/ 1,200.00`.
String formatMinor(int minor, String currency) {
  final symbol = _symbolFor(currency);
  final negative = minor < 0;
  final abs = minor.abs();
  final units = abs ~/ 100;
  final cents = (abs % 100).toString().padLeft(2, '0');
  return '${negative ? '-' : ''}$symbol ${_thousands(units)}.$cents';
}

String _symbolFor(String currency) {
  switch (currency.toUpperCase()) {
    case 'PEN':
      return 'S/';
    case 'USD':
      return 'US\$';
    case 'EUR':
      return '€';
  }
  return currency.toUpperCase();
}

String _thousands(int n) {
  final digits = n.toString();
  final buffer = StringBuffer();
  for (var i = 0; i < digits.length; i++) {
    if (i > 0 && (digits.length - i) % 3 == 0) buffer.write(',');
    buffer.write(digits[i]);
  }
  return buffer.toString();
}

/// `2026-02-15` -> `15/02/2026`. Si no es ISO, devuelve el texto tal cual.
String formatValueDate(String iso) {
  final parts = iso.split('-');
  if (parts.length != 3) return iso;
  return '${parts[2]}/${parts[1]}/${parts[0]}';
}
