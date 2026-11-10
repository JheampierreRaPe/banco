/// Modelos del area servicios y recargas (E4-T19 / HU16, contrato en
/// `docs/05` §6.5).
///
/// Shapes del backend (envoltorio `docs/05` §4):
/// - `GET /services/catalog` (`category?`/`q?`) ->
///   `200 {"data": [{biller_id, category, name, required_fields}]}`.
/// - `POST /services/payments` (`Idempotency-Key` obligatoria) ->
///   `201 {"data": {payment_id, transaction_id, status
///   (SETTLED/PENDING_AUTHORIZATION/REJECTED), amount_minor, currency,
///   receipt?, failure_code?, authorization?}}`.
/// - `POST /topups` (`Idempotency-Key` obligatoria) ->
///   `201 {"data": {topup_id, transaction_id, status, amount_minor,
///   currency, receipt?, failure_code?, authorization?}}`.
/// - `POST /services/subscriptions` (sin `Idempotency-Key`: no mueve
///   dinero) -> `201 {"data": {subscription_id, biller_id, supply_ref,
///   notify_days_before, status}}`.
///
/// Reglas de oro (docs/16 §2.9, docs/19 §4, docs/13 §1.2):
/// - Cliente delgado estricto: la app envia datos crudos
///   (`supply_ref`/`phone`/monto/fuente) y muestra `status`/`receipt` tal
///   cual del backend. No calcula montos/comisiones, no decide
///   limites/disponibilidad, no genera numeros de transaccion.
/// - El formateo `minor -> texto` es presentacion exacta (division entera,
///   sin redondeo ni `double`).
/// - Sin PII/secretos: los modelos no se loguean; el `supply_ref`/`phone`
///   solo vive como parametro efimero del request y enmascarado en el
///   comprobante (`supply_masked`/`phone_masked` del servidor).
library;

/// Empresa de servicios u operador del catalogo (tal cual del backend).
class Biller {
  const Biller({
    required this.billerId,
    required this.category,
    required this.name,
    required this.requiredFields,
  });

  final String billerId;
  final String category;
  final String name;

  /// Campos que exige el `biller` (p. ej. `["supply_ref"]` o `["phone"]`);
  /// la UI los construye dinamicamente, sin suponer el set.
  final List<String> requiredFields;

  bool get wantsSupply => requiredFields.contains('supply_ref');
  bool get wantsPhone => requiredFields.contains('phone');

  factory Biller.fromJson(Map<String, dynamic> json) {
    final raw = json['required_fields'];
    final fields = raw is List
        ? raw.map((e) => e.toString()).toList()
        : <String>[];
    return Biller(
      billerId: json['biller_id']?.toString() ?? '',
      category: (json['category']?.toString() ?? '').toUpperCase(),
      name: json['name']?.toString() ?? '',
      requiredFields: fields,
    );
  }
}

/// Comprobante estructurado tal cual del backend (numero de transaccion
/// oficial `receipt_no` `OP-…`; la app lo muestra, no lo genera).
class ServiceReceipt {
  const ServiceReceipt({
    required this.receiptNo,
    required this.transactionId,
    required this.concept,
    this.maskedRef,
    required this.amountMinor,
    required this.currency,
    this.paidAt,
    required this.status,
  });

  final String receiptNo;
  final String transactionId;
  final String concept;

  /// `supply_masked` o `phone_masked` segun la operacion (tal cual del
  /// servidor; nunca el valor completo en la UI).
  final String? maskedRef;
  final int amountMinor;
  final String currency;
  final String? paidAt;
  final String status;

  factory ServiceReceipt.fromJson(Map<String, dynamic> json) {
    return ServiceReceipt(
      receiptNo: json['receipt_no']?.toString() ?? '',
      transactionId: json['transaction_id']?.toString() ?? '',
      concept: json['concept']?.toString() ?? '',
      maskedRef: (json['supply_masked'] ?? json['phone_masked'])?.toString(),
      amountMinor: _asInt(json['amount_minor']),
      currency: (json['currency']?.toString() ?? 'PEN').toUpperCase(),
      paidAt: json['paid_at']?.toString(),
      status: (json['status']?.toString() ?? '').toUpperCase(),
    );
  }
}

/// Resultado de pago de servicio o recarga (tal cual del backend).
class ServiceOperationResult {
  const ServiceOperationResult({
    required this.operationId,
    required this.transactionId,
    required this.status,
    required this.amountMinor,
    required this.currency,
    this.receipt,
    this.failureCode,
    this.authMethod,
    this.authNonce,
    this.authExpiresAt,
    this.replayed = false,
  });

  /// `payment_id` o `topup_id` segun la operacion.
  final String operationId;
  final String transactionId;

  /// `SETTLED` | `PENDING_AUTHORIZATION` | `REJECTED` (lo decide el
  /// backend).
  final String status;
  final int amountMinor;
  final String currency;
  final ServiceReceipt? receipt;

  /// Solo con `REJECTED` (`DAILY_LIMIT_EXCEEDED`/`INSUFFICIENT_FUNDS`,
  /// patron E2-T07).
  final String? failureCode;

  /// Reto del motor (solo `PENDING_AUTHORIZATION`, patron
  /// `_challenge_fields` E2-T19): se confirma con
  /// `POST /transactions/{id}/authorize` (firma o PIN, E2-T31).
  final String? authMethod;
  final String? authNonce;
  final String? authExpiresAt;

  final bool replayed;

  bool get isSettled => status == 'SETTLED';
  bool get isPendingAuth => status == 'PENDING_AUTHORIZATION';
  bool get isRejected => status == 'REJECTED';

  String get statusLabel {
    switch (status) {
      case 'SETTLED':
        return 'Pagado';
      case 'PENDING_AUTHORIZATION':
        return 'Por autorizar';
      case 'REJECTED':
        return 'Rechazado';
      default:
        return status.isEmpty ? 'Sin estado' : status;
    }
  }

  factory ServiceOperationResult.fromJson(
    Map<String, dynamic> json, {
    bool replayed = false,
  }) {
    Map<String, dynamic> asMap(Object? value) {
      if (value is Map<String, dynamic>) return value;
      if (value is Map) return Map<String, dynamic>.from(value);
      return const {};
    }

    final auth = asMap(json['authorization']);
    final receiptRaw = json['receipt'];
    return ServiceOperationResult(
      operationId:
          (json['payment_id'] ?? json['topup_id'])?.toString() ?? '',
      transactionId: json['transaction_id']?.toString() ?? '',
      status: (json['status']?.toString() ?? '').toUpperCase(),
      amountMinor: _asInt(json['amount_minor']),
      currency: (json['currency']?.toString() ?? 'PEN').toUpperCase(),
      receipt: receiptRaw is Map
          ? ServiceReceipt.fromJson(Map<String, dynamic>.from(receiptRaw))
          : null,
      failureCode: json['failure_code']?.toString(),
      authMethod: auth['method']?.toString(),
      authNonce: auth['nonce']?.toString(),
      authExpiresAt: auth['expires_at']?.toString(),
      replayed: replayed,
    );
  }
}

/// Servicio registrado para recordatorios (tal cual del backend).
class ServiceSubscription {
  const ServiceSubscription({
    required this.subscriptionId,
    required this.billerId,
    this.billerName,
    required this.supplyRef,
    required this.notifyDaysBefore,
    required this.status,
  });

  final String subscriptionId;
  final String billerId;

  /// Nombre resuelto en el cliente desde el catalogo (presentacion; el
  /// backend solo devuelve `biller_id`).
  final String? billerName;
  final String supplyRef;
  final int notifyDaysBefore;
  final String status;

  String get statusLabel {
    switch (status.toUpperCase()) {
      case 'ACTIVE':
        return 'Recordatorio activo';
      default:
        return status.isEmpty ? 'Sin estado' : status;
    }
  }

  factory ServiceSubscription.fromJson(Map<String, dynamic> json) {
    return ServiceSubscription(
      subscriptionId: json['subscription_id']?.toString() ?? '',
      billerId: json['biller_id']?.toString() ?? '',
      billerName: json['biller_name']?.toString(),
      supplyRef: json['supply_ref']?.toString() ?? '',
      notifyDaysBefore: _asInt(json['notify_days_before']),
      status: (json['status']?.toString() ?? '').toUpperCase(),
    );
  }

  ServiceSubscription withBillerName(String name) => ServiceSubscription(
        subscriptionId: subscriptionId,
        billerId: billerId,
        billerName: name,
        supplyRef: supplyRef,
        notifyDaysBefore: notifyDaysBefore,
        status: status,
      );
}

int _asInt(Object? value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  return int.tryParse(value?.toString() ?? '') ?? 0;
}

/// Error de servicios/recargas ya traducido a UI: mensaje accionable en
/// español + si el Reintentar conserva la clave o no.
///
/// Nunca incluye `supply_ref`/`phone` completos ni montos no solicitados
/// (sin PII en mensajes ni logs).
class ServiceFailure {
  const ServiceFailure({required this.message, required this.retry});

  final String message;

  /// `true` = el Reintentar puede reusar la misma `Idempotency-Key`
  /// (resultado incierto: red/timeout). `false` = el fallo es definitivo
  /// o sin clave (validacion/negocio).
  final bool retry;
}

/// Traduce un `error.code` del backend (envoltorio `docs/05` §4) a mensaje
/// UI. No infiere el motivo: traduce el `code` (y el `failure_code` de un
/// `REJECTED`) que reporta el servidor.
ServiceFailure serviceFailureFor({
  required String code,
  String? failureCode,
  String fallback = 'Ocurrió un error inesperado. Inténtalo de nuevo.',
}) {
  switch (code) {
    case 'NOT_FOUND':
      return const ServiceFailure(
        message: 'No encontramos ese servicio u operador. Verifica el '
            'nombre o número ingresado e inténtalo de nuevo.',
        retry: false,
      );
    case 'VALIDATION_ERROR':
      return const ServiceFailure(
        message: 'Revisa los datos ingresados: algún campo es inválido. '
            'No se realizó ningún cobro.',
        retry: false,
      );
    case 'CONFLICT':
    case 'IDEMPOTENCY_CONFLICT':
    case 'DUPLICATE_REQUEST':
      return const ServiceFailure(
        message: 'Esta operación ya fue registrada. Revisa tus movimientos '
            'antes de intentarlo de nuevo: no se cobró dos veces.',
        retry: false,
      );
    case 'DAILY_LIMIT_EXCEEDED':
      return const ServiceFailure(
        message: 'Superaste tu límite diario. Podrás operar de nuevo '
            'mañana. No se descontó nada de tu cuenta.',
        retry: false,
      );
    case 'INSUFFICIENT_FUNDS':
    case 'NOT_SUFFICIENT_FUNDS':
      return const ServiceFailure(
        message: 'No tienes saldo suficiente en la fuente elegida. '
            'Elige otra fuente o recarga. No se debitó nada.',
        retry: false,
      );
    case 'SERVICE_PROVIDER_UNAVAILABLE':
      return const ServiceFailure(
        message: 'La empresa del servicio no responde ahora mismo. '
            'Inténtalo de nuevo en unos minutos. No se cobró nada.',
        retry: true,
      );
    case 'SETTLEMENT_UNAVAILABLE':
      // Liquidacion del proveedor no configurada/no disponible: NO es un
      // error del formulario del titular (por eso no se muestra "algún
      // campo es inválido"). Mensaje honesto y neutro, sin detalles internos.
      return const ServiceFailure(
        message: 'El servicio de pagos no está disponible por ahora. '
            'Inténtalo más tarde. No se cobró nada.',
        retry: true,
      );
    case 'SERVICE_PROVIDER_TIMEOUT':
      return const ServiceFailure(
        message: 'La consulta al proveedor tardó demasiado. Inténtalo de '
            'nuevo; si la operación se completó la verás en tus '
            'movimientos (no se cobra dos veces).',
        retry: true,
      );
    case 'NETWORK_ERROR':
    case 'TIMEOUT_ERROR':
      return const ServiceFailure(
        message: 'Sin conexión. Revisa tu internet e inténtalo de nuevo. '
            'Si la operación se completó, la verás en tus movimientos '
            '(no se cobra dos veces).',
        retry: true,
      );
    default:
      final failure = (failureCode ?? '').toUpperCase();
      if (failure == 'DAILY_LIMIT_EXCEEDED') {
        return const ServiceFailure(
          message: 'Superaste tu límite diario. Podrás operar de nuevo '
              'mañana. No se descontó nada de tu cuenta.',
          retry: false,
        );
      }
      if (failure == 'INSUFFICIENT_FUNDS') {
        return const ServiceFailure(
          message: 'No tienes saldo suficiente en la fuente elegida. '
              'Elige otra fuente o recarga. No se debitó nada.',
          retry: false,
        );
      }
      return ServiceFailure(message: fallback, retry: true);
  }
}

/// Formatea centimos del servidor a texto exacto (division entera, sin
/// redondeo ni `double`; solo presentacion, sin recalculo de negocio).
String formatServiceMinor(int minor, String currency) {
  final symbol = currency.toUpperCase() == 'USD' ? 'US\$' : 'S/';
  final abs = minor.abs();
  final sign = minor < 0 ? '-' : '';
  return '$sign$symbol ${abs ~/ 100}.${(abs % 100).toString().padLeft(2, '0')}';
}
