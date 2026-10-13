// Servicio de autorizacion de pagos sensibles (E2-T21 + E2-T31, HU08).
//
// Contrato (docs/05 §6.3, E2-T19 + challenge de reintento + PIN E2-T31):
// - `POST /transactions/{id}/authorize` (+ `Idempotency-Key`) con
//   `{device_id, signature[, nonce]}` (firma, E2-T19) o `{pin}` (PIN del
//   titular, E2-T31, mutuamente excluyente) ->
//   `200 {data: {transaction_id, status, tracking, failure_code},
//   meta: {request_id, replayed}}`.
// - `POST /transactions/{id}/authorize/challenge` (sin `Idempotency-Key`:
//   solo rota el reto, no mueve dinero) ->
//   `200 {data: {transaction_id, nonce, expires_in}, meta: {request_id}}`
//   para reintentar tras `EXPIRED_NONCE` con una `Idempotency-Key` NUEVA
//   (la anterior snapshotteo el fallo: misma clave + cuerpo distinto
//   = `409`; `GET /transactions/{id}` solo relee el `nonce` almacenado
//   sin rotarlo y no sirve para reintentar).
//
// Cliente delgado (docs/19 §4): captura, envia y presenta; no decide
// umbrales (el reto lo dispara el backend con `PENDING_AUTHORIZATION`).
// La firma del `nonce` la produce `BiometricService.signNonce` (F-T03);
// este servicio solo la transporta. Sin PII ni firmas en logs.
library;

import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';
import '../../transfers/models/transfer.dart';

/// Resultado de `POST /transactions/{id}/authorize`.
class AuthorizationResult {
  const AuthorizationResult({
    required this.transactionId,
    required this.status,
    this.tracking = const {},
    this.failureCode,
    this.replayed = false,
  });

  final String transactionId;
  final String status;
  final Map<String, dynamic> tracking;
  final String? failureCode;
  final bool replayed;

  /// `true` cuando el backend cerro la operacion como rechazada sin mover
  /// dinero (p. ej. bloqueo preventivo tras N fallos).
  bool get isRejected => status.toUpperCase() == 'REJECTED';

  /// `true` cuando el bloqueo preventivo gano (`REJECTED` +
  /// `BIOMETRIC_PREVENTIVE_BLOCK`).
  bool get isPreventiveBlock =>
      isRejected && failureCode == 'BIOMETRIC_PREVENTIVE_BLOCK';

  String get holderMasked => tracking['holder_name_masked']?.toString() ?? '';

  factory AuthorizationResult.fromJson(
    Map<String, dynamic> json, {
    bool replayed = false,
  }) {
    Map<String, dynamic> trackingOf(Object? value) {
      if (value is Map) return Map<String, dynamic>.from(value);
      return const {};
    }

    return AuthorizationResult(
      transactionId: json['transaction_id']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      tracking: trackingOf(json['tracking']),
      failureCode: json['failure_code']?.toString(),
      replayed: replayed,
    );
  }
}

/// Contrato del servicio de autorizacion (seam para tests con fake).
abstract class PaymentAuthorizationServiceBase {
  /// Autoriza la transaccion firmando el reto (`POST .../authorize`).
  ///
  /// [idempotencyKey] es la clave del intento: el primer intento usa la del
  /// pago; el reintento tras `EXPIRED_NONCE` usa una NUEVA (derivada por el
  /// controlador) porque el fallo previo quedo snapshotteado bajo la
  /// anterior (misma clave + cuerpo distinto = `409`).
  Future<AuthorizationResult> authorize({
    required String transactionId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  });

  /// Autoriza la transaccion con el PIN del titular (`POST .../authorize`
  /// con `{pin}`, E2-T31, alternativa a la firma).
  ///
  /// [idempotencyKey] sigue las mismas reglas que en [authorize] (misma
  /// clave + mismo cuerpo = replay; cuerpo distinto = `409`).
  Future<AuthorizationResult> authorizeWithPin({
    required String transactionId,
    required String pin,
    String? idempotencyKey,
  });

  /// Rota el reto (`POST .../authorize/challenge`) para reintentar tras
  /// `EXPIRED_NONCE` (devuelve el `nonce` nuevo; conserva
  /// `failed_attempts`). Sin `Idempotency-Key`: solo rota, no mueve dinero.
  Future<TransactionDetail> refreshChallenge(String transactionId);
}

/// Servicio HTTP de autorizacion sobre [ApiClient] (E2-T21, docs/05 §6.3).
class PaymentAuthorizationService implements PaymentAuthorizationServiceBase {
  PaymentAuthorizationService({required this.api});

  final ApiClient api;

  static Map<String, dynamic> _dataEnvelope(Object? body) {
    if (body is Map<String, dynamic> && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    if (body is Map<String, dynamic>) return body;
    throw const FormatException('Respuesta de autorizacion invalida');
  }

  static bool _replayed(Object? body) {
    if (body is Map<String, dynamic> && body['meta'] is Map) {
      return (body['meta'] as Map)['replayed'] == true;
    }
    return false;
  }

  @override
  Future<AuthorizationResult> authorize({
    required String transactionId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/transactions/$transactionId/authorize',
      data: {'device_id': deviceId, 'signature': signature, 'nonce': nonce},
      idempotencyKey: idempotencyKey,
    );
    return AuthorizationResult.fromJson(
      _dataEnvelope(resp.data),
      replayed: _replayed(resp.data),
    );
  }

  @override
  Future<AuthorizationResult> authorizeWithPin({
    required String transactionId,
    required String pin,
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/transactions/$transactionId/authorize',
      data: {'pin': pin},
      idempotencyKey: idempotencyKey,
    );
    return AuthorizationResult.fromJson(
      _dataEnvelope(resp.data),
      replayed: _replayed(resp.data),
    );
  }

  @override
  Future<TransactionDetail> refreshChallenge(String transactionId) async {
    final resp = await api.post(
      '/transactions/$transactionId/authorize/challenge',
    );
    return TransactionDetail.fromJson(_dataEnvelope(resp.data));
  }
}

/// Fabrica inyectable (mismo patron que `transfersServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con
/// el `ApiClient` compartido; `null` = DI no configurada.
typedef PaymentAuthorizationServiceFactory =
    PaymentAuthorizationServiceBase Function();

PaymentAuthorizationServiceFactory? paymentAuthorizationServiceFactory;

/// Mensaje UI en espanol para un [ApiException] de autorizacion (E2-T21).
///
/// Sin tecnicismos: el codigo viaja solo en el detalle plegable de
/// [PaymentErrorView]. Sin PII: nunca incluye firmas, nonces ni cuerpos.
String paymentAuthorizationUserMessage(ApiException error) {
  switch (error.code) {
    case 'EXPIRED_NONCE':
      return 'La solicitud expiro. Vuelve a intentarlo con una '
          'nueva autorizacion.';
    case 'NOT_AUTHORIZED':
      return 'No pudimos verificar tu identidad. Intentalo de nuevo o '
          'usa tu PIN de seguridad.';
    case 'DAILY_LIMIT_EXCEEDED':
      return 'Superaste tu limite diario. Podras operar de nuevo manana.';
    case 'BIOMETRIC_PREVENTIVE_BLOCK':
      return 'Protegimos tu cuenta temporalmente. Verifica tu identidad '
          'para continuar.';
    case 'INSUFFICIENT_FUNDS':
      return 'No tienes saldo suficiente para completar la operacion. '
          'Tu dinero esta seguro: no se debito nada.';
    case 'TRANSFER_TIMEOUT':
    case 'INTERBANK_UNAVAILABLE':
    case 'INTERBANK_REJECTED':
      return 'La operacion tardo demasiado. Verificamos si se completo '
          'antes de que reintentes.';
    case 'CONFLICT':
    case 'IDEMPOTENCY_CONFLICT':
    case 'DUPLICATE_REQUEST':
      return 'Esta operacion ya fue procesada. Revisa tus movimientos '
          'antes de reintentarla.';
    case 'NOT_FOUND':
      return 'No encontramos la operacion. Vuelve e inicia el flujo de nuevo.';
    case 'VALIDATION_ERROR':
      return 'Revisa los datos ingresados e intentalo de nuevo.';
    default:
      return error.message;
  }
}
