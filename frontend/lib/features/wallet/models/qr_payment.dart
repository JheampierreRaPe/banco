/// Modelos del area pago QR del pagador (E4-T13 / HU15, contrato E4-T12
/// en `docs/05` §6.5).
///
/// Shape del backend (envoltorio `docs/05` §4):
/// - `POST /payments/qr` (`Idempotency-Key` obligatoria) ->
///   `201 {"data": {transaction_id, qr_payment_id, charge_id,
///   merchant {merchant_id, business_name}, amount_minor (entero),
///   currency, status (SETTLED/PENDING_AUTHORIZATION/REJECTED),
///   failure_code?, authorization?}, "meta": {request_id, replayed}}`.
/// - `authorization` (solo `PENDING_AUTHORIZATION`) sigue el patron
///   `_challenge_fields` (E2-T19): `{method: "BIOMETRIC", nonce,
///   expires_at}`; el cliente lo confirma con
///   `POST /transactions/{id}/authorize` (firma o PIN, E2-T31).
///
/// Reglas de oro (docs/16 §2.9, docs/19 §4, docs/13 §1.2):
/// - Cliente delgado estricto: la app envia el `qr_content` crudo y
///   muestra `comercio`/`monto`/`status` tal cual del backend. No valida
///   la firma del QR, no decide el umbral/limite, no calcula montos.
/// - El formateo `minor -> texto` es presentacion exacta (division
///   entera, sin redondeo ni `double`).
/// - Sin PII/secretos: estos modelos no tienen campos para el secreto
///   HMAC, el RUC ni el `qr_content`; el `qr_content` solo vive como
///   parametro efimero del request, jamas en logs ni en disco.
library;

/// Resultado del pago QR tal cual del backend (E4-T12, `docs/05` §6.5;
/// E4-T21 agrega `payee` en QR personal; E4-T24 agrega
/// `payee_name_masked` donde el servidor lo exponga).
class QrPaymentResult {
  const QrPaymentResult({
    required this.transactionId,
    required this.qrPaymentId,
    required this.chargeId,
    required this.merchantId,
    required this.merchantName,
    this.payeeOwnerId,
    this.payeeNameMasked,
    required this.amountMinor,
    required this.currency,
    required this.status,
    this.failureCode,
    this.authMethod,
    this.authNonce,
    this.authExpiresAt,
    this.replayed = false,
  });

  final String transactionId;
  final String qrPaymentId;
  final String chargeId;
  final String merchantId;

  /// Razon social / nombre del comercio (tal cual del backend; vacio
  /// en QR personal).
  final String merchantName;

  /// Titular destino de un QR personal (E4-T21; `null` en comercio).
  final String? payeeOwnerId;

  /// Nombre + un apellido del dueno enmascarado tal cual del servidor
  /// (E4-T24, `payee_name_masked`, p. ej. `DIEG••• GONZ•••`); `null`
  /// cuando el payload no lo trae (cliente delgado: se muestra tal cual,
  /// sin calcularlo; la UI oculta la fila).
  final String? payeeNameMasked;
  final int amountMinor;
  final String currency;

  /// `SETTLED` | `PENDING_AUTHORIZATION` | `REJECTED` (lo decide el
  /// backend).
  final String status;

  /// Solo con `REJECTED` (`DAILY_LIMIT_EXCEEDED`/`INSUFFICIENT_FUNDS`,
  /// patron E2-T07): sin tocar saldos ni el cargo.
  final String? failureCode;

  /// Reto del motor (solo `PENDING_AUTHORIZATION`): `method` (`BIOMETRIC`),
  /// `nonce` y `expires_at` (patron `_challenge_fields`, E2-T19).
  final String? authMethod;
  final String? authNonce;
  final String? authExpiresAt;

  /// `true` si el backend reuso un intento previo (misma clave + mismo
  /// cuerpo, sin duplicar el pago).
  final bool replayed;

  bool get isSettled => status == 'SETTLED';
  bool get isPendingAuth => status == 'PENDING_AUTHORIZATION';
  bool get isRejected => status == 'REJECTED';

  /// `true` cuando el cobro fue a un QR personal (E4-T21).
  bool get isPersonal => (payeeOwnerId ?? '').isNotEmpty;

  /// Nombre a mostrar: comercio o etiqueta neutra del QR personal
  /// (presentacion; el destino real lo decidio el backend).
  String get displayName =>
      merchantName.isEmpty ? 'QR personal' : merchantName;

  /// Etiqueta en español para el badge (presentacion del `status` del
  /// servidor; no decide nada).
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

  factory QrPaymentResult.fromJson(
    Map<String, dynamic> json, {
    bool replayed = false,
  }) {
    Map<String, dynamic> merchantOf(Object? value) {
      if (value is Map<String, dynamic>) return value;
      if (value is Map) return Map<String, dynamic>.from(value);
      return const {};
    }

    Map<String, dynamic> authOf(Object? value) {
      if (value is Map<String, dynamic>) return value;
      if (value is Map) return Map<String, dynamic>.from(value);
      return const {};
    }

    final merchant = merchantOf(json['merchant']);
    final auth = authOf(json['authorization']);
    final payee = merchantOf(json['payee']);
    final ownerId = payee['owner_user_id']?.toString();
    return QrPaymentResult(
      transactionId: json['transaction_id']?.toString() ?? '',
      qrPaymentId: json['qr_payment_id']?.toString() ?? '',
      chargeId: json['charge_id']?.toString() ?? '',
      merchantId: merchant['merchant_id']?.toString() ?? '',
      merchantName: merchant['business_name']?.toString() ?? '',
      payeeOwnerId: (ownerId == null || ownerId.isEmpty) ? null : ownerId,
      payeeNameMasked: _asMaskedName(json['payee_name_masked']),
      amountMinor: _asInt(json['amount_minor']),
      currency: (json['currency']?.toString() ?? 'PEN').toUpperCase(),
      status: (json['status']?.toString() ?? '').toUpperCase(),
      failureCode: json['failure_code']?.toString(),
      authMethod: auth['method']?.toString(),
      authNonce: auth['nonce']?.toString(),
      authExpiresAt: auth['expires_at']?.toString(),
      replayed: replayed,
    );
  }
}

int _asInt(Object? value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  return int.tryParse(value?.toString() ?? '') ?? 0;
}

/// Vista previa del cargo QR antes de confirmar (fix MEDIO-3, HU15 CA-01,
/// `POST /payments/qr/preview`, `docs/05` §6.5).
///
/// Solo lectura: `comercio`/`monto` tal cual del backend para el
/// consentimiento informado. Sin `qr_content`, firma, secreto ni RUC.
/// `payee_name_masked` (E4-T24, aditivo): nombre + un apellido del dueno
/// enmascarado tal cual del servidor; en comercio es el nombre natural
/// del dueno (`business_name` intacto); `null`/ausente si no resoluble
/// (la UI oculta la fila).
class QrChargePreview {
  const QrChargePreview({
    required this.chargeId,
    required this.merchantId,
    required this.merchantName,
    this.payeeOwnerId,
    this.payeeNameMasked,
    required this.amountMinor,
    required this.currency,
    this.expiresAt,
    this.status = 'ACTIVE',
  });

  final String chargeId;
  final String merchantId;

  /// Razon social / nombre del comercio (tal cual del backend; vacio
  /// en QR personal).
  final String merchantName;

  /// Titular destino de un QR personal (E4-T21; `null` en comercio).
  final String? payeeOwnerId;

  /// Nombre + un apellido del dueno enmascarado tal cual del servidor
  /// (E4-T24; `null` si no resoluble; la UI oculta la fila).
  final String? payeeNameMasked;

  /// Monto fijo del cargo en centimos (`0` + [isPersonal] cuando el QR
  /// no trae monto: el pagador lo digita al confirmar).
  final int amountMinor;
  final String currency;

  /// `expires_at` (`None` en `STATIC`).
  final String? expiresAt;

  /// `ACTIVE` (lo decide el backend).
  final String status;

  /// `true` cuando el QR es personal sin monto (E4-T21): el pagador
  /// digita el monto en soles en el resumen.
  bool get isPersonal => (payeeOwnerId ?? '').isNotEmpty;

  /// Nombre a mostrar: comercio o etiqueta neutra del QR personal.
  String get displayName =>
      merchantName.isEmpty ? 'QR personal' : merchantName;

  factory QrChargePreview.fromJson(Map<String, dynamic> json) {
    Map<String, dynamic> merchantOf(Object? value) {
      if (value is Map<String, dynamic>) return value;
      if (value is Map) return Map<String, dynamic>.from(value);
      return const {};
    }

    final merchant = merchantOf(json['merchant']);
    final payee = merchantOf(json['payee']);
    final ownerId = payee['owner_user_id']?.toString();
    return QrChargePreview(
      chargeId: json['charge_id']?.toString() ?? '',
      merchantId: merchant['merchant_id']?.toString() ?? '',
      merchantName: merchant['business_name']?.toString() ?? '',
      payeeOwnerId: (ownerId == null || ownerId.isEmpty) ? null : ownerId,
      payeeNameMasked: _asMaskedName(json['payee_name_masked']),
      amountMinor: _asInt(json['amount_minor']),
      currency: (json['currency']?.toString() ?? 'PEN').toUpperCase(),
      expiresAt: json['expires_at']?.toString(),
      status: (json['status']?.toString() ?? 'ACTIVE').toUpperCase(),
    );
  }
}

/// `payee_name_masked` es opaco para el cliente (E4-T24): se muestra tal
/// cual del servidor. Ausente/`null`/vacio -> `null` (la UI oculta la
/// fila); nunca se calcula ni se completa en el cliente.
String? _asMaskedName(Object? value) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? null : text;
}

/// Accion de recuperacion que acompaña a cada error de pago QR
/// (E4-T14): volver al escaner o reintentar el mismo intento.
enum QrPayRecovery { rescan, retry, none }

/// Error de pago QR ya traducido a UI (E4-T14): un mensaje accionable
/// en español + su accion de recuperacion.
///
/// - La app no infiere el motivo por su cuenta: traduce el `code` (y el
///   `failure_code` de un `REJECTED`) que reporta el backend.
/// - Nunca incluye el `qr_content`, montos no solicitados ni datos del
///   comercio (sin PII/secretos en mensajes ni logs).
class QrPayFailure {
  const QrPayFailure({
    required this.message,
    required this.recovery,
    this.invalidQr = false,
  });

  /// Mensaje claro y accionable en español.
  final String message;

  /// Camino de recuperacion (`Re-escanear` / `Reintentar`).
  final QrPayRecovery recovery;

  /// `true` cuando el QR es invalido (vencido/corrupto/manipulado/
  /// ajeno): la UI muestra la alerta visual (`error-container` + badge
  /// `QR inválido`, E4-T14).
  final bool invalidQr;
}

/// Traduce un rechazo del backend a mensaje UI (E4-T14).
///
/// - [code]: `error.code` del envoltorio `docs/05` §4 (`VALIDATION_ERROR`,
///   `CONFLICT`, `NOT_AUTHORIZED`, `DAILY_LIMIT_EXCEEDED`, red, ...).
/// - [serverHint]: indicio textual del servidor (`serverMessage`) para
///   distinguir vencido/corrupto/manipulado/ajeno dentro de un
///   `VALIDATION_ERROR` (`ApiException` no propaga `details`; supuesto
///   documentado en el reporte E4-T14).
/// - [failureCode]: `failure_code` de un `REJECTED` (`docs/05` §6.5,
///   patron E2-T07).
/// - [fallback]: mensaje del servidor ya traducido (se usa solo para
///   codigos sin mapeo propio).
QrPayFailure qrPayFailureFor({
  required String code,
  String? serverHint,
  String? failureCode,
  String fallback = 'Ocurrió un error inesperado. Inténtalo de nuevo.',
}) {
  final hint = (serverHint ?? '').toLowerCase();
  switch (code) {
    case 'VALIDATION_ERROR':
      // Granularidad por indicio del servidor (decision abierta 2 de
      // E4-T14: E4-T12 no garantiza `details` estructurado; se usa el
      // mensaje del servidor como indicio, sin logica de negocio).
      if (_containsAny(hint, ['venc', 'expir', 'caduc', 'ttl'])) {
        return const QrPayFailure(
          message: 'Este QR venció. Pide al comercio un QR nuevo y '
              'vuelve a escanearlo. No se descontó nada de tu cuenta.',
          recovery: QrPayRecovery.rescan,
          invalidQr: true,
        );
      }
      if (_containsAny(hint, [
        'manip',
        'tamper',
        'firma',
        'signature',
        'sig ',
        'alter',
        'hash',
      ])) {
        return const QrPayFailure(
          message: 'Este QR fue manipulado o no es confiable. Por tu '
              'seguridad no se realizó ningún cobro. Verifica el origen '
              'con el comercio y vuelve a escanear un QR válido.',
          recovery: QrPayRecovery.rescan,
          invalidQr: true,
        );
      }
      if (_containsAny(hint, [
        'ajeno',
        'desconocido',
        'no reconocido',
        'unknown',
        'foreign',
        'inexistente',
      ])) {
        return const QrPayFailure(
          message: 'No reconocemos este QR. Verifica que sea de un '
              'comercio afiliado y vuelve a escanearlo. '
              'No se descontó nada de tu cuenta.',
          recovery: QrPayRecovery.rescan,
          invalidQr: true,
        );
      }
      return const QrPayFailure(
        message: 'Este QR está corrupto o no se puede leer. Vuelve a '
            'escanearlo con buena luz. No se descontó nada de tu cuenta.',
        recovery: QrPayRecovery.rescan,
        invalidQr: true,
      );
    case 'CONFLICT':
    case 'IDEMPOTENCY_CONFLICT':
    case 'DUPLICATE_REQUEST':
      return const QrPayFailure(
        message: 'Este QR ya fue pagado. Revisa tus movimientos antes '
            'de intentarlo de nuevo: no se realizó un segundo cobro.',
        recovery: QrPayRecovery.rescan,
      );
    case 'DAILY_LIMIT_EXCEEDED':
      return const QrPayFailure(
        message: 'Superaste tu límite diario. Podrás pagar de nuevo '
            'mañana. No se descontó nada de tu cuenta.',
        recovery: QrPayRecovery.rescan,
      );
    case 'INSUFFICIENT_FUNDS':
    case 'NOT_SUFFICIENT_FUNDS':
      return const QrPayFailure(
        message: 'No tienes saldo suficiente en la fuente elegida. '
            'Elige otra fuente o recarga. Tu dinero está seguro: '
            'no se debitó nada.',
        recovery: QrPayRecovery.retry,
      );
    case 'NOT_AUTHORIZED':
      return const QrPayFailure(
        message: 'Tu sesión no tiene permiso para este pago. '
            'Vuelve a iniciar sesión e inténtalo de nuevo.',
        recovery: QrPayRecovery.retry,
      );
    case 'NOT_FOUND':
      return const QrPayFailure(
        message: 'No encontramos la fuente de fondos elegida. '
            'Elige otra fuente e inténtalo de nuevo.',
        recovery: QrPayRecovery.retry,
      );
    case 'NETWORK_ERROR':
    case 'TIMEOUT_ERROR':
      return const QrPayFailure(
        message: 'Sin conexión. Revisa tu internet e inténtalo de nuevo. '
            'Si el pago se completó, lo verás en tus movimientos '
            '(no se cobra dos veces).',
        recovery: QrPayRecovery.retry,
      );
    default:
      // `REJECTED` con `failure_code` (patron E2-T07): el HTTP es `201`
      // y el motivo viaja en el cuerpo, no en `error.code`.
      final failure = (failureCode ?? '').toUpperCase();
      if (failure == 'DAILY_LIMIT_EXCEEDED') {
        return const QrPayFailure(
          message: 'Superaste tu límite diario. Podrás pagar de nuevo '
              'mañana. No se descontó nada de tu cuenta.',
          recovery: QrPayRecovery.rescan,
        );
      }
      if (failure == 'INSUFFICIENT_FUNDS') {
        return const QrPayFailure(
          message: 'No tienes saldo suficiente en la fuente elegida. '
              'Elige otra fuente o recarga. Tu dinero está seguro: '
              'no se debitó nada.',
          recovery: QrPayRecovery.retry,
        );
      }
      return QrPayFailure(message: fallback, recovery: QrPayRecovery.retry);
  }
}

bool _containsAny(String haystack, List<String> needles) {
  for (final needle in needles) {
    if (haystack.contains(needle)) return true;
  }
  return false;
}

/// Formatea centimos del servidor a texto exacto (division entera, sin
/// redondeo ni `double`; solo presentacion, sin recalculo de negocio).
String formatQrMinor(int minor, String currency) {
  final symbol = currency.toUpperCase() == 'USD' ? 'US\$' : 'S/';
  final abs = minor.abs();
  final sign = minor < 0 ? '-' : '';
  return '$sign$symbol ${abs ~/ 100}.${(abs % 100).toString().padLeft(2, '0')}';
}
