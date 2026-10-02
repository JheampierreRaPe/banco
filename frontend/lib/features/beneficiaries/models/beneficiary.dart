/// Modelo de beneficiario (E2-T15 / HU07, contrato E2-T13 en docs/05 §6.2).
///
/// Shape del backend `GET /beneficiaries` -> `{"data": [Beneficiary],
/// "meta": {"total", "q"}}`, donde `Beneficiary` expone `id`, `alias`,
/// `photo_url`, `bank_code`, `account_masked` (`****` + ultimos 4),
/// `holder_name`, `preapproved_limit_minor`, `currency`, `status`
/// (`ACTIVE`/`DELETED`), `created_at`/`updated_at`.
///
/// Reglas de oro:
/// - La cuenta/CCI viaja YA enmascarada (`accountMasked`); el cliente NUNCA
///   reconstruye ni muestra el numero completo (docs/16 §2.9, docs/13 §1.2).
/// - Dinero entero en centimos (`preapprovedLimitMinor`); el cliente no calcula
///   reglas ni filtra bases grandes: la busqueda la resuelve el backend con
///   `?q=` (cliente delgado, docs/19 §4).
/// - La agrupacion (por banco o alfabetica) es criterio de UI, no de negocio.
library;

/// Beneficiario frecuente con datos enmascarados por defecto.
class Beneficiary {
  const Beneficiary({
    required this.id,
    required this.alias,
    required this.accountMasked,
    this.photoUrl,
    this.bankCode = '',
    this.holderName = '',
    this.preapprovedLimitMinor,
    this.currency = 'PEN',
    this.status = 'ACTIVE',
    this.createdAt,
    this.updatedAt,
  });

  final String id;
  final String alias;

  /// Cuenta/CCI YA enmascarada por el backend (p. ej. `•••• 7744` o
  /// `****7744`). Se muestra tal cual, nunca se desenmascara.
  final String accountMasked;
  final String? photoUrl;

  /// Codigo de la entidad (`BCP`, `INTERBANK`, `BBVA`, ...).
  final String bankCode;

  /// Nombre del titular (el backend lo envia enmascarado).
  final String holderName;

  /// Tope pre-aprobado en centimos (`null` = sin tope).
  final int? preapprovedLimitMinor;
  final String currency;

  /// `ACTIVE` | `DELETED`.
  final String status;
  final String? createdAt;
  final String? updatedAt;

  /// `true` si tiene tope (> 0): va sin biometria hasta ese monto (E2-T14).
  bool get hasTop =>
      preapprovedLimitMinor != null && preapprovedLimitMinor! > 0;

  /// Iniciales del avatar (p. ej. "Mama Lima" -> "ML").
  String get initials {
    final words = alias
        .split(RegExp(r'\s+'))
        .where((w) => w.isNotEmpty)
        .toList();
    if (words.isEmpty) return '?';
    final first = words.first[0];
    final second = words.length > 1 ? words[1][0] : '';
    return '$first$second'.toUpperCase();
  }

  factory Beneficiary.fromJson(Map<String, dynamic> json) {
    int? minorOf(Object? value) {
      if (value == null) return null;
      if (value is int) return value;
      if (value is num) return value.toInt();
      return int.tryParse(value.toString());
    }

    // El contrato nombra `account_masked`; se tolera el alias
    // `account_or_cci_masked` por compatibilidad.
    final masked = json['account_masked']?.toString() ??
        json['account_or_cci_masked']?.toString() ??
        '••••';
    return Beneficiary(
      id: json['id']?.toString() ?? '',
      alias: json['alias']?.toString() ?? '',
      accountMasked: masked,
      photoUrl: json['photo_url']?.toString(),
      bankCode: json['bank_code']?.toString() ?? '',
      holderName: json['holder_name']?.toString() ??
          json['holder_name_masked']?.toString() ??
          '',
      preapprovedLimitMinor: minorOf(json['preapproved_limit_minor']),
      currency: json['currency']?.toString() ?? 'PEN',
      status: json['status']?.toString() ?? 'ACTIVE',
      createdAt: json['created_at']?.toString(),
      updatedAt: json['updated_at']?.toString(),
    );
  }
}

/// Nombres de exhibicion por entidad (mockup `Agenda de Beneficiarios`).
String bankDisplayName(String code) {
  switch (code.toUpperCase()) {
    case 'BCP':
      return 'BCP · BANCO DE CRÉDITO';
    case 'INTERBANK':
    case 'IBK':
      return 'INTERBANK';
    case 'BBVA':
      return 'BBVA PERÚ';
    case 'SCOTIABANK':
      return 'SCOTIABANK';
    default:
      return code.isEmpty ? 'OTROS BANCOS' : code.toUpperCase();
  }
}

/// Nombre corto para la vista alfabetica (`Interbank · •••• 9102`).
String bankShortName(String code) {
  switch (code.toUpperCase()) {
    case 'BCP':
      return 'BCP';
    case 'INTERBANK':
    case 'IBK':
      return 'Interbank';
    case 'BBVA':
      return 'BBVA';
    case 'SCOTIABANK':
      return 'Scotiabank';
    default:
      return code.isEmpty ? 'Banco' : code;
  }
}

/// Etiqueta de moneda del mockup (`Cuenta Soles` / `Cuenta Dólares`).
String currencyAccountLabel(String currency) =>
    currency.toUpperCase() == 'USD' ? 'Cuenta Dólares' : 'Cuenta Soles';

/// Simbolo de moneda del mockup (`S/` / `$`).
String currencySymbol(String currency) =>
    currency.toUpperCase() == 'USD' ? r'$' : 'S/';

/// Formatea centimos a `S/ 1,000` (presentacion; el monto viene del servidor).
String formatTop(int minor, String currency) {
  final units = minor ~/ 100;
  final grouped = _thousands(units);
  return '${currencySymbol(currency)} $grouped';
}

String _thousands(int value) {
  final digits = value.abs().toString();
  final buffer = StringBuffer();
  var count = 0;
  for (var i = digits.length - 1; i >= 0; i--) {
    buffer.write(digits[i]);
    count++;
    if (count == 3 && i != 0) {
      buffer.write(',');
      count = 0;
    }
  }
  final reversed = buffer.toString().split('').reversed.join();
  return value < 0 ? '-$reversed' : reversed;
}

/// Grupo de beneficiarios para la lista (cabecera + items).
class BeneficiaryGroup {
  const BeneficiaryGroup({required this.title, required this.items, this.hint});

  final String title;
  final List<Beneficiary> items;

  /// Sub-etiqueta derecha del mockup (`Prioritarios`/`Frecuentes`), opcional.
  final String? hint;
}

/// Agrupa por banco (vista `Por Banco` del mockup, orden por banco + alias).
List<BeneficiaryGroup> groupByBank(List<Beneficiary> items) {
  final map = <String, List<Beneficiary>>{};
  final order = <String>[];
  for (final b in items) {
    final key = b.bankCode.toUpperCase().isEmpty
        ? 'OTROS'
        : b.bankCode.toUpperCase();
    if (!map.containsKey(key)) {
      map[key] = [];
      order.add(key);
    }
    map[key]!.add(b);
  }
  order.sort();
  return [
    for (final key in order)
      BeneficiaryGroup(
        title:
            '${bankDisplayName(key == 'OTROS' ? '' : key)} (${map[key]!.length})',
        items: List<Beneficiary>.from(map[key]!)
          ..sort((a, b) => a.alias.compareTo(b.alias)),
      ),
  ];
}

/// Agrupa por inicial del alias (vista `Alfabética (A-Z)` del mockup).
List<BeneficiaryGroup> groupByLetter(List<Beneficiary> items) {
  final map = <String, List<Beneficiary>>{};
  final order = <String>[];
  for (final b in items) {
    final key =
        b.alias.trim().isEmpty ? '#' : b.alias.trim()[0].toUpperCase();
    if (!map.containsKey(key)) {
      map[key] = [];
      order.add(key);
    }
    map[key]!.add(b);
  }
  order.sort();
  return [
    for (final key in order)
      BeneficiaryGroup(
        title: key,
        items: List<Beneficiary>.from(map[key]!)
          ..sort((a, b) => a.alias.compareTo(b.alias)),
      ),
  ];
}
