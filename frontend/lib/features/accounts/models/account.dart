/// Modelos del feature `accounts` (E2-T05 / HU05).
///
/// Shapes inferidos SOLO de los tests del backend autorizados
/// (`test_accounts_endpoints.py`, `test_accounts_movements_endpoints.py`):
/// - `GET /accounts` -> `{data: [{id, account_number_masked, type, currency,
///   available_minor, held_minor, balance_minor, ...}], meta: {total, ...}}`
/// - `GET /accounts/{id}` -> `{data: {..., status}}`
/// - `GET /accounts/{id}/movements` -> `{data: [{journal_entry_id, direction,
///   description, value_date, ...}], meta: {page, page_size, total}}`
/// - `GET .../movements/export` -> bytes (`text/csv`, cabecera `attachment`).
///
/// Regla de oro (docs/16 §2.9): el numero de cuenta viaja enmascarado
/// (`account_number_masked`, p. ej. `****1234`) y el cliente NUNCA intenta
/// reconstruir ni mostrar el numero completo.
library;

/// Cuenta con saldos diferenciados (docs/05 §6.2, HU05 CA-01/CA-02).
class Account {
  const Account({
    required this.id,
    required this.accountNumberMasked,
    required this.type,
    required this.currency,
    required this.availableMinor,
    required this.heldMinor,
    required this.balanceMinor,
    this.status,
  });

  final String id;

  /// Numero YA enmascarado por el backend. Se muestra tal cual.
  final String accountNumberMasked;
  final String type;
  final String currency;

  /// Saldo disponible / retenido / contable, en centimos (docs/16 §2.3).
  final int availableMinor;
  final int heldMinor;
  final int balanceMinor;
  final String? status;

  factory Account.fromJson(Map<String, dynamic> json) {
    int minorOf(Object? value) {
      if (value is int) return value;
      if (value is num) return value.toInt();
      return int.tryParse(value?.toString() ?? '') ?? 0;
    }

    return Account(
      id: json['id']?.toString() ?? '',
      accountNumberMasked: json['account_number_masked']?.toString() ?? '****',
      type: json['type']?.toString() ?? '',
      currency: json['currency']?.toString() ?? 'PEN',
      availableMinor: minorOf(json['available_minor']),
      heldMinor: minorOf(json['held_minor']),
      balanceMinor: minorOf(json['balance_minor']),
      status: json['status']?.toString(),
    );
  }
}

/// Movimiento de la cuenta (HU05 CA-03).
///
/// El campo de monto se lee de forma defensiva: los tests de export prueban
/// `amount_minor`; la lista expone `journal_entry_id/direction/description/
/// value_date`. Si el backend no trae monto, se usa 0 (no se inventa dinero).
class Movement {
  const Movement({
    required this.journalEntryId,
    required this.direction,
    required this.description,
    required this.valueDate,
    required this.amountMinor,
    this.currency = 'PEN',
  });

  final String journalEntryId;

  /// `CREDIT` | `DEBIT`.
  final String direction;
  final String description;

  /// Fecha valor ISO (`yyyy-MM-dd`), tal como la envia el backend.
  final String valueDate;
  final int amountMinor;
  final String currency;

  /// `true` para abonos (`CREDIT`).
  bool get isCredit => direction.toUpperCase() == 'CREDIT';

  factory Movement.fromJson(Map<String, dynamic> json) {
    Object? rawAmount = json['amount_minor'];
    // Soporte defensivo a envoltorio `{amount: {amount_minor}}` (docs/05 §1).
    final nested = json['amount'];
    if (rawAmount == null && nested is Map) {
      rawAmount = (nested)['amount_minor'];
    }
    int minor = 0;
    if (rawAmount is int) {
      minor = rawAmount;
    } else if (rawAmount is num) {
      minor = rawAmount.toInt();
    } else {
      minor = int.tryParse(rawAmount?.toString() ?? '') ?? 0;
    }
    String currency = json['currency']?.toString() ?? 'PEN';
    if (nested is Map && nested['currency'] != null) {
      currency = nested['currency'].toString();
    }
    return Movement(
      journalEntryId: json['journal_entry_id']?.toString() ?? '',
      direction: (json['direction']?.toString() ?? '').toUpperCase(),
      description: json['description']?.toString() ?? '',
      valueDate: json['value_date']?.toString() ?? '',
      amountMinor: minor,
      currency: currency,
    );
  }
}

/// Pagina de movimientos (formato docs/05 §4: `data` + `meta`).
class MovementsPage {
  const MovementsPage({
    required this.items,
    required this.page,
    required this.pageSize,
    required this.total,
  });

  final List<Movement> items;
  final int page;
  final int pageSize;
  final int total;

  bool get hasMore => items.length + (page - 1) * pageSize < total;

  factory MovementsPage.fromJson(Map<String, dynamic> json) {
    final data = json['data'];
    final meta = json['meta'];
    final items = data is List
        ? data
            .whereType<Map>()
            .map((e) => Movement.fromJson(Map<String, dynamic>.from(e)))
            .toList()
        : <Movement>[];
    int intOf(Object? v, int fallback) {
      if (v is int) return v;
      if (v is num) return v.toInt();
      return int.tryParse(v?.toString() ?? '') ?? fallback;
    }

    final metaMap = meta is Map ? Map<String, dynamic>.from(meta) : {};
    return MovementsPage(
      items: items,
      page: intOf(metaMap['page'], 1),
      pageSize: intOf(metaMap['page_size'], items.length),
      total: intOf(metaMap['total'], items.length),
    );
  }
}

/// Resultado de exportar movimientos (HU05 CA-04).
///
/// Contiene los bytes crudos + el nombre de archivo sugerido (de la cabecera
/// `content-disposition` o construido localmente). El guardado nativo en el
/// dispositivo queda pendiente (F-futuro): NO se agrega `file_saver` ni otra
/// dependencia (ver decision en el servicio).
class ExportResult {
  const ExportResult({
    required this.bytes,
    required this.suggestedFilename,
    required this.contentType,
  });

  final List<int> bytes;
  final String suggestedFilename;
  final String contentType;
}
