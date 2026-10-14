/// Modelos del feature `notifications` (E6-T15 / HU22, contrato
/// `docs/05` §6.9).
///
/// Shapes del backend (base `/api/v1` ya incluida en el cliente):
/// - `GET /notifications` -> `200 {"data": [{id, channel, template_code,
///   status, created_at, read_at, device, location, reference}], "meta":
///   {page, page_size, total, request_id}}` (metadatos sin `payload_json` ni
///   PII; mas recientes primero; `device`/`location`/`reference` son los
///   campos de render acotados de la alerta — etiquetas saneadas sin PII,
///   `null` en filas que no los traen, fix A2).
/// - `GET /notifications/{id}` -> `200 {"data": {...misma vista acotada...},
///   "meta": {request_id}}` (detalle read-only de UNA notificacion propia;
///   ajena/inexistente -> `404` neutro; id malformado -> `422`).
/// - `POST /notifications/{id}/read` -> `200 {"data": {...}, "meta": ...}`
///   (fija `read_at`; idempotente).
/// - `POST /notifications/{id}/report` body opcional `{reason?}` ->
///   `200 {"data": {reported, replayed, notification_id, confirmation_id,
///   reference}, "meta": ...}` (idempotente por estado).
/// - `GET /notifications/preferences` -> `200 {"data": {push, email, sms}}`
///   (defaults `true`/`true`/`false` si aun no hay fila).
/// - `PUT /notifications/preferences` body `{push, email, sms}` ->
///   `200` con las preferencias guardadas.
///
/// Reglas (docs/19 cliente delgado, docs/20 §8):
/// - Sin PII: estos modelos solo llevan metadatos tal cual del backend
///   (dispositivo/hora/ubicacion aproximada cuando el servidor los
///   incluya en el item; nunca coordenadas exactas ni datos de otro
///   cliente). El cliente no decide bloqueos ni arma alertas.
library;

/// Alerta de seguridad del titular (metadatos enmascarados).
class NotificationItem {
  const NotificationItem({
    required this.id,
    required this.channel,
    required this.templateCode,
    required this.status,
    required this.createdAt,
    this.readAt,
    this.device,
    this.location,
    this.reference,
  });

  final String id;
  final String channel;
  final String templateCode;
  final String status;
  final String createdAt;
  final String? readAt;

  /// Dispositivo tal cual del backend (cuando lo incluye).
  final String? device;

  /// Ubicacion aproximada tal cual del backend (nunca exacta).
  final String? location;

  /// Referencia no PII del evento (para el reporte "no reconozco").
  final String? reference;

  bool get isRead => readAt != null && readAt!.isNotEmpty;

  factory NotificationItem.fromJson(Map<String, dynamic> json) =>
      NotificationItem(
        id: json['id']?.toString() ?? '',
        channel: (json['channel']?.toString() ?? '').toLowerCase(),
        templateCode: json['template_code']?.toString() ?? '',
        status: (json['status']?.toString() ?? '').toUpperCase(),
        createdAt: json['created_at']?.toString() ?? '',
        readAt: json['read_at']?.toString(),
        // Contrato real `docs/05` §6.9 (fix A2): solo `device`/`location`/
        // `reference` (opcionales, `null` en filas viejas). Sin alias.
        device: json['device']?.toString(),
        location: json['location']?.toString(),
        reference: json['reference']?.toString(),
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'channel': channel,
        'template_code': templateCode,
        'status': status,
        'created_at': createdAt,
        if (readAt != null) 'read_at': readAt,
        if (device != null) 'device': device,
        if (location != null) 'location': location,
        if (reference != null) 'reference': reference,
      };
}

/// Pagina del centro (tal cual del backend; sin recalculo).
class NotificationPage {
  const NotificationPage({
    required this.items,
    required this.page,
    required this.pageSize,
    required this.total,
  });

  final List<NotificationItem> items;
  final int page;
  final int pageSize;
  final int total;

  static const empty = NotificationPage(
    items: [],
    page: 1,
    pageSize: 20,
    total: 0,
  );

  factory NotificationPage.fromJson(Map<String, dynamic> json) {
    final raw = json['data'];
    final items = raw is List
        ? raw
            .whereType<Map>()
            .map((e) => NotificationItem.fromJson(
                  Map<String, dynamic>.from(e),
                ))
            .toList()
        : <NotificationItem>[];
    final meta = json['meta'] is Map
        ? Map<String, dynamic>.from(json['meta'] as Map)
        : <String, dynamic>{};
    int asInt(Object? v, int fallback) {
      if (v is int) return v;
      return int.tryParse(v?.toString() ?? '') ?? fallback;
    }

    return NotificationPage(
      items: items,
      page: asInt(meta['page'], 1),
      pageSize: asInt(meta['page_size'], items.length),
      total: asInt(meta['total'], items.length),
    );
  }
}

/// Preferencias de canal del titular (tal cual del backend).
class ChannelPreferences {
  const ChannelPreferences({
    required this.push,
    required this.email,
    required this.sms,
  });

  final bool push;
  final bool email;
  final bool sms;

  factory ChannelPreferences.fromJson(Map<String, dynamic> json) {
    final data = json['data'] is Map
        ? Map<String, dynamic>.from(json['data'] as Map)
        : json;
    bool asBool(Object? v, bool fallback) {
      if (v is bool) return v;
      final s = v?.toString().toLowerCase();
      if (s == 'true' || s == '1') return true;
      if (s == 'false' || s == '0') return false;
      return fallback;
    }

    return ChannelPreferences(
      push: asBool(data['push'], true),
      email: asBool(data['email'], true),
      sms: asBool(data['sms'], false),
    );
  }

  Map<String, dynamic> toJson() => {
        'push': push,
        'email': email,
        'sms': sms,
      };
}

/// Resultado del reporte "no reconozco" (lo decide el servidor).
class ReportResult {
  const ReportResult({
    required this.reported,
    required this.replayed,
    required this.notificationId,
    this.confirmationId,
    this.reference,
  });

  final bool reported;
  final bool replayed;
  final String notificationId;
  final String? confirmationId;
  final String? reference;

  factory ReportResult.fromJson(Map<String, dynamic> json) {
    final data = json['data'] is Map
        ? Map<String, dynamic>.from(json['data'] as Map)
        : json;
    bool asBool(Object? v) => v is bool ? v : v?.toString() == 'true';
    return ReportResult(
      reported: asBool(data['reported']),
      replayed: asBool(data['replayed']),
      notificationId: data['notification_id']?.toString() ?? '',
      confirmationId: data['confirmation_id']?.toString(),
      reference: data['reference']?.toString(),
    );
  }
}
