// Modelos del feature `movements` (Historial + Reverso, E2-T24/E2-T25).
//
// Shapes del backend (`TransactionListItem`, `TransactionDetailData`,
// `ReverseTransactionData`):
// - `GET /transactions?page=&page_size=&date_from=&date_to=&type=&status=`
//   -> `{data: [...], meta: {page, page_size, total, request_id}}`.
// - `GET /transactions/{id}` -> `{data: {..., tracking, history}, meta}`.
// - `POST /transactions/{id}/reverse` (+ `Idempotency-Key`,
//   `{reason?}` <= 280) -> `200 {data: {transaction_id, status, ...}}` o
//   `409 CONFLICT` (estado no reversible / cuerpo distinto / carrera).
// - `GET /transactions/{id}/receipt` -> PDF (constancia E2-T10).
//
// Reglas de oro (docs/16 §2.9, docs/13 §1.2, docs/19 §4):
// - Dinero entero en centimos (`int`); el cliente NUNCA calcula saldos ni
//   comisiones: solo exhibe los valores que devuelve el servidor.
// - Datos enmascarados: la UI solo muestra mascaras (`••••` + ultimos 4);
//   el titular/cuenta en claro jamas se muestra ni se loguea.
// - Sin PII ni secretos en logs.
library;

/// Estados reversibles: solo estos admiten `POST .../reverse` (E2-T25).
const Set<String> kReversibleStatuses = {
  'POSTED',
  'SETTLED',
  'CONCILIATED',
};

/// `true` si [status] admite reverso por estado.
bool isReversibleStatus(String status) =>
    kReversibleStatuses.contains(status.toUpperCase());

/// Ventana de reverso inmediato del mockup (`Reverso 4`: maximo 72h).
const Duration kReverseWindow = Duration(hours: 72);

/// Filtros del historial (van a query params de `GET /transactions`).
class MovementFilters {
  const MovementFilters({
    this.dateFrom,
    this.dateTo,
    this.type,
    this.status,
    this.query = '',
  });

  /// ISO-8601 UTC (`date_from`).
  final DateTime? dateFrom;

  /// ISO-8601 UTC (`date_to`).
  final DateTime? dateTo;

  /// Tipo (`OWN_TRANSFER`, `THIRD_PARTY_TRANSFER`, `INTERBANK_TRANSFER`,
  /// `REVERSAL`, ...). `null` = todos.
  final String? type;

  /// Estado (`SETTLED`, `PENDING`, `REVERSED`, ...). `null` = todos.
  final String? status;

  /// Busqueda local en memoria (alias/DNI/num. op): NO viaja al servidor
  /// (E2-T24 no expone `q`); el servidor resuelve fecha/tipo/estado y el
  /// cliente filtra el texto solo sobre la pagina cargada.
  final String query;

  bool get isEmpty =>
      dateFrom == null &&
      dateTo == null &&
      type == null &&
      status == null &&
      query.trim().isEmpty;

  MovementFilters copyWith({
    DateTime? dateFrom,
    DateTime? dateTo,
    String? type,
    String? status,
    String? query,
    bool clearDates = false,
    bool clearType = false,
    bool clearStatus = false,
  }) {
    return MovementFilters(
      dateFrom: clearDates ? null : (dateFrom ?? this.dateFrom),
      dateTo: clearDates ? null : (dateTo ?? this.dateTo),
      type: clearType ? null : (type ?? this.type),
      status: clearStatus ? null : (status ?? this.status),
      query: query ?? this.query,
    );
  }

  /// Query params para `GET /transactions` (sin el `query` local).
  Map<String, dynamic> toQueryParams({required int page, required int pageSize}) {
    return {
      'page': page,
      'page_size': pageSize,
      if (dateFrom != null) 'date_from': dateFrom!.toUtc().toIso8601String(),
      if (dateTo != null) 'date_to': dateTo!.toUtc().toIso8601String(),
      if (type != null && type!.isNotEmpty) 'type': type,
      if (status != null && status!.isNotEmpty) 'status': status,
    };
  }
}

/// Presets de fecha del chip `Todas las fechas` (mockup Historial 1).
enum MovementDatePreset { all, today, last7, last30 }

/// Rango UTC del preset (hoy en hora local, bordes a UTC).
({DateTime? from, DateTime? to}) dateRangeForPreset(MovementDatePreset preset) {
  final now = DateTime.now();
  final startOfToday = DateTime(now.year, now.month, now.day);
  switch (preset) {
    case MovementDatePreset.all:
      return (from: null, to: null);
    case MovementDatePreset.today:
      return (from: startOfToday, to: null);
    case MovementDatePreset.last7:
      return (from: startOfToday.subtract(const Duration(days: 6)), to: null);
    case MovementDatePreset.last30:
      return (from: startOfToday.subtract(const Duration(days: 29)), to: null);
  }
}

/// Fila del historial (`GET /transactions`, E2-T24).
class Movement {
  const Movement({
    required this.transactionId,
    required this.type,
    required this.status,
    required this.amountMinor,
    required this.currency,
    this.feeMinor = 0,
    this.createdAt,
    this.settledAt,
    this.sourceMasked,
    this.targetMasked,
    this.externalRef,
  });

  final String transactionId;
  final String type;
  final String status;
  final int amountMinor;
  final String currency;
  final int feeMinor;
  final DateTime? createdAt;
  final DateTime? settledAt;
  final String? sourceMasked;
  final String? targetMasked;
  final String? externalRef;

  /// Titulo enmascarado para la tarjeta (sin PII: solo mascaras + tipo).
  String get title {
    final target = (targetMasked ?? '').trim();
    if (target.isNotEmpty) return target;
    final source = (sourceMasked ?? '').trim();
    if (source.isNotEmpty) return source;
    return typeLabel(type);
  }

  /// Numero de operacion corto (`OP-...` de `external_ref` o id corto).
  String get operationLabel {
    final ref = (externalRef ?? '').trim();
    if (ref.isNotEmpty) return ref;
    if (transactionId.length >= 8) {
      return 'Op #${transactionId.substring(0, 8).toUpperCase()}';
    }
    return 'Op #$transactionId';
  }

  /// `true` si el movimiento es de salida (signo negativo en el mockup).
  bool get isOutbound => amountMinor < 0 || type != 'REVERSAL';

  factory Movement.fromJson(Map<String, dynamic> json) {
    int minorOf(Object? value) {
      if (value is int) return value;
      if (value is num) return value.toInt();
      return int.tryParse(value?.toString() ?? '') ?? 0;
    }

    DateTime? dateOf(Object? value) {
      if (value == null) return null;
      return DateTime.tryParse(value.toString());
    }

    return Movement(
      transactionId: json['transaction_id']?.toString() ?? '',
      type: json['type']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      amountMinor: minorOf(json['amount_minor']),
      currency: json['currency']?.toString() ?? 'PEN',
      feeMinor: minorOf(json['fee_minor']),
      createdAt: dateOf(json['created_at']),
      settledAt: dateOf(json['settled_at']),
      sourceMasked: json['source_account_masked']?.toString(),
      targetMasked: json['target_account_masked']?.toString(),
      externalRef: json['external_ref']?.toString(),
    );
  }
}

/// Pagina del historial (`data` + `meta` de paginacion).
class PaginatedMovements {
  const PaginatedMovements({
    required this.items,
    required this.page,
    required this.pageSize,
    required this.total,
  });

  final List<Movement> items;
  final int page;
  final int pageSize;
  final int total;

  bool get hasMore => page * pageSize < total;

  factory PaginatedMovements.fromJson(Map<String, dynamic> body) {
    final data = body['data'];
    final meta = body['meta'];
    List raw = const [];
    if (data is List) {
      raw = data;
    }
    int metaInt(Object? value, int fallback) {
      if (value is int) return value;
      return int.tryParse(value?.toString() ?? '') ?? fallback;
    }

    return PaginatedMovements(
      items: raw
          .whereType<Map>()
          .map((e) => Movement.fromJson(Map<String, dynamic>.from(e)))
          .toList(),
      page: meta is Map ? metaInt(meta['page'], 1) : 1,
      pageSize: meta is Map ? metaInt(meta['page_size'], raw.length) : raw.length,
      total: meta is Map ? metaInt(meta['total'], raw.length) : raw.length,
    );
  }
}

/// Detalle de movimiento (`GET /transactions/{id}`).
class MovementDetail {
  const MovementDetail({
    required this.transactionId,
    required this.type,
    required this.status,
    required this.amountMinor,
    required this.currency,
    this.feeMinor = 0,
    this.externalRef,
    this.tracking = const {},
    this.failureCode,
    this.createdAt,
    this.settledAt,
    this.sourceAccountId,
    this.targetAccountId,
  });

  final String transactionId;
  final String type;
  final String status;
  final int amountMinor;
  final String currency;
  final int feeMinor;
  final String? externalRef;
  final Map<String, dynamic> tracking;
  final String? failureCode;
  final DateTime? createdAt;
  final DateTime? settledAt;
  final String? sourceAccountId;
  final String? targetAccountId;

  /// Total a debitar para EXHIBIR (`amount + fee` del servidor).
  int get totalMinor => amountMinor.abs() + feeMinor;

  /// `true` si el estado admite reverso (sin mirar la ventana de 72h).
  bool get isReversibleByStatus => isReversibleStatus(status);

  /// `true` si ya supero la ventana de 72h desde `createdAt`.
  bool get isExpired {
    final created = createdAt;
    if (created == null) return false;
    return DateTime.now().difference(created) > kReverseWindow;
  }

  /// Reversible = estado permitido y dentro de 72h.
  bool get isReversible => isReversibleByStatus && !isExpired;

  /// Explicacion para el boton deshabilitado (mockups Reverso 2/3/4).
  String get nonReversibleHint {
    final upper = status.toUpperCase();
    if (upper == 'REVERSED') {
      return 'Esta operación ya fue reversada. No admite un segundo reverso.';
    }
    if (upper == 'PENDING' || upper == 'PENDING_AUTHORIZATION' || upper == 'PROCESSING') {
      return 'Aún en compensación interbancaria y no admite reverso '
          'automático ahora. Espera la liquidación para solicitar reverso.';
    }
    if (upper == 'REJECTED' || upper == 'FAILED') {
      return 'La operación no se liquidó, por eso no admite reverso. '
          'Tu dinero está seguro: no se debitó nada.';
    }
    if (isExpired) {
      return 'Plazo límite superado (máximo 72 horas bancarias). '
          'Contacta a soporte para reclamos.';
    }
    return 'Este estado no admite reverso automático. '
        'Contacta a soporte si necesitas ayuda.';
  }

  /// Mascara corta de un UUID de cuenta (ultimos 4, sin PII).
  static String maskAccountId(String? id) {
    final clean = (id ?? '').replaceAll('-', '');
    if (clean.length < 4) return '••••';
    return '•••• ${clean.substring(clean.length - 4).toUpperCase()}';
  }

  String get sourceMasked => maskAccountId(sourceAccountId);
  String get targetMasked => maskAccountId(targetAccountId);

  String get trackingCode =>
      tracking['cce_tracking']?.toString() ??
      tracking['tracking_code']?.toString() ??
      tracking['external_ref']?.toString() ??
      '';

  factory MovementDetail.fromJson(Map<String, dynamic> json) {
    int minorOf(Object? value) {
      if (value is int) return value;
      if (value is num) return value.toInt();
      return int.tryParse(value?.toString() ?? '') ?? 0;
    }

    DateTime? dateOf(Object? value) {
      if (value == null) return null;
      return DateTime.tryParse(value.toString());
    }

    // El detalle (`GET /transactions/{id}`) no trae `created_at` directo:
    // se deriva del primer hito del historial (fecha de la operacion).
    DateTime? fromHistory(Object? history) {
      if (history is List) {
        for (final item in history) {
          if (item is Map && item['created_at'] != null) {
            final parsed = dateOf(item['created_at']);
            if (parsed != null) return parsed;
          }
        }
      }
      return null;
    }

    return MovementDetail(
      transactionId: json['transaction_id']?.toString() ?? '',
      type: json['type']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      amountMinor: minorOf(json['amount_minor']),
      currency: json['currency']?.toString() ?? 'PEN',
      feeMinor: minorOf(json['fee_minor']),
      externalRef: json['external_ref']?.toString(),
      tracking: json['tracking'] is Map
          ? Map<String, dynamic>.from(json['tracking'] as Map)
          : const {},
      failureCode: json['failure_code']?.toString(),
      createdAt: dateOf(json['created_at']) ?? fromHistory(json['history']),
      settledAt: dateOf(json['settled_at']),
      sourceAccountId: json['source_account_id']?.toString(),
      targetAccountId: json['target_account_id']?.toString(),
    );
  }
}

/// Resultado del reverso (`POST .../reverse`, E2-T25).
class ReversalResult {
  const ReversalResult({
    required this.transactionId,
    required this.status,
    this.replayed = false,
  });

  final String transactionId;
  final String status;
  final bool replayed;

  factory ReversalResult.fromJson(Map<String, dynamic> json, {bool replayed = false}) {
    return ReversalResult(
      transactionId: json['transaction_id']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      replayed: replayed,
    );
  }
}

/// Etiqueta corta del tipo para la UI.
String typeLabel(String type) {
  switch (type.toUpperCase()) {
    case 'OWN_TRANSFER':
      return 'Transf. propia';
    case 'THIRD_PARTY_TRANSFER':
      return 'Transferencia';
    case 'INTERBANK_TRANSFER':
      return 'Pago CCE';
    case 'REVERSAL':
      return 'Reverso';
    default:
      return 'Movimiento';
  }
}

/// Etiqueta del estado para el chip (mockups Historial 1 / Reverso 1-4).
String statusLabel(String status) {
  switch (status.toUpperCase()) {
    case 'POSTED':
      return 'POSTED';
    case 'SETTLED':
      return 'SETTLED';
    case 'CONCILIATED':
      return 'CONCILIADO';
    case 'PENDING':
    case 'PROCESSING':
      return 'PROCESANDO';
    case 'PENDING_AUTHORIZATION':
      return 'POR AUTORIZAR';
    case 'REVERSED':
      return 'REVERSADO';
    case 'REJECTED':
      return 'RECHAZADO';
    case 'FAILED':
      return 'FALLIDO';
    default:
      return status.toUpperCase();
  }
}

/// Formatea centimos a moneda (`35000/PEN` -> `S/ 350.00`).
///
/// Presentacion de valores que YA vienen del servidor; no decide reglas.
String formatMoneyMinor(int minor, String currency) {
  final symbol = currency.toUpperCase() == 'USD' ? r'$' : 'S/';
  final negative = minor < 0;
  final abs = minor.abs();
  final units = abs ~/ 100;
  final cents = (abs % 100).toString().padLeft(2, '0');
  return '$symbol ${negative ? '-' : ''}${_thousands(units)}.$cents';
}

String _thousands(int value) {
  final digits = value.toString();
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
  return buffer.toString().split('').reversed.join();
}
