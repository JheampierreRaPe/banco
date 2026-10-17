/// Modelos del QR personal estatico (E4-T21 / HU14, contrato en
/// `docs/05` §6.5).
///
/// Shapes del backend (envoltorio `docs/05` §4):
/// - `POST /wallet/qr {currency?, concept?}` -> `201 {"data":
///   {charge_id, qr_content, payload, payload_hash, signature,
///   amount_minor: null, currency, concept, mode: "STATIC", status,
///   expires_at: null, created_at, payee_type: "PERSONAL",
///   owner_user_id, payee_account_id, reusable: true}, "meta": {...}}`.
/// - `GET /wallet/qr/{id}` -> `200 {"data": {charge_id, qr_content,
///   status, amount_minor: null, currency, expires_at: null, paid_at?,
///   payee_type, reusable, payee_name_masked}, "meta": {...}}` (solo el
///   titular dueno; ajeno/de comercio -> `404` neutro).
///   `payee_name_masked` (E4-T24, aditivo): nombre + un apellido del
///   titular enmascarado tal cual del servidor (`DIEG••• GONZ•••`);
///   `null`/ausente cuando no se pudo resolver (la UI oculta la fila).
///
/// - `qr_content` (`String`, sobre canonico `BO-QR1` base64url con
///   `amount_minor = 0`) es el contenido escaneable: lo que se
///   renderiza como QR. El pagador digita el monto al escanear.
/// - Sin monto ni caducidad: el QR es reutilizable (multiples cobros).
///
/// Reglas de oro (docs/16 §2.9, docs/19 §4):
/// - Cliente delgado estricto: solo se muestran los valores tal cual
///   del backend. El cliente no firma, no calcula montos/estados ni
///   decide el cobro.
/// - Sin PII/secretos: estos modelos no tienen campos para cuentas en
///   claro ni secretos.
library;

/// QR personal estatico generado (respuesta de `POST /wallet/qr`).
class PersonalQr {
  const PersonalQr({
    required this.chargeId,
    required this.qrContent,
    this.payload = const {},
    required this.payloadHash,
    this.signature,
    this.concept,
    required this.currency,
    required this.mode,
    required this.status,
    this.createdAt,
    required this.ownerUserId,
    this.payeeAccountId,
    this.reusable = true,
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
  final String? concept;
  final String currency;

  /// Siempre `STATIC` (lo decide el backend).
  final String mode;

  /// `ACTIVE` al crear (lo decide el backend; nunca vence).
  final String status;
  final String? createdAt;
  final String ownerUserId;
  final String? payeeAccountId;
  final bool reusable;

  factory PersonalQr.fromJson(Map<String, dynamic> json) => PersonalQr(
        chargeId: json['charge_id']?.toString() ?? '',
        qrContent: _asQrContent(json),
        payload: _asPayloadMap(json['payload']),
        payloadHash: json['payload_hash']?.toString() ?? '',
        signature: json['signature']?.toString(),
        concept: json['concept']?.toString(),
        currency: (json['currency']?.toString() ?? 'PEN').toUpperCase(),
        mode: (json['mode']?.toString() ?? 'STATIC').toUpperCase(),
        status: (json['status']?.toString() ?? '').toUpperCase(),
        createdAt: json['created_at']?.toString(),
        ownerUserId: json['owner_user_id']?.toString() ?? '',
        payeeAccountId: json['payee_account_id']?.toString(),
        reusable: json['reusable'] as bool? ?? true,
      );
}

/// Estado del QR personal (respuesta de `GET /wallet/qr/{id}`).
class PersonalQrStatus {
  const PersonalQrStatus({
    required this.chargeId,
    required this.qrContent,
    required this.status,
    required this.currency,
    this.paidAt,
    this.reusable = true,
    this.payeeNameMasked,
  });

  final String chargeId;
  final String qrContent;

  /// `ACTIVE` (el personal no vence ni se agota).
  final String status;
  final String currency;
  final String? paidAt;
  final bool reusable;

  /// Nombre + un apellido del titular enmascarado tal cual del servidor
  /// (E4-T24, `payee_name_masked`, p. ej. `DIEG••• GONZ•••`); `null`
  /// cuando el servidor no lo pudo resolver (cliente delgado: se muestra
  /// tal cual, sin calcularlo; la UI oculta la fila).
  final String? payeeNameMasked;

  bool get isActive => status == 'ACTIVE';

  /// Etiqueta en español para el badge (presentacion del `status` del
  /// servidor; no decide nada).
  String get statusLabel {
    switch (status) {
      case 'ACTIVE':
        return 'Activo';
      default:
        return status.isEmpty ? 'Sin estado' : status;
    }
  }

  factory PersonalQrStatus.fromJson(Map<String, dynamic> json) =>
      PersonalQrStatus(
        chargeId: json['charge_id']?.toString() ?? '',
        qrContent: _asQrContent(json),
        status: (json['status']?.toString() ?? '').toUpperCase(),
        currency: (json['currency']?.toString() ?? 'PEN').toUpperCase(),
        paidAt: json['paid_at']?.toString(),
        reusable: json['reusable'] as bool? ?? true,
        payeeNameMasked: _asMaskedName(json['payee_name_masked']),
      );
}

/// `payee_name_masked` es opaco para el cliente (E4-T24): se muestra tal
/// cual del servidor. Ausente/`null`/vacio -> `null` (la UI oculta la
/// fila); nunca se calcula ni se completa en el cliente.
String? _asMaskedName(Object? value) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? null : text;
}

/// `qr_content` es el contenido escaneable (`String` base64url
/// `BO-QR1`). Si falta, el contenido queda vacio (no se inventa un QR).
String _asQrContent(Map<String, dynamic> json) {
  final content = json['qr_content'];
  if (content is String && content.isNotEmpty) return content;
  final legacy = json['payload'];
  if (legacy is String) return legacy;
  return '';
}

/// `payload` del backend es un objeto estructurado (dict). Si llega
/// otra forma, se conserva como mapa vacio: el cliente nunca lo trata
/// como contenido escaneable.
Map<String, dynamic> _asPayloadMap(Object? value) {
  if (value is Map<String, dynamic>) return Map<String, dynamic>.from(value);
  if (value is Map) return Map<String, dynamic>.from(value);
  return const {};
}
