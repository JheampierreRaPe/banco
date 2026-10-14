import 'package:dio/dio.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';
import '../models/notification.dart';

/// Contrato del servicio de notificaciones (seam para tests con fake).
abstract class NotificationsServiceBase {
  /// Centro del titular (`GET /notifications`; filtros `channel`/`unread`).
  Future<NotificationPage> list({
    String? channel,
    bool unreadOnly = false,
    int page = 1,
    int pageSize = 20,
  });

  /// Detalle read-only de UNA notificacion propia
  /// (`GET /notifications/{id}`; solo titular; ajena/inexistente ->
  /// `404` neutro; id malformado -> `422`).
  Future<NotificationItem> detail(String id);

  /// Marca una notificacion propia como leida (`POST /.../read`;
  /// idempotente: repetir devuelve el mismo `read_at`).
  Future<NotificationItem> markRead(String id);

  /// Preferencias de canal (`GET /notifications/preferences`).
  Future<ChannelPreferences> getPreferences();

  /// Actualiza preferencias (`PUT /notifications/preferences` con los tres
  /// booleanos obligatorios; idempotente por `user_id`).
  Future<ChannelPreferences> updatePreferences(ChannelPreferences prefs);

  /// Reporta "no reconozco esta operacion" (`POST /.../report`, body
  /// opcional `{reason?}`, max 280; idempotente por estado:
  /// repetir devuelve `replayed: true`).
  Future<ReportResult> report(String id, {String? reason});
}

/// Servicio de notificaciones sobre [ApiClient] (`docs/05` §6.9, E6-T15).
///
/// Cliente delgado (docs/19 §4): solo captura/muestra y envia; no decide
/// bloqueos, no calcula estados, no arma alertas ni guarda notificaciones
/// localmente. Sin conexion al servidor no se opera.
class NotificationsService implements NotificationsServiceBase {
  NotificationsService({required this.api});

  final ApiClient api;

  static NotificationPage _parsePage(Object? data) {
    if (data is Map<String, dynamic>) {
      return NotificationPage.fromJson(data);
    }
    throw const FormatException('Respuesta de notificaciones invalida');
  }

  static NotificationItem _parseItem(Object? data) {
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return NotificationItem.fromJson(
        Map<String, dynamic>.from(data['data'] as Map),
      );
    }
    if (data is Map<String, dynamic>) {
      return NotificationItem.fromJson(data);
    }
    throw const FormatException('Respuesta de notificacion invalida');
  }

  static ChannelPreferences _parsePrefs(Object? data) {
    if (data is Map<String, dynamic>) {
      return ChannelPreferences.fromJson(data);
    }
    throw const FormatException('Respuesta de preferencias invalida');
  }

  static ReportResult _parseReport(Object? data) {
    if (data is Map<String, dynamic>) {
      return ReportResult.fromJson(data);
    }
    throw const FormatException('Respuesta de reporte invalida');
  }

  @override
  Future<NotificationPage> list({
    String? channel,
    bool unreadOnly = false,
    int page = 1,
    int pageSize = 20,
  }) async {
    final query = <String, dynamic>{
      'page': page,
      'page_size': pageSize,
      if (channel != null && channel.isNotEmpty) 'channel': channel,
      if (unreadOnly) 'unread': 'true',
    };
    final resp = await api.get('/notifications', queryParameters: query);
    return _parsePage(resp.data);
  }

  @override
  Future<NotificationItem> markRead(String id) async {
    final resp = await api.post('/notifications/$id/read');
    return _parseItem(resp.data);
  }

  @override
  Future<NotificationItem> detail(String id) async {
    final resp = await api.get('/notifications/$id');
    return _parseItem(resp.data);
  }

  @override
  Future<ChannelPreferences> getPreferences() async {
    final resp = await api.get('/notifications/preferences');
    return _parsePrefs(resp.data);
  }

  @override
  Future<ChannelPreferences> updatePreferences(
    ChannelPreferences prefs,
  ) async {
    try {
      final resp = await api.dio.put(
        '/notifications/preferences',
        data: prefs.toJson(),
      );
      return _parsePrefs(resp.data);
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
  }

  @override
  Future<ReportResult> report(String id, {String? reason}) async {
    final body = <String, dynamic>{
      if (reason != null && reason.isNotEmpty) 'reason': reason,
    };
    final resp = await api.post('/notifications/$id/report', data: body);
    return _parseReport(resp.data);
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `walletServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con
/// el `ApiClient` compartido; `null` = DI no configurada: las pantallas
/// muestran un error accionable en vez de romper.
typedef NotificationsServiceFactory = NotificationsServiceBase Function();

NotificationsServiceFactory? notificationsServiceFactory;
