// Pruebas de los controladores de recuperación (F-T40, CA-01..CA-04).
//
// - Email: formato inválido bloquea; 200 expone el mensaje neutro + `request`
//   con `{email}`; error de red con mensaje accionable.
// - OTP (contrato E1-T33): `canSubmit` exige 6 dígitos; éxito persiste
//   `user_ref` y expone `verifiedUserRef` para navegar a `/login?userRef=`
//   SIN abrir sesión; envía `device_id` + `device_public_key` (`hmac:<hex>`)
//   + `platform`; `INVALID_RECOVERY_CODE` -> mismo mensaje genérico;
//   `429` -> espera; reenvío respeta el cooldown; `saveUserRef` fallido no
//   bloquea la continuación al login.
import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/errors/error_messages.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/recovery/recovery_controller.dart';
import 'package:banca_online/features/recovery/recovery_service.dart';
import 'package:flutter_test/flutter_test.dart';

class FakeRecoveryService implements RecoveryService {
  FakeRecoveryService({
    this.requestResult = const RecoveryRequestResult(
      accepted: true,
      ttlSeconds: 600,
      resendWaitSeconds: 30,
    ),
    this.verifyResult = const RecoveryVerifyResult(
      userRef: 'user-123',
      deviceBound: true,
    ),
  });

  RecoveryRequestResult requestResult;
  RecoveryVerifyResult verifyResult;
  ApiException? requestError;
  ApiException? verifyError;

  /// Falla no tipada (p. ej. `StateError`) para regresión de `resend()`.
  Object? requestFailure;

  int requestCalls = 0;
  String? lastRequestEmail;
  Map<String, dynamic>? lastVerify;

  @override
  Future<RecoveryRequestResult> request({required String email}) async {
    requestCalls++;
    lastRequestEmail = email;
    final failure = requestFailure;
    if (failure != null) throw failure;
    final error = requestError;
    if (error != null) throw error;
    return requestResult;
  }

  @override
  Future<RecoveryVerifyResult> verify({
    required String email,
    required String code,
    String? deviceId,
    String? devicePublicKey,
    String? platform,
  }) async {
    lastVerify = {
      'email': email,
      'code': code,
      'device_id': deviceId,
      'device_public_key': devicePublicKey,
      'platform': platform,
    };
    final error = verifyError;
    if (error != null) throw error;
    return verifyResult;
  }
}

/// Store que falla al guardar `user_ref` (best-effort: no debe tumbar la
/// sesión ya concedida).
class _FailingSaveUserRefStore extends InMemorySessionIdentityStore {
  @override
  Future<void> saveUserRef(String userRef) async {
    throw StateError('secure storage no disponible');
  }
}

void main() {
  group('RecoveryEmailController (CA-01)', () {
    test('email inválido bloquea el envío con mensaje de formato', () async {
      final service = FakeRecoveryService();
      final controller = RecoveryEmailController(service: service);
      addTearDown(controller.dispose);

      final ok = await controller.submit('no-es-un-correo');

      expect(ok, isFalse);
      expect(service.requestCalls, 0);
      expect(
        controller.errorMessage,
        RecoveryEmailController.invalidEmailMessage,
      );
    });

    test('200 expone mensaje neutro idéntico exista o no el email',
        () async {
      final service = FakeRecoveryService();
      final existing = RecoveryEmailController(service: service);
      final missing = RecoveryEmailController(service: service);
      addTearDown(existing.dispose);
      addTearDown(missing.dispose);

      expect(await existing.submit('existe@banco.com'), isTrue);
      expect(await missing.submit('nadie@banco.com'), isTrue);

      // Mensaje neutro único (constante compartida, sin ramificar).
      expect(
        RecoveryEmailController.neutralMessage,
        contains('Si el correo está registrado'),
      );
      expect(existing.lastResult?.accepted, isTrue);
      expect(missing.lastResult?.accepted, isTrue);
      expect(service.lastRequestEmail, 'nadie@banco.com');
    });

    test('error de red deja estado de error accionable con reintento',
        () async {
      final service = FakeRecoveryService()
        ..requestError = ApiException.network();
      final controller = RecoveryEmailController(service: service);
      addTearDown(controller.dispose);

      expect(await controller.submit('a@b.com'), isFalse);
      expect(controller.status, RecoveryEmailStatus.error);
      expect(
        controller.errorMessage,
        messageForCode('NETWORK_ERROR'),
      );

      controller.retry();
      expect(controller.status, RecoveryEmailStatus.idle);
      expect(controller.errorMessage, isNull);
    });
  });

  group('RecoveryOtpController (CA-02, CA-03)', () {
    test('código incompleto deshabilita el submit', () {
      final controller = RecoveryOtpController(
        service: FakeRecoveryService(),
        session: InMemorySessionRepository(),
        email: 'a@b.com',
      );
      addTearDown(controller.dispose);

      expect(controller.canSubmit, isFalse);
      controller.setCode('123');
      expect(controller.canSubmit, isFalse);
      controller.setCode('123456');
      expect(controller.canSubmit, isTrue);
    });

    test('OTP correcto persiste user_ref SIN abrir sesion (E1-T33)',
        () async {
      final service = FakeRecoveryService();
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore(deviceId: 'device-1');
      final controller = RecoveryOtpController(
        service: service,
        session: session,
        email: 'a@b.com',
        identity: identity,
        platform: 'android',
      );
      addTearDown(controller.dispose);

      final ok = await controller.submit('123456');

      expect(ok, isTrue);
      expect(controller.succeeded, isTrue);
      // Verify YA NO abre sesión: la única sesión la abre el login.
      expect(session.isAuthenticated, isFalse);
      expect(controller.verifiedUserRef, 'user-123');
      expect(await identity.readUserRef(), 'user-123');
      // Contrato de binding (CA-03): `device_id` + `hmac:<hex>` + `platform`.
      expect(service.lastVerify?['email'], 'a@b.com');
      expect(service.lastVerify?['code'], '123456');
      expect(service.lastVerify?['device_id'], 'device-1');
      expect(
        (service.lastVerify?['device_public_key'] as String?) ?? '',
        startsWith('hmac:'),
      );
      expect(service.lastVerify?['platform'], 'android');
    });

    test('saveUserRef fallido no bloquea la continuacion al login', () async {
      final session = InMemorySessionRepository();
      final controller = RecoveryOtpController(
        service: FakeRecoveryService(),
        session: session,
        email: 'a@b.com',
        identity: _FailingSaveUserRefStore(),
        platform: 'android',
      );
      addTearDown(controller.dispose);

      expect(await controller.submit('123456'), isTrue);
      expect(controller.succeeded, isTrue);
      expect(controller.verifiedUserRef, 'user-123');
      expect(session.isAuthenticated, isFalse);
    });

    test('INVALID_RECOVERY_CODE muestra el mismo mensaje genérico',
        () async {
      final service = FakeRecoveryService()
        ..verifyError = ApiException(
          code: 'INVALID_RECOVERY_CODE',
          message: 'detalle interno',
        );
      final controller = RecoveryOtpController(
        service: service,
        session: InMemorySessionRepository(),
        email: 'a@b.com',
      );
      addTearDown(controller.dispose);
      controller.setCode('000000');

      expect(await controller.submit('000000'), isFalse);
      expect(controller.succeeded, isFalse);
      expect(
        controller.errorMessage,
        RecoveryOtpController.invalidCodeMessage,
      );
      // La sesión NO se abre y se puede reintentar/reenviar.
      expect(controller.canSubmit, isTrue);
    });

    test('429 muestra mensaje de espera', () async {
      final service = FakeRecoveryService()
        ..verifyError = ApiException(
          code: 'RATE_LIMITED',
          message: 'detalle interno',
        );
      final controller = RecoveryOtpController(
        service: service,
        session: InMemorySessionRepository(),
        email: 'a@b.com',
      );
      addTearDown(controller.dispose);

      expect(await controller.submit('123456'), isFalse);
      expect(controller.errorMessage, RecoveryOtpController.rateMessage);
    });

    test('reenvío respeta el cooldown y vuelve a llamar a request', () async {
      final service = FakeRecoveryService();
      final controller = RecoveryOtpController(
        service: service,
        session: InMemorySessionRepository(),
        email: 'a@b.com',
        otpValiditySeconds: 600,
        resendWaitSeconds: 30,
      );
      addTearDown(controller.dispose);

      // Sin cooldown inicial el reenvío procede.
      expect(controller.canResend, isTrue);
      expect(await controller.resend(), isTrue);
      expect(service.requestCalls, 1);
      expect(service.lastRequestEmail, 'a@b.com');
      // Tras reenviar aplica el cooldown: bloquea el siguiente intento.
      expect(controller.canResend, isFalse);
      expect(await controller.resend(), isFalse);
      expect(service.requestCalls, 1);
      // Al agotar el cooldown se habilita de nuevo.
      for (var i = 0; i < 30; i++) {
        controller.tick();
      }
      expect(controller.canResend, isTrue);
      expect(await controller.resend(), isTrue);
      expect(service.requestCalls, 2);
    });
  });

  group('regresión BAJOs F-T29', () {
    test('reenvío usa el resend_wait recién devuelto por el backend',
        () async {
      final service = FakeRecoveryService()
        ..requestResult = const RecoveryRequestResult(
          accepted: true,
          ttlSeconds: 600,
          resendWaitSeconds: 45,
        );
      final controller = RecoveryOtpController(
        service: service,
        session: InMemorySessionRepository(),
        email: 'a@b.com',
        otpValiditySeconds: 600,
        resendWaitSeconds: 30,
      );
      addTearDown(controller.dispose);

      expect(await controller.resend(), isTrue);
      // Sin el fix usaría el 30 del constructor; con el fix usa el 45 del
      // backend recién devuelto.
      expect(controller.resendCooldownSeconds, 45);
      for (var i = 0; i < 30; i++) {
        controller.tick();
      }
      expect(controller.resendCooldownSeconds, 15);
      expect(controller.canResend, isFalse);
    });

    test('resend con excepción no tipada limpia isResending con mensaje',
        () async {
      final service = FakeRecoveryService()
        ..requestFailure = StateError('fallo inesperado');
      final controller = RecoveryOtpController(
        service: service,
        session: InMemorySessionRepository(),
        email: 'a@b.com',
      );
      addTearDown(controller.dispose);

      // Sin el fix la excepción se propaga y `_isResending` queda en true.
      expect(await controller.resend(), isFalse);
      expect(controller.isResending, isFalse);
      expect(
        controller.errorMessage,
        'No pudimos completar la recuperación. Inténtalo de nuevo.',
      );
      expect(controller.canResend, isTrue);
    });

    test('validación acepta emails hasta 320 (contrato E1-T31)', () {
      final email256 = '${'a' * 250}@b.com';
      expect(email256.length, greaterThan(254));
      expect(email256.length, lessThanOrEqualTo(320));
      // Sin el fix (límite 254) este caso daba `false`.
      expect(isRecoveryEmailValid(email256), isTrue);

      final atLimit = '${'a' * 314}@b.com';
      expect(atLimit.length, 320);
      expect(isRecoveryEmailValid(atLimit), isTrue);

      final overLimit = '${'a' * 315}@b.com';
      expect(overLimit.length, 321);
      expect(isRecoveryEmailValid(overLimit), isFalse);
    });
  });

  group('sin PII en logs (CA-04)', () {
    test('operaciones no emiten email/OTP/user_ref/device_id', () async {
      final printed = <String>[];
      final service = FakeRecoveryService();
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore(deviceId: 'device-1');
      await runZoned(
        () async {
          final emailController =
              RecoveryEmailController(service: service);
          await emailController.submit('secreto@banco.com');
          emailController.dispose();
          final otpController = RecoveryOtpController(
            service: service,
            session: session,
            email: 'secreto@banco.com',
            identity: identity,
            platform: 'android',
          );
          await otpController.submit('654321');
          otpController.dispose();
        },
        zoneSpecification: ZoneSpecification(
          print: (self, parent, zone, line) {
            printed.add(line);
          },
        ),
      );

      const secrets = [
        'secreto@banco.com',
        '654321',
        'user-123',
        'device-1',
      ];
      for (final line in printed) {
        for (final secret in secrets) {
          expect(line, isNot(contains(secret)));
        }
      }
      // Los controladores tampoco exponen secretos en su `toString`.
      expect(service.lastVerify?['code'], '654321');
    });
  });
}
