/// Formato de presentacion del feature `credits` (E3-T04).
///
/// Solo formato para UI (separador de miles, simbolo `S/`); nunca opera sobre
/// montos ni tasas: todo numero lo calculo el servidor (cliente delgado,
/// `docs/19` §4).
library;

/// `1000000` minor + `PEN` -> `S/ 10,000.00`.
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

/// Tasa decimal exacta del servidor (`"0.185"`) -> `"18.50%"`.
///
/// Solo presentacion (multiplicar por 100 para mostrar porcentaje no es
/// recalcular la tasa). Si el texto no parsea, se devuelve tal cual.
String formatPercent(String decimalText) {
  final value = double.tryParse(decimalText.trim());
  if (value == null) return decimalText;
  return '${(value * 100).toStringAsFixed(2)}%';
}

/// Texto del usuario (`"3,500.00"`, `"S/ 10,000"`, `"12"`) -> centimos.
///
/// Solo parseo de entrada para el request (el cliente no calcula montos:
/// los valores del servidor se muestran con [formatMinor]). Acepta
/// separadores de miles (`,`/espacio), simbolo `S/` y hasta 2 decimales;
/// devuelve `null` si no es un monto valido (vacio, negativo, letras o mas
/// de 2 decimales). Jamas usa `double` (regla de oro 3).
int? parseAmountMinor(String input) {
  var text = input.trim().toUpperCase();
  text = text.replaceAll('S/', '').replaceAll('PEN', '').trim();
  text = text.replaceAll(',', '').replaceAll(' ', '');
  if (text.isEmpty) return null;
  final match = RegExp(r'^(\d+)(?:\.(\d{1,2}))?$').firstMatch(text);
  if (match == null) return null;
  final units = int.parse(match.group(1)!);
  final centsText = (match.group(2) ?? '').padRight(2, '0');
  final cents = int.parse(centsText);
  return units * 100 + cents;
}
