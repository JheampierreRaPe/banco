// Controlador del reto biometrico de pago (E2-T21, HU08).
//
// Flujo (docs/05 §6.3, E2-T19 + challenge de reintento):
//  1. El backend deja la operacion en `PENDING_AUTHORIZATION` con un `nonce`.
//  2. La app pide la biometria del SO ([BiometricReader], F-T03/F-T46).
//  3. Con el biometrico superado, firma el `nonce` ([signNonce], reutiliza
//     `BiometricService.signNonce` de F-T03 sin romperlo) y llama a
//     `POST /transactions/{id}/authorize` con la `Idempotency-Key` del
//     intento de pago.
//  4. Ante `EXPIRED_NONCE`, rota el reto con
//     `POST /transactions/{id}/authorize/challenge` (el `GET` solo relee el
//     `nonce` almacenado sin rotarlo) y reintenta UNA vez con el `nonce`
//     nuevo y una `Idempotency-Key` NUEVA (la anterior snapshotteo el fallo:
//     misma clave + cuerpo distinto = `409`).
//
// Reglas (brief E2-T21 + docs/16):
//  - Sin biometria disponible -> fallback a PIN (no es error).
//  - Biometria cancelada/fallida -> mensaje claro + fallback a PIN.
//  - `EXPIRED_NONCE` -> rota el reto (`POST .../authorize/challenge`) y
//    reintenta UNA vez con el `nonce` nuevo y clave NUEVA derivada.
//  - Nunca loguea nonces, firmas ni resultados biometricos (docs/16 7/10).
//  - Nunca bloquea el hilo: todo es `async` y la UI decide los tiempos.
library;

import '../../core/errors/api_exception.dart';
import '../biometrics/biometric_reader.dart';
import 'data/payment_authorization_service.dart';

/// Deriva la clave del primer `authorize` desde la del borrador.
///
/// El backend tiene UQ global `(key, user_id)` sin `endpoint`: reutilizar
/// la clave del `POST /transfers/...` en `POST .../authorize` (cuerpo
/// distinto) devuelve `409 CONFLICT` y el reto nunca se ejecuta. Por eso
/// el `authorize` NUNCA usa la clave del borrador tal cual, sino
/// `<borrador>-auth` (truncada a 80 conservando el sufijo).
String? paymentAuthInitialKey(String? draftKey) {
  final clean = (draftKey ?? '').trim();
  if (clean.isEmpty) return null;
  final derived = '$clean-auth';
  if (derived.length <= 80) return derived;
  return derived.substring(derived.length - 80);
}

/// Deriva la clave del reintento tras `EXPIRED_NONCE` (contrato real,
///
/// docs/05 §6.3): el fallo previo quedo snapshotteado bajo [key] (misma
/// clave + cuerpo distinto = `409`), asi que el reintento con el `nonce`
/// nuevo exige una clave distinta. Sin clave no hay reintento (`null`).
/// Aplicada sobre la clave ya derivada (`<borrador>-auth`) produce
/// `<borrador>-auth-retry-1`, tambien valida ante `409` por reuso
/// cruzado (misma funcion para el reintento por conflicto).
String? paymentAuthRetryKey(String? key) {
  final clean = (key ?? '').trim();
  if (clean.isEmpty) return null;
  final derived = '$clean-retry-1';
  if (derived.length <= 80) return derived;
  return derived.substring(derived.length - 80);
}

/// `true` si [code] es un conflicto de idempotencia (`409`).
bool isPaymentAuthConflictCode(String? code) {
  final c = (code ?? '').toUpperCase();
  return c == 'CONFLICT' ||
      c == 'IDEMPOTENCY_CONFLICT' ||
      c == 'DUPLICATE_REQUEST';
}

/// Desenlace del reto para que la UI continue el flujo de pago.
enum PaymentAuthOutcome {
  /// Firma aceptada: continuar (comprobante / seguimiento).
  authorized,

  /// Hay que dirigir al usuario al PIN (sin biometria, cancelada o
  /// fallida). No es un error tecnico.
  requiresPinFallback,

  /// El reto expiro incluso tras el reintento: reiniciar el flujo.
  expired,

  /// Fallo comunicado (mensaje en [PaymentAuthState.message]).
  failed,
}

/// Estado observable del reto (4 estados de UI: reposo / cargando /
///
/// exito / error; el fallback a PIN es un desenlace, no un error).
class PaymentAuthState {
  const PaymentAuthState._({
    required this._phase,
    required this.outcome,
    this.message = '',
    this.result,
    this.attempts = 0,
    this.code,
  });

  const PaymentAuthState.idle()
    : _phase = _Phase.idle,
      outcome = null,
      message = '',
      result = null,
      attempts = 0,
      code = null;

  final _Phase _phase;
  final PaymentAuthOutcome? outcome;

  /// Mensaje claro para la UI (sin tecnicismos; el codigo va en el
  /// detalle plegable de [PaymentErrorView]).
  final String message;

  /// Resultado del backend cuando hubo autorizacion.
  final AuthorizationResult? result;

  /// Intentos de red realizados (1 = directo, 2 = tras `EXPIRED_NONCE`).
  final int attempts;

  /// Codigo del backend del fallo (`EXPIRED_NONCE`, `NOT_AUTHORIZED`,
  /// ...; `null` en exito o fallback sin red).
  final String? code;

  bool get isLoading => _phase == _Phase.working;
}

enum _Phase { idle, working, done }

/// Orquesta biometria del SO + firma del `nonce` + `authorize` (E2-T21).
///
/// [signNonce] reutiliza `BiometricService.signNonce` en produccion
/// (inyectado por la ruta); en tests se pasa un firmador falso. El
/// [deviceId] viaja como `device_id` del contrato E2-T19.
class PaymentAuthorizationController {
  PaymentAuthorizationController({
    required this._service,
    required this._reader,
    required this._signNonce,
    required this._deviceId,
  });

  final PaymentAuthorizationServiceBase _service;
  final BiometricReader _reader;
  final Future<String> Function(String nonce) _signNonce;
  final String _deviceId;

  /// Ejecuta el reto completo y devuelve su desenlace.
  ///
  /// [reason] es el texto visible del prompt del SO
  /// (`Usa tu huella para enviar ...`). [idempotencyKey] es la clave del
  /// intento de pago (primer `authorize`); el reintento tras `EXPIRED_NONCE`
  /// usa una clave NUEVA derivada ([paymentAuthRetryKey]).
  Future<PaymentAuthState> authorize({
    required String transactionId,
    required String nonce,
    String? idempotencyKey,
    required String reason,
  }) async {
    // 1. Disponibilidad: sin biometria -> fallback directo (brief E2-T21).
    final available = await _reader.isAvailable();
    if (!available) {
      return const PaymentAuthState._(
        phase: _Phase.done,
        outcome: PaymentAuthOutcome.requiresPinFallback,
        message:
            'Este dispositivo no tiene biometria disponible. '
            'Continua con tu PIN de seguridad.',
      );
    }
    // 2. Desafio del SO: cancelado/fallido -> fallback con mensaje claro.
    final auth = await _reader.authenticate(reason: reason);
    if (!auth.authenticated) {
      return const PaymentAuthState._(
        phase: _Phase.done,
        outcome: PaymentAuthOutcome.requiresPinFallback,
        message:
            'No pudimos verificar tu huella. '
            'Puedes reintentar o usar tu PIN de seguridad.',
      );
    }
    // 3. Firma del `nonce` con la clave en almacenamiento seguro (F-T03).
    late final String signature;
    try {
      signature = await _signNonce(nonce);
    } on ArgumentError {
      return const PaymentAuthState._(
        phase: _Phase.done,
        outcome: PaymentAuthOutcome.failed,
        message:
            'La solicitud expiro. Vuelve a intentarlo con una '
            'nueva autorizacion.',
      );
    }
    // 4. `authorize` con la `Idempotency-Key` del intento.
    try {
      final result = await _service.authorize(
        transactionId: transactionId,
        nonce: nonce,
        signature: signature,
        deviceId: _deviceId,
        idempotencyKey: idempotencyKey,
      );
      return PaymentAuthState._(
        phase: _Phase.done,
        outcome: PaymentAuthOutcome.authorized,
        result: result,
        attempts: 1,
      );
    } on ApiException catch (e) {
      if (e.code == 'EXPIRED_NONCE') {
        return _retryWithFreshNonce(
          transactionId: transactionId,
          idempotencyKey: idempotencyKey,
        );
      }
      if (e.code == 'NOT_AUTHORIZED') {
        return PaymentAuthState._(
          phase: _Phase.done,
          outcome: PaymentAuthOutcome.requiresPinFallback,
          message: paymentAuthorizationUserMessage(e),
          code: e.code,
        );
      }
      return PaymentAuthState._(
        phase: _Phase.done,
        outcome: PaymentAuthOutcome.failed,
        message: paymentAuthorizationUserMessage(e),
        code: e.code,
      );
    }
  }

  /// Reintento tras `EXPIRED_NONCE`: rota el reto y re-autoriza con el
  /// `nonce` nuevo y una `Idempotency-Key` NUEVA (la anterior snapshotteo
  /// el fallo: reutilizarla con el cuerpo nuevo devuelve `409`).
  Future<PaymentAuthState> _retryWithFreshNonce({
    required String transactionId,
    String? idempotencyKey,
  }) async {
    final retryKey = paymentAuthRetryKey(idempotencyKey);
    if (retryKey == null) {
      return const PaymentAuthState._(
        phase: _Phase.done,
        outcome: PaymentAuthOutcome.expired,
        message:
            'La solicitud expiro. Vuelve a intentarlo con una '
            'nueva autorizacion.',
        attempts: 1,
      );
    }
    late final String freshNonce;
    try {
      final detail = await _service.refreshChallenge(transactionId);
      final candidate = detail.nonce ?? '';
      if (candidate.isEmpty) {
        return const PaymentAuthState._(
          phase: _Phase.done,
          outcome: PaymentAuthOutcome.expired,
          message:
              'La solicitud expiro. Vuelve a intentarlo con una '
              'nueva autorizacion.',
          attempts: 1,
        );
      }
      freshNonce = candidate;
    } on ApiException catch (e) {
      return PaymentAuthState._(
        phase: _Phase.done,
        outcome: PaymentAuthOutcome.failed,
        message: paymentAuthorizationUserMessage(e),
        attempts: 1,
        code: e.code,
      );
    }
    // El biometrico ya fue superado en este intento: solo se re-firma el
    // `nonce` nuevo (la clave sigue en almacenamiento seguro).
    late final String signature;
    try {
      signature = await _signNonce(freshNonce);
    } on ArgumentError {
      return const PaymentAuthState._(
        phase: _Phase.done,
        outcome: PaymentAuthOutcome.expired,
        message:
            'La solicitud expiro. Vuelve a intentarlo con una '
            'nueva autorizacion.',
        attempts: 1,
      );
    }
    try {
      final result = await _service.authorize(
        transactionId: transactionId,
        nonce: freshNonce,
        signature: signature,
        deviceId: _deviceId,
        idempotencyKey: retryKey,
      );
      return PaymentAuthState._(
        phase: _Phase.done,
        outcome: PaymentAuthOutcome.authorized,
        result: result,
        attempts: 2,
      );
    } on ApiException catch (e) {
      if (e.code == 'EXPIRED_NONCE') {
        return const PaymentAuthState._(
          phase: _Phase.done,
          outcome: PaymentAuthOutcome.expired,
          message:
              'La solicitud expiro. Vuelve a intentarlo con una '
              'nueva autorizacion.',
          attempts: 2,
        );
      }
      if (e.code == 'NOT_AUTHORIZED') {
        return PaymentAuthState._(
          phase: _Phase.done,
          outcome: PaymentAuthOutcome.requiresPinFallback,
          message: paymentAuthorizationUserMessage(e),
          attempts: 2,
          code: e.code,
        );
      }
      return PaymentAuthState._(
        phase: _Phase.done,
        outcome: PaymentAuthOutcome.failed,
        message: paymentAuthorizationUserMessage(e),
        attempts: 2,
        code: e.code,
      );
    }
  }
}
