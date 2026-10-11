// Controlador de la firma del contrato (E3-T15, HU11).
//
// Flujo (`docs/05` §6.4, detalle E3-T14 + spec `creditos-pantallas.md` §6):
//  1. La app pide el reto (`POST .../sign/challenge` -> `nonce` de un solo
//     uso, lo obtiene la pagina, no este controlador).
//  2. La app exige consentimiento explicito (checkbox en la pagina; sin el,
//     el CTA esta bloqueado y este controlador nunca se invoca).
//  3. La app pide la biometria del SO ([BiometricReader], F-T03/F-T46).
//  4. Con el biometrico superado, firma el `nonce` ([signNonce], reutiliza
//     `BiometricService.signNonce` de F-T03 sin romperlo) y llama a
//     `POST .../sign` con la `Idempotency-Key` del intento.
//  5. Ante `EXPIRED_NONCE`, rota el reto (`POST .../sign/challenge`) y
//     reintenta UNA vez con el `nonce` nuevo y una `Idempotency-Key` NUEVA
//     (la anterior snapshotteo el fallo: misma clave + cuerpo distinto =
//     `409`; patron E2-T21/G39).
//  6. Contingencia: firma con PIN (`{pin}`, via mutuamente excluyente).
//
// Reglas (brief E3-T15 + `docs/16`):
//  - Sin exito biometrico NO se envia la firma y el contrato no se firma.
//  - Biometria no disponible/cancelada/fallida -> fallback a PIN con mensaje
//    generico (sin revelar el motivo exacto del backend).
//  - Nunca loguea nonces, firmas, PIN ni resultados biometricos (7/10).
//  - Nunca bloquea el hilo: todo es `async` y la UI decide los tiempos.
//  - El PIN nunca se guarda (solo viaja en el cuerpo del intento).
library;

import '../../../core/errors/api_exception.dart';
import '../../../core/session/session_repository.dart';
import '../../biometrics/biometric_reader.dart';
import '../../biometrics/biometric_service.dart';
import '../models/credit_contract.dart';
import 'credit_contract_service.dart';

/// Deriva la clave del reintento tras `EXPIRED_NONCE`.
///
/// El fallo previo quedo snapshotteado bajo [key] (misma clave + cuerpo
/// distinto = `409`), asi que el reintento con el `nonce` nuevo exige una
/// clave distinta. Sin clave no hay reintento (`null`). Trunca a 80
/// conservando el sufijo (mismo patron que `paymentAuthRetryKey`, E2-T21).
String? creditSignRetryKey(String? key) {
  final clean = (key ?? '').trim();
  if (clean.isEmpty) return null;
  final derived = '$clean-retry-1';
  if (derived.length <= 80) return derived;
  return derived.substring(derived.length - 80);
}

/// `true` si [code] es un conflicto de idempotencia (`409`).
bool isCreditSignConflictCode(String? code) {
  final c = (code ?? '').toUpperCase();
  return c == 'CONFLICT' ||
      c == 'IDEMPOTENCY_CONFLICT' ||
      c == 'DUPLICATE_REQUEST';
}

/// Desenlace de la firma para que la UI continue el flujo.
enum CreditSignOutcome {
  /// Firma aceptada (`SIGNED`): mostrar exito + copia enviada.
  signed,

  /// Hay que dirigir al usuario al PIN (sin biometria, cancelada o
  /// fallida, o `NOT_AUTHORIZED` generico). No es un error tecnico.
  requiresPinFallback,

  /// El reto expiro incluso tras el reintento: reiniciar el flujo.
  expired,

  /// Fallo comunicado (mensaje en [CreditSignState.message]).
  failed,
}

/// Estado observable de la firma.
class CreditSignState {
  const CreditSignState._({
    required this.outcome,
    this.message = '',
    this.result,
    this.attempts = 0,
    this.code,
  });

  /// Fallback a PIN sin haber tocado red (biometria no disponible o DI
  /// incompleta). Publico para que la pagina pueda senalarlo sin invocar
  /// el constructor privado del controlador.
  const CreditSignState.pinFallback(this.message)
      : outcome = CreditSignOutcome.requiresPinFallback,
        result = null,
        attempts = 0,
        code = null;

  final CreditSignOutcome outcome;

  /// Mensaje claro para la UI (en espanol; el codigo va en [code]).
  final String message;

  /// Resultado del backend cuando hubo firma.
  final ContractSignatureResult? result;

  /// Intentos de red realizados (1 = directo, 2 = tras `EXPIRED_NONCE`).
  final int attempts;

  /// Codigo del backend del fallo (`EXPIRED_NONCE`, `NOT_AUTHORIZED`,
  /// ...; `null` en exito o fallback sin red).
  final String? code;
}

/// Orquesta biometria del SO + firma del `nonce` + `POST .../sign`.
///
/// [signNonce] reutiliza `BiometricService.signNonce` en produccion
/// (inyectado por la ruta); en tests se pasa un firmador falso. El
/// [deviceId] viaja como `device_id` del contrato E3-T14.
class CreditSignController {
  CreditSignController({
    required this.service,
    required this.reader,
    required this.signNonce,
    required this.deviceId,
  });

  final CreditContractServiceBase service;
  final BiometricReader reader;
  final Future<String> Function(String nonce) signNonce;
  final String deviceId;

  /// Ejecuta la firma biometrica completa y devuelve su desenlace.
  ///
  /// [challenge] es el reto vigente (lo pidio la pagina). [idempotencyKey]
  /// es la clave del intento de firma (el reintento tras `EXPIRED_NONCE`
  /// usa una NUEVA derivada con [creditSignRetryKey]). [reason] es el texto
  /// visible del prompt del SO.
  Future<CreditSignState> signWithBiometrics({
    required String applicationId,
    required SignChallenge challenge,
    String? idempotencyKey,
    required String reason,
  }) async {
    // 1. Disponibilidad: sin biometria -> fallback directo (brief E3-T15).
    final available = await reader.isAvailable();
    if (!available) {
      return const CreditSignState._(
        outcome: CreditSignOutcome.requiresPinFallback,
        message: creditBiometricUnavailableMessage,
      );
    }
    // 2. Desafio del SO: cancelado/fallido -> fallback con mensaje generico.
    //    Sin exito biometrico NO se envia la firma.
    final auth = await reader.authenticate(reason: reason);
    if (!auth.authenticated) {
      return const CreditSignState._(
        outcome: CreditSignOutcome.requiresPinFallback,
        message: creditBiometricFallbackMessage,
      );
    }
    // 3. Firma del `nonce` con la clave en almacenamiento seguro (F-T03).
    late final String signature;
    try {
      signature = await signNonce(challenge.nonce);
    } on ArgumentError {
      return const CreditSignState._(
        outcome: CreditSignOutcome.expired,
        message: 'Tu sesión de firma venció. Estamos generando un nuevo reto…',
      );
    }
    // 4. `POST .../sign` con la `Idempotency-Key` del intento.
    try {
      final result = await service.signWithBiometrics(
        applicationId: applicationId,
        nonce: challenge.nonce,
        signature: signature,
        deviceId: deviceId,
        idempotencyKey: idempotencyKey,
      );
      return CreditSignState._(
        outcome: CreditSignOutcome.signed,
        result: result,
        attempts: 1,
      );
    } on ApiException catch (e) {
      if (e.code == 'EXPIRED_NONCE') {
        return _retryWithFreshChallenge(
          applicationId: applicationId,
          idempotencyKey: idempotencyKey,
        );
      }
      if (e.code == 'NOT_AUTHORIZED') {
        return CreditSignState._(
          outcome: CreditSignOutcome.requiresPinFallback,
          message: creditContractUserMessage(e),
          code: e.code,
        );
      }
      return CreditSignState._(
        outcome: CreditSignOutcome.failed,
        message: creditContractUserMessage(e),
        code: e.code,
      );
    }
  }

  /// Ejecuta la firma con PIN (contingencia, via mutuamente excluyente).
  ///
  /// El formato se valida en la pagina (6 digitos); aqui solo se transporta
  /// (el PIN nunca se guarda). Cada intento usa su propia [idempotencyKey]:
  /// el cuerpo cambia entre intentos (el usuario puede corregir el PIN) y
  /// misma clave + cuerpo distinto = `409`.
  Future<CreditSignState> signWithPin({
    required String applicationId,
    required String pin,
    String? idempotencyKey,
  }) async {
    try {
      final result = await service.signWithPin(
        applicationId: applicationId,
        pin: pin,
        idempotencyKey: idempotencyKey,
      );
      return CreditSignState._(
        outcome: CreditSignOutcome.signed,
        result: result,
        attempts: 1,
      );
    } on ApiException catch (e) {
      if (e.code == 'NOT_AUTHORIZED') {
        // PIN incorrecto: mismo cuerpo generico, sin oraculo.
        return const CreditSignState._(
          outcome: CreditSignOutcome.failed,
          message: creditPinInvalidMessage,
          code: 'NOT_AUTHORIZED',
        );
      }
      return CreditSignState._(
        outcome: CreditSignOutcome.failed,
        message: creditContractUserMessage(e),
        code: e.code,
      );
    }
  }

  /// Reintento tras `EXPIRED_NONCE`: rota el reto y re-firma con el `nonce`
  /// nuevo y una `Idempotency-Key` NUEVA (la anterior snapshotteo el fallo).
  Future<CreditSignState> _retryWithFreshChallenge({
    required String applicationId,
    String? idempotencyKey,
  }) async {
    final retryKey = creditSignRetryKey(idempotencyKey);
    if (retryKey == null) {
      return const CreditSignState._(
        outcome: CreditSignOutcome.expired,
        message:
            'Tu sesión de firma venció. Vuelve a intentarlo con un nuevo reto.',
        attempts: 1,
      );
    }
    late final SignChallenge fresh;
    try {
      fresh = await service.requestChallenge(
        applicationId,
        deviceId: deviceId,
      );
      if (fresh.nonce.isEmpty) {
        return const CreditSignState._(
          outcome: CreditSignOutcome.expired,
          message: 'Tu sesión de firma venció. '
              'Vuelve a intentarlo con un nuevo reto.',
          attempts: 1,
        );
      }
    } on ApiException catch (e) {
      return CreditSignState._(
        outcome: CreditSignOutcome.failed,
        message: creditContractUserMessage(e),
        attempts: 1,
        code: e.code,
      );
    }
    // El biometrico ya fue superado en este intento: solo se re-firma el
    // `nonce` nuevo (la clave sigue en almacenamiento seguro).
    late final String signature;
    try {
      signature = await signNonce(fresh.nonce);
    } on ArgumentError {
      return const CreditSignState._(
        outcome: CreditSignOutcome.expired,
        message:
            'Tu sesión de firma venció. Vuelve a intentarlo con un nuevo reto.',
        attempts: 1,
      );
    }
    try {
      final result = await service.signWithBiometrics(
        applicationId: applicationId,
        nonce: fresh.nonce,
        signature: signature,
        deviceId: deviceId,
        idempotencyKey: retryKey,
      );
      return CreditSignState._(
        outcome: CreditSignOutcome.signed,
        message: 'Se generó un nuevo reto y se completó la firma.',
        result: result,
        attempts: 2,
      );
    } on ApiException catch (e) {
      if (e.code == 'EXPIRED_NONCE') {
        return const CreditSignState._(
          outcome: CreditSignOutcome.expired,
          message: 'Tu sesión de firma venció. '
              'Vuelve a intentarlo con un nuevo reto.',
          attempts: 2,
        );
      }
      if (e.code == 'NOT_AUTHORIZED') {
        return CreditSignState._(
          outcome: CreditSignOutcome.requiresPinFallback,
          message: creditContractUserMessage(e),
          attempts: 2,
          code: e.code,
        );
      }
      return CreditSignState._(
        outcome: CreditSignOutcome.failed,
        message: creditContractUserMessage(e),
        attempts: 2,
        code: e.code,
      );
    }
  }
}

/// Dependencias de la firma, cableadas una vez por el orquestador
/// (`core/router/app_router.dart`) con el `ApiClient` compartido, la
/// sesion, el lector del SO y el `device_id` vigente.
class CreditSignDeps {
  CreditSignDeps({
    required this.service,
    required this.reader,
    required this.session,
    this.deviceId = '',
  });

  final CreditContractServiceBase service;
  final BiometricReader reader;
  final SessionRepository session;
  final String deviceId;

  /// Controlador listo para un intento (reutiliza el firmador de `nonce`
  /// de F-T03: `BiometricService.signNonce`, sin romperlo).
  CreditSignController buildController() {
    final reader = this.reader;
    final session = this.session;
    return CreditSignController(
      service: service,
      reader: reader,
      signNonce: (nonce) =>
          BiometricService(session: session, reader: reader).signNonce(nonce),
      deviceId: deviceId,
    );
  }
}

/// Fabrica inyectable (en tests se asigna un fake).
typedef CreditSignDepsFactory = CreditSignDeps Function();

CreditSignDepsFactory? creditSignDepsFactory;
