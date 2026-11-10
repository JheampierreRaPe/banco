import '../../../core/http/api_client.dart';
import '../models/personal_qr.dart';

/// Contrato del servicio de QR personal (seam para tests con fake).
abstract class PersonalQrServiceBase {
  /// Genera el QR personal estatico (`POST /wallet/qr`).
  ///
  /// [idempotencyKey] es opcional (no mueve dinero): si se pasa, el
  /// Reintentar del mismo intento reutiliza la misma clave (sin
  /// duplicar el cargo); sin clave el servidor crea igual.
  Future<PersonalQr> createPersonalQr({
    String currency = 'PEN',
    String? concept,
    String? idempotencyKey,
  });

  /// Consulta el estado del QR personal (`GET /wallet/qr/{id}`; solo
  /// el titular dueno). Solo lectura.
  Future<PersonalQrStatus> getPersonalQrStatus(String chargeId);
}

/// Servicio de QR personal sobre [ApiClient] (docs/05 §6.5, E4-T21).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente):
/// - `POST /wallet/qr` (+ `Idempotency-Key` opcional) ->
///   `201 {"data": {charge_id, qr_content, ...}}` (sin monto ni
///   caducidad; reutilizable).
/// - `GET /wallet/qr/{id}` (sin `Idempotency-Key`: no mueve dinero,
///   solo lectura) -> `200 {"data": {charge_id, status, ...}}`
class PersonalQrService implements PersonalQrServiceBase {
  PersonalQrService({required this.api});

  final ApiClient api;

  static PersonalQr _parseQr(Object? data) {
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return PersonalQr.fromJson(Map<String, dynamic>.from(data['data'] as Map));
    }
    if (data is Map<String, dynamic>) return PersonalQr.fromJson(data);
    throw const FormatException('Respuesta de QR personal invalida');
  }

  static PersonalQrStatus _parseStatus(Object? data) {
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return PersonalQrStatus.fromJson(
        Map<String, dynamic>.from(data['data'] as Map),
      );
    }
    if (data is Map<String, dynamic>) return PersonalQrStatus.fromJson(data);
    throw const FormatException('Respuesta de estado de QR personal invalida');
  }

  @override
  Future<PersonalQr> createPersonalQr({
    String currency = 'PEN',
    String? concept,
    String? idempotencyKey,
  }) async {
    final body = {
      'currency': currency,
      if (concept != null && concept.isNotEmpty) 'concept': concept,
    };
    if (idempotencyKey == null) {
      final resp = await api.post('/wallet/qr', data: body);
      return _parseQr(resp.data);
    }
    final resp = await api.postWithIdempotency(
      '/wallet/qr',
      data: body,
      idempotencyKey: idempotencyKey,
    );
    return _parseQr(resp.data);
  }

  @override
  Future<PersonalQrStatus> getPersonalQrStatus(String chargeId) async {
    final resp = await api.get('/wallet/qr/$chargeId');
    return _parseStatus(resp.data);
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `walletServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con
/// el `ApiClient` compartido; `null` = DI no configurada: las pantallas
/// muestran un error accionable en vez de romper.
typedef PersonalQrServiceFactory = PersonalQrServiceBase Function();

PersonalQrServiceFactory? personalQrServiceFactory;
