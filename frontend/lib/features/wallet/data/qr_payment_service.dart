import '../../../core/http/api_client.dart';
import '../models/qr_payment.dart';

/// Contrato del servicio de pago QR del pagador (seam para tests con
/// fake).
abstract class QrPaymentServiceBase {
  /// Paga el QR escaneado (`POST /payments/qr`, E4-T12 en `docs/05`
  /// §6.5).
  ///
  /// - [qrContent]: sobre canonico `BO-QR1` tal cual escaneado (crudo;
  ///   el backend decodifica y verifica la firma, el cliente no).
  /// - [linkId]: UUID del vinculo `ACCOUNT` a debitar (de `GET
  ///   /wallet`; solo del titular).
  /// - [idempotencyKey]: clave del intento (obligatoria en el contrato;
  ///   el Reintentar del mismo intento reutiliza la misma clave, sin
  ///   duplicar el pago).
  Future<QrPaymentResult> payQr({
    required String qrContent,
    required String linkId,
    String? idempotencyKey,
  });

  /// Previsualiza el cargo QR antes de confirmar
  /// (`POST /payments/qr/preview`, fix MEDIO-3, `docs/05` §6.5).
  ///
  /// Solo lectura (sin `Idempotency-Key`, no mueve dinero): devuelve
  /// `comercio`/`monto` tal cual del backend. `422` = QR
  /// vencido/corrupto/manipulado/ajeno; `409` = ya cobrado.
  Future<QrChargePreview> previewQr({required String qrContent});
}

/// Servicio de pago QR sobre [ApiClient] (E4-T13, `docs/05` §6.5).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente):
/// - `POST /payments/qr/preview` (`{qr_content}`, sin `Idempotency-Key`,
///   solo lectura) -> `200 {"data": {charge_id, merchant, amount_minor,
///   currency, expires_at, status}}` (fix MEDIO-3).
/// - `POST /payments/qr` (+ `Idempotency-Key` obligatoria) ->
///   `201 {"data": {transaction_id, qr_payment_id, charge_id, merchant,
///   amount_minor, currency, status, failure_code?, authorization?},
///   "meta": {request_id, replayed}}`.
/// - La autorizacion del reto (`PENDING_AUTHORIZATION`) vive en
///   `PaymentAuthorizationService` (`POST /transactions/{id}/authorize`,
///   firma o PIN, E2-T19/E2-T31, patron F-T03): este servicio no la
///   duplica.
///
/// > Sin PII: el `qr_content` viaja solo en el cuerpo del request; nunca
/// > se escribe en logs (este servicio no loguea cuerpos).
class QrPaymentService implements QrPaymentServiceBase {
  QrPaymentService({required this.api});

  final ApiClient api;

  static QrPaymentResult _parseResult(Object? data) {
    Map<String, dynamic> asMap(Object? value) {
      if (value is Map<String, dynamic>) return value;
      if (value is Map) return Map<String, dynamic>.from(value);
      throw const FormatException('Respuesta de pago QR invalida');
    }

    if (data is Map<String, dynamic> && data['data'] is Map) {
      return QrPaymentResult.fromJson(
        Map<String, dynamic>.from(data['data'] as Map),
        replayed: (data['meta'] is Map) &&
            (data['meta'] as Map)['replayed'] == true,
      );
    }
    if (data is Map<String, dynamic>) {
      return QrPaymentResult.fromJson(asMap(data));
    }
    throw const FormatException('Respuesta de pago QR invalida');
  }

  @override
  Future<QrPaymentResult> payQr({
    required String qrContent,
    required String linkId,
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/payments/qr',
      data: {'qr_content': qrContent, 'link_id': linkId},
      idempotencyKey: idempotencyKey,
    );
    return _parseResult(resp.data);
  }

  @override
  Future<QrChargePreview> previewQr({required String qrContent}) async {
    final resp = await api.post(
      '/payments/qr/preview',
      data: {'qr_content': qrContent},
    );
    final data = resp.data;
    if (data is Map<String, dynamic> && data['data'] is Map) {
      final inner = data['data'];
      if (inner is Map<String, dynamic>) {
        return QrChargePreview.fromJson(inner);
      }
      return QrChargePreview.fromJson(
        Map<String, dynamic>.from(inner as Map),
      );
    }
    if (data is Map<String, dynamic>) {
      if (data['data'] is Map) {
        return QrChargePreview.fromJson(
          Map<String, dynamic>.from(data['data'] as Map),
        );
      }
      return QrChargePreview.fromJson(data);
    }
    throw const FormatException('Respuesta de preview QR invalida');
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `walletServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con
/// el `ApiClient` compartido; `null` = DI no configurada: las pantallas
/// muestran un error accionable en vez de romper.
typedef QrPaymentServiceFactory = QrPaymentServiceBase Function();

QrPaymentServiceFactory? qrPaymentServiceFactory;
