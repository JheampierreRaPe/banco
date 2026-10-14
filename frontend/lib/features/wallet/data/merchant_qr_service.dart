import '../../../core/http/api_client.dart';
import '../models/merchant_qr.dart';

/// Contrato del servicio de cobros QR del comercio (seam para tests con
/// fake).
abstract class MerchantQrServiceBase {
  /// Genera un cobro (`POST /merchants/qr`).
  ///
  /// [idempotencyKey] es opcional (contrato E4-T08, patron `E2-T13`): si
  /// se pasa, el Reintentar del mismo intento reutiliza la misma clave
  /// (sin duplicar el cargo); sin clave el servidor crea igual.
  Future<QrCharge> createCharge(
    QrChargeInput input, {
    String? idempotencyKey,
  });

  /// Consulta el estado efectivo del cobro (`GET /merchants/qr/{id}`;
  /// on-read `EXPIRED` si vencio, E4-T07). Solo lectura.
  Future<QrChargeStatus> getChargeStatus(String chargeId);
}

/// Servicio de cobros QR sobre [ApiClient] (docs/05 §6.5, E4-T09).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente):
/// - `POST /merchants/qr` (+ `Idempotency-Key` opcional) ->
///   `201 {"data": {charge_id, payload, ...}}`
/// - `GET /merchants/qr/{id}` (sin `Idempotency-Key`: no mueve dinero,
///   solo lectura) -> `200 {"data": {charge_id, status, ...}}`
class MerchantQrService implements MerchantQrServiceBase {
  MerchantQrService({required this.api});

  final ApiClient api;

  static QrCharge _parseCharge(Object? data) {
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return QrCharge.fromJson(Map<String, dynamic>.from(data['data'] as Map));
    }
    if (data is Map<String, dynamic>) return QrCharge.fromJson(data);
    throw const FormatException('Respuesta de cobro QR invalida');
  }

  static QrChargeStatus _parseStatus(Object? data) {
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return QrChargeStatus.fromJson(
        Map<String, dynamic>.from(data['data'] as Map),
      );
    }
    if (data is Map<String, dynamic>) return QrChargeStatus.fromJson(data);
    throw const FormatException('Respuesta de estado de cobro invalida');
  }

  @override
  Future<QrCharge> createCharge(
    QrChargeInput input, {
    String? idempotencyKey,
  }) async {
    // `Idempotency-Key` opcional (E4-T08 decision 1): solo se envia si el
    // llamador la provee (el Reintentar del mismo intento la reutiliza).
    // `POST /merchants/qr` no mueve dinero: crea el cargo firmado.
    if (idempotencyKey == null) {
      final resp = await api.post('/merchants/qr', data: input.toJson());
      return _parseCharge(resp.data);
    }
    final resp = await api.postWithIdempotency(
      '/merchants/qr',
      data: input.toJson(),
      idempotencyKey: idempotencyKey,
    );
    return _parseCharge(resp.data);
  }

  @override
  Future<QrChargeStatus> getChargeStatus(String chargeId) async {
    final resp = await api.get('/merchants/qr/$chargeId');
    return _parseStatus(resp.data);
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `walletServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con
/// el `ApiClient` compartido; `null` = DI no configurada: las pantallas
/// muestran un error accionable en vez de romper.
typedef MerchantQrServiceFactory = MerchantQrServiceBase Function();

MerchantQrServiceFactory? merchantQrServiceFactory;
