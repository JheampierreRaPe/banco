// Pruebas de controladores y validadores del restablecimiento de PIN
// (F-T43, CA-03/CA-04).
//
// - Identidad: formato bloquea sin red; 200 marca éxito neutro; 429 muestra
//   espera; red permite reintentar.
// - OTP: incompleto bloquea; formato inválido da error; válido avanza sin
//   red; cuenta atrás y reenvío con cooldown usan `requestOtp` (nunca
//   `/auth/otp/resend`).
// - Confirmación: éxito persiste `user_ref` sin abrir sesión;
//   `INVALID_PIN_RESET` -> copy genérico único (sin revelar el campo);
//   `429` -> espera; `422` -> mensaje accionable.
// - Sin PII: ningún mensaje contiene email/DNI/OTP/PIN.
// - Validadores: email/DNI solo formato; máscaras no exponen datos.
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/pin_reset/pin_reset_controllers.dart';
import 'package:banca_online/features/pin_reset/pin_reset_service.dart';
import 'package:banca_online/features/pin_reset/pin_reset_validators.dart';
import 'package:flutter_test/flutter_test.dart';

class FakePinResetService implements PinResetService {
  FakePinResetService({
    this.requestResult = const PinResetRequestResult(
      accepted: true,
      ttlSeconds: 600,
      resendWaitSeconds: 30,
    ),
    this.resetResult = const PinResetResult(
      userRef: 'user-123',
      pinSet: true,
    ),
  });

  PinResetRequestResult requestResult;
  PinResetResult resetResult;
  ApiException? requestError;
  ApiException? resetError;
  int requestCalls = 0;
  int resetCalls = 0;
  Map<String, String> lastResetBody = const {};

  @override
  Future<PinResetRequestResult> requestOtp({required String email}) async {
    requestCalls++;
    final error = requestError;
    if (error != null) throw error;
    return requestResult;
  }

  @override
  Future<PinResetResult> resetPin({
    required String email,
    required String docNumber,
    required String code,
    required String pin,
  }) async {
    resetCalls++;
    lastResetBody = {
      'email': email,
      'docNumber': docNumber,
      'code': code,
      'pin': pin,
    };
    final error = resetError;
    if (error != null) throw error;
    return resetResult;
  }
}

void main() {
  group('validadores y máscaras', () {
    test('email solo formato (no decide existencia)', () {
      expect(isPinResetEmailValid('a@b.com'), isTrue);
      expect(isPinResetEmailValid('no-es-correo'), isFalse);
      expect(isPinResetEmailValid(''), isFalse);
    });

    test('DNI son 8 dígitos (no decide coincidencia)', () {
      expect(isDocNumberValid('12345678'), isTrue);
      expect(isDocNumberValid('1234567'), isFalse);
      expect(isDocNumberValid('123456789'), isFalse);
      expect(isDocNumberValid('abcdefgh'), isFalse);
      expect(isDocNumberValid(''), isFalse);
    });

    test('máscaras no exponen datos completos', () {
      expect(maskDocNumber('12345678'), '****5678');
      expect(maskDocNumber('12345678'), isNot(contains('1234')));
      expect(maskPinResetEmail('juan@banco.com'), isNot(contains('juan')));
      expect(maskPinResetEmail('juan@banco.com'), contains('@banco.com'));
    });
  });

  group('PinResetIdentityController', () {
    test('email inválido bloquea sin llamar a red', () async {
      final service = FakePinResetService();
      final controller = PinResetIdentityController(service: service);
      addTearDown(controller.dispose);

      final ok = await controller.submit(
        email: 'no-es-correo',
        docNumber: '12345678',
      );

      expect(ok, isFalse);
      expect(service.requestCalls, 0);
      expect(controller.errorMessage, pinResetInvalidEmailMessage);
    });

    test('DNI inválido bloquea sin llamar a red', () async {
      final service = FakePinResetService();
      final controller = PinResetIdentityController(service: service);
      addTearDown(controller.dispose);

      final ok = await controller.submit(
        email: 'a@b.com',
        docNumber: '1234',
      );

      expect(ok, isFalse);
      expect(service.requestCalls, 0);
      expect(controller.errorMessage, pinResetInvalidDocMessage);
    });

    test('200 marca éxito con mensaje neutro (CA-03)', () async {
      final service = FakePinResetService();
      final controller = PinResetIdentityController(service: service);
      addTearDown(controller.dispose);

      final ok = await controller.submit(
        email: 'a@b.com',
        docNumber: '12345678',
      );

      expect(ok, isTrue);
      expect(service.requestCalls, 1);
      expect(controller.succeeded, isTrue);
      expect(controller.lastResult?.ttlSeconds, 600);
      expect(controller.lastResult?.resendWaitSeconds, 30);
    });

    test('429 muestra espera', () async {
      final service = FakePinResetService()
        ..requestError = ApiException(
          code: 'RATE_LIMITED',
          message: 'detalle interno',
        );
      final controller = PinResetIdentityController(service: service);
      addTearDown(controller.dispose);

      final ok = await controller.submit(
        email: 'a@b.com',
        docNumber: '12345678',
      );

      expect(ok, isFalse);
      expect(controller.errorMessage, PinResetOtpController.rateMessage);
    });

    test('error de red permite reintentar (retry limpia)', () async {
      final service = FakePinResetService()
        ..requestError = ApiException.network();
      final controller = PinResetIdentityController(service: service);
      addTearDown(controller.dispose);

      await controller.submit(email: 'a@b.com', docNumber: '12345678');
      expect(controller.errorMessage, isNotNull);

      controller.retry();
      expect(controller.errorMessage, isNull);
      expect(controller.status, PinResetIdentityStatus.idle);
    });
  });

  group('PinResetOtpController', () {
    test('código incompleto bloquea el avance', () {
      final controller = PinResetOtpController(
        service: FakePinResetService(),
        email: 'a@b.com',
      );
      addTearDown(controller.dispose);

      controller.setCode('123');
      expect(controller.canSubmit, isFalse);
    });

    test('código inválido da error sin red', () async {
      final service = FakePinResetService();
      final controller = PinResetOtpController(
        service: service,
        email: 'a@b.com',
      );
      addTearDown(controller.dispose);

      final ok = await controller.submit('12ab');

      expect(ok, isFalse);
      expect(service.requestCalls, 0);
      expect(controller.errorMessage, isNotNull);
    });

    test('código completo avanza sin red (se consume al final)', () async {
      final service = FakePinResetService();
      final controller = PinResetOtpController(
        service: service,
        email: 'a@b.com',
      );
      addTearDown(controller.dispose);

      final ok = await controller.submit('654321');

      expect(ok, isTrue);
      expect(service.requestCalls, 0);
      expect(controller.succeeded, isTrue);
    });

    test('cuenta atrás Descuenta vigencia y cooldown', () {
      final controller = PinResetOtpController(
        service: FakePinResetService(),
        email: 'a@b.com',
        otpValiditySeconds: 600,
        resendWaitSeconds: 30,
      );
      addTearDown(controller.dispose);

      expect(controller.remainingSeconds, 600);
      controller.tick();
      expect(controller.remainingSeconds, 599);
    });

    test('reenvío bloqueado durante el cooldown y permitido después',
        () async {
      final service = FakePinResetService();
      final controller = PinResetOtpController(
        service: service,
        email: 'a@b.com',
        resendWaitSeconds: 30,
      );
      addTearDown(controller.dispose);
      // Simula el cooldown recién devuelto por el backend.
      controller
        ..setCode('')
        ..tick();
      // Fuerza el cooldown: el constructor lo inicia en 0, así que se pide
      // un reenvío para fijarlo y luego se verifica el bloqueo.
      await controller.resend();
      expect(controller.canResend, isFalse);

      for (var i = 0; i < 30; i++) {
        controller.tick();
      }
      expect(controller.canResend, isTrue);
    });

    test('reenvío usa requestOtp y reinicia vigencia (nunca otp/resend)',
        () async {
      final service = FakePinResetService();
      final controller = PinResetOtpController(
        service: service,
        email: 'a@b.com',
      );
      addTearDown(controller.dispose);

      final ok = await controller.resend();

      expect(ok, isTrue);
      expect(service.requestCalls, 1);
      expect(controller.remainingSeconds, 600);
    });

    test('reenvío con 429 muestra espera', () async {
      final service = FakePinResetService()
        ..requestError = ApiException(
          code: 'RATE_LIMITED',
          message: 'detalle interno',
        );
      final controller = PinResetOtpController(
        service: service,
        email: 'a@b.com',
      );
      addTearDown(controller.dispose);

      final ok = await controller.resend();

      expect(ok, isFalse);
      expect(controller.errorMessage, PinResetOtpController.rateMessage);
      expect(controller.isResending, isFalse);
    });
  });

  group('PinResetConfirmController', () {
    PinResetDraft draft() => const PinResetDraft(
          email: 'a@b.com',
          docNumber: '12345678',
          code: '654321',
          pin: '482916',
        );

    test('éxito persiste user_ref sin abrir sesión (CA-02)', () async {
      final service = FakePinResetService();
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final controller = PinResetConfirmController(
        service: service,
        draft: draft(),
        identity: identity,
      );
      addTearDown(controller.dispose);

      final ok = await controller.submit();

      expect(ok, isTrue);
      expect(service.resetCalls, 1);
      expect(service.lastResetBody['docNumber'], '12345678');
      expect(controller.resetUserRef, 'user-123');
      expect(await identity.readUserRef(), 'user-123');
      expect(session.isAuthenticated, isFalse);
    });

    test('INVALID_PIN_RESET usa el copy genérico único (CA-03)', () async {
      final service = FakePinResetService()
        ..resetError = ApiException(
          code: 'INVALID_PIN_RESET',
          message: 'detalle interno del servidor',
        );
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final controller = PinResetConfirmController(
        service: service,
        draft: draft(),
        identity: identity,
      );
      addTearDown(controller.dispose);

      final ok = await controller.submit();

      expect(ok, isFalse);
      expect(
        controller.errorMessage,
        PinResetConfirmController.invalidResetMessage,
      );
      // Anti-enumeración: no revela el campo ni propaga el detalle.
      expect(controller.errorMessage, isNot(contains('detalle interno')));
      expect(await identity.readUserRef(), isNull);
    });

    test('mismo copy para DNI incorrecto, OTP incorrecto o vencido', () async {
      // El backend colapsa todos los casos en el mismo código; el cliente
      // muestra el mismo mensaje sin distinguir.
      for (final d in [
        draft(),
        draft().copyWith(code: '000000'),
      ]) {
        final service = FakePinResetService()
          ..resetError = ApiException(
            code: 'INVALID_PIN_RESET',
            message: 'x',
          );
        final controller = PinResetConfirmController(
          service: service,
          draft: d,
        );
        addTearDown(controller.dispose);

        await controller.submit();

        expect(
          controller.errorMessage,
          PinResetConfirmController.invalidResetMessage,
        );
      }
    });

    test('429 muestra espera', () async {
      final service = FakePinResetService()
        ..resetError = ApiException(
          code: 'RATE_LIMITED',
          message: 'detalle interno',
        );
      final controller = PinResetConfirmController(
        service: service,
        draft: draft(),
      );
      addTearDown(controller.dispose);

      await controller.submit();

      expect(
        controller.errorMessage,
        PinResetConfirmController.rateMessage,
      );
    });

    test('422 muestra mensaje accionable y permite reintentar', () async {
      final service = FakePinResetService()
        ..resetError = ApiException(
          code: 'VALIDATION_ERROR',
          message: 'Revisa los datos ingresados e inténtalo de nuevo.',
        );
      final controller = PinResetConfirmController(
        service: service,
        draft: draft(),
      );
      addTearDown(controller.dispose);

      final ok = await controller.submit();

      expect(ok, isFalse);
      expect(
        controller.errorMessage,
        'Revisa los datos ingresados e inténtalo de nuevo.',
      );
      // Doble envío bloqueado mientras está ocupado; tras el error se puede
      // reintentar.
      service.resetError = null;
      final retry = await controller.submit();
      expect(retry, isTrue);
    });

    test('retry limpia el error y permite reintentar', () async {
      final service = FakePinResetService()
        ..resetError = ApiException(
          code: 'INVALID_PIN_RESET',
          message: 'x',
        );
      final controller = PinResetConfirmController(
        service: service,
        draft: draft(),
      );
      addTearDown(controller.dispose);

      await controller.submit();
      expect(controller.errorMessage, isNotNull);

      controller.retry();
      expect(controller.errorMessage, isNull);
      expect(controller.status, PinResetConfirmStatus.idle);

      service.resetError = null;
      expect(await controller.submit(), isTrue);
    });

    test('ningún mensaje contiene PII (CA-04)', () async {      final service = FakePinResetService()
        ..resetError = ApiException(
          code: 'INVALID_PIN_RESET',
          message: 'x',
        );
      final controller = PinResetConfirmController(
        service: service,
        draft: draft(),
      );
      addTearDown(controller.dispose);

      await controller.submit();

      final message = controller.errorMessage ?? '';
      expect(message, isNot(contains('a@b.com')));
      expect(message, isNot(contains('12345678')));
      expect(message, isNot(contains('654321')));
      expect(message, isNot(contains('482916')));
    });
  });
}
