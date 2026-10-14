/// Modelos del area comercio QR (E4-T09 / HU14, contrato E4-T08 en
/// `docs/05` §6.5).
///
/// Shapes del backend (envoltorio `docs/05` §4):
/// - `POST /merchants/qr` -> `201 {"data": {charge_id, qr_content,
///   payload, payload_hash, signature, amount_minor, currency, concept,
///   mode, status ("ACTIVE"), expires_at, created_at}, "meta": {...}}`.
/// - `GET /merchants/qr/{id}` -> `200 {"data": {charge_id, qr_content,
///   status, amount_minor, currency, expires_at, paid_at?},
///   "meta": {...}}`.
///   El `status` es efectivo on-read: un `ACTIVE` vencido se reporta
///   `EXPIRED` (E4-T07).
///
/// - `qr_content` (`String`, sobre canonico `BO-QR1` base64url) es el
///   contenido escaneable: lo que se renderiza como QR y lo que un
///   lector conforme (HU15) decodifica/verifica.
/// - `payload` es un objeto estructurado (dict); NO es el contenido del
///   QR y nunca se usa como `String` escaneable.
///
/// Reglas de oro (docs/16 §2.9, docs/19 §4, docs/13 §1.2):
/// - El secreto HMAC y el RUC jamas viajan en respuestas: estos modelos
///   ni siquiera tienen campos para ellos.
/// - Cliente delgado estricto: solo se muestran los valores tal cual del
///   backend (`payload`/`status`/`expires_at`/`amount_minor`). El cliente
///   no firma, no calcula montos/estados ni decide el cobro. El formateo
///   `minor -> texto` es presentacion exacta (division entera, sin
///   redondeo ni `double`).
library;

/// Entrada para generar un cobro (`POST /merchants/qr`).
///
/// - `amountMinor` entero > 0 (centimos; el backend valida).
/// - `currency` ISO-4217 (default `PEN`, lo decide el backend si falta).
/// - `concept` opcional (<= 120; el backend valida).
/// - `mode` `DYNAMIC` (default, con `expires_at`) | `STATIC` (sin
///   vencimiento).
class QrChargeInput {
  const QrChargeInput({
    required this.amountMinor,
    this.currency = 'PEN',
    this.concept,
    this.mode = 'DYNAMIC',
  });

  final int amountMinor;
  final String currency;
  final String? concept;
  final String mode;

  Map<String, dynamic> toJson() {
    final concept = this.concept;
    return {
      'amount_minor': amountMinor,
      'currency': currency,
      if (concept != null && concept.isNotEmpty) 'concept': concept,
      'mode': mode,
    };
  }
}

/// Cobro QR generado (respuesta de `POST /merchants/qr`, tal cual del
/// backend).
class QrCharge {
  const QrCharge({
    required this.chargeId,
    required this.qrContent,
    this.payload = const {},
    required this.payloadHash,
    this.signature,
    required this.amountMinor,
    required this.currency,
    this.concept,
    required this.mode,
    required this.status,
    this.expiresAt,
    this.createdAt,
  });

  final String chargeId;

  /// Sobre canonico `BO-QR1` (base64url, `String`): contenido escaneable
  /// del QR. La app solo lo renderiza, nunca lo firma ni lo interpreta.
  final String qrContent;

  /// Payload estructurado del backend (objeto/dict, opaco para el
  /// cliente; no escaneable, no se renderiza como QR).
  final Map<String, dynamic> payload;
  final String payloadHash;
  final String? signature;
  final int amountMinor;
  final String currency;
  final String? concept;
  final String mode;

  /// `ACTIVE` al crear (lo decide el backend).
  final String status;
  final String? expiresAt;
  final String? createdAt;

  factory QrCharge.fromJson(Map<String, dynamic> json) => QrCharge(
        chargeId: json['charge_id']?.toString() ?? '',
        qrContent: _asQrContent(json),
        payload: _asPayloadMap(json['payload']),
        payloadHash: json['payload_hash']?.toString() ?? '',
        signature: json['signature']?.toString(),
        amountMinor: _asInt(json['amount_minor']),
        currency: (json['currency']?.toString() ?? 'PEN').toUpperCase(),
        concept: json['concept']?.toString(),
        mode: (json['mode']?.toString() ?? 'DYNAMIC').toUpperCase(),
        status: (json['status']?.toString() ?? '').toUpperCase(),
        expiresAt: json['expires_at']?.toString(),
        createdAt: json['created_at']?.toString(),
      );
}

/// Estado resumido del cobro (respuesta de `GET /merchants/qr/{id}`, tal
/// cual del backend; fuente de verdad del estado).
class QrChargeStatus {
  const QrChargeStatus({
    required this.chargeId,
    required this.status,
    required this.amountMinor,
    required this.currency,
    this.expiresAt,
    this.paidAt,
  });

  final String chargeId;

  /// `ACTIVE` | `EXPIRED` | `PAID` (efectivo on-read; lo decide el
  /// backend, E4-T07/E4-T08).
  final String status;
  final int amountMinor;
  final String currency;
  final String? expiresAt;
  final String? paidAt;

  bool get isActive => status == 'ACTIVE';
  bool get isPaid => status == 'PAID';
  bool get isExpired => status == 'EXPIRED';

  /// Etiqueta en español para el badge (presentacion del `status` del
  /// servidor; no decide nada).
  String get statusLabel {
    switch (status) {
      case 'ACTIVE':
        return 'Pendiente';
      case 'PAID':
        return 'Pagado';
      case 'EXPIRED':
        return 'Vencido';
      default:
        return status.isEmpty ? 'Sin estado' : status;
    }
  }

  factory QrChargeStatus.fromJson(Map<String, dynamic> json) =>
      QrChargeStatus(
        chargeId: json['charge_id']?.toString() ?? '',
        status: (json['status']?.toString() ?? '').toUpperCase(),
        amountMinor: _asInt(json['amount_minor']),
        currency: (json['currency']?.toString() ?? 'PEN').toUpperCase(),
        expiresAt: json['expires_at']?.toString(),
        paidAt: json['paid_at']?.toString(),
      );
}

int _asInt(Object? value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  return int.tryParse(value?.toString() ?? '') ?? 0;
}

/// `qr_content` es el contenido escaneable (`String` base64url
/// `BO-QR1`). Compatibilidad: si falta (respuestas antiguas), usa el
/// `payload` cuando venia como `String`; si el `payload` es objeto,
/// el contenido queda vacio (no se inventa un QR).
String _asQrContent(Map<String, dynamic> json) {
  final content = json['qr_content'];
  if (content is String && content.isNotEmpty) return content;
  final legacy = json['payload'];
  if (legacy is String) return legacy;
  return '';
}

/// `payload` del backend es un objeto estructurado (dict). Si llega
/// otra forma (legado como `String`), se conserva como mapa vacio: el
/// cliente nunca lo trata como contenido escaneable.
Map<String, dynamic> _asPayloadMap(Object? value) {
  if (value is Map<String, dynamic>) return Map<String, dynamic>.from(value);
  if (value is Map) return Map<String, dynamic>.from(value);
  return const {};
}

/// Parsea un monto en soles tecleado (`25.50`, `25,50`, `100`) a centimos
/// exactos sin `double` (solo codificacion de transporte para el request;
/// no es calculo de negocio: el backend valida y decide).
///
/// Devuelve `null` si el formato no es valido.
int? parseSolesToMinor(String raw) {
  final cleaned = raw.trim().replaceAll(' ', '');
  final match =
      RegExp(r'^(\d+)([.,](\d{1,2}))?$').firstMatch(cleaned);
  if (match == null) return null;
  final soles = int.parse(match.group(1)!);
  final fraction = (match.group(3) ?? '').padRight(2, '0');
  return soles * 100 + int.parse(fraction);
}

/// Formatea centimos del servidor a texto exacto (division entera, sin
/// redondeo ni `double`; solo presentacion).
String formatMinor(int minor, String currency) {
  final symbol = currency.toUpperCase() == 'USD' ? 'US\$' : 'S/';
  final abs = minor.abs();
  final sign = minor < 0 ? '-' : '';
  return '$sign$symbol ${abs ~/ 100}.${(abs % 100).toString().padLeft(2, '0')}';
}

/// Formatea un ISO-8601 del servidor a fecha/hora local legible; si no
/// hay valor (cobro `STATIC`), indica que no vence. Si el texto no
/// parsea, se devuelve tal cual (nunca se inventa una fecha).
String formatIsoLocal(String? iso) {
  if (iso == null || iso.isEmpty) return 'Sin vencimiento';
  try {
    final dt = DateTime.parse(iso).toLocal();
    String pad(int n) => n.toString().padLeft(2, '0');
    return '${pad(dt.day)}/${pad(dt.month)}/${dt.year} '
        '${pad(dt.hour)}:${pad(dt.minute)}';
  } catch (_) {
    return iso;
  }
}

/// Texto visual de la vigencia restante (meramente informativo: la fuente
/// de verdad es el `status`/`expires_at` del backend; el cliente no
/// decide el vencimiento).
String remainingText(String? iso) {
  if (iso == null || iso.isEmpty) {
    return 'Cobro estático: no vence.';
  }
  try {
    final diff = DateTime.parse(iso).toLocal().difference(DateTime.now());
    if (diff.isNegative) {
      return 'Hora local vencida: actualiza el estado para confirmar.';
    }
    if (diff.inHours >= 1) {
      return 'Vence en ~${diff.inHours} h '
          '(visual; el estado lo confirma el servidor).';
    }
    return 'Vence en ~${diff.inMinutes} min ${diff.inSeconds % 60} s '
        '(visual; el estado lo confirma el servidor).';
  } catch (_) {
    return '';
  }
}
