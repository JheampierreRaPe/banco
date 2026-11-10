import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/activation/activation_controller.dart';
import 'package:banca_online/features/activation/activation_service.dart';
import 'package:flutter_test/flutter_test.dart';

/// Fake configurable del contrato (sin red).
class FakeActivationService implements ActivationService {
  ActivationResult? activateResult;
  ApiException? activateError;
  ResendResult? resendResult;
  ApiException? resendError;
  int activateCalls = 0;
  int resendCalls = 0;

  @override
  Future<ActivationResult> activate({
    required String userRef,
    required String code,
  }) async {
    activateCalls++;
    final error = activateError;
    if (error != null) throw error;
    return activateResult ??
        const ActivationResult(userId: 'u-1', status: 'ACTIVE');
  }

  @override
  Future<ResendResult> resend({required String userRef, String? channel}) async {
    resendCalls++;
    final error = resendError;
    if (error != null) throw error;
    return resendResult ??
        const ResendResult(userId: 'u-1', resendCount: 1, expiresInSeconds: 600);
  }
}

ActivationController _controller(
  FakeActivationService service, {
  int validity = 600,
  int wait = 30,
}) =>
    ActivationController(
      service: service,
      userRef: 'u-1',
      otpValiditySeconds: validity,
      resendWaitSeconds: wait,
    );

void main() {
  test('formatCountdown cubre 10:00, minutos y cero', () {
    expect(formatCountdown(600), '10:00');
    expect(formatCountdown(61), '01:01');
    expect(formatCountdown(9), '00:09');
    expect(formatCountdown(0), '00:00');
  });

  test('el contador desciende con tick y al agotarse marca expirado', () {
    final controller = _controller(FakeActivationService(), validity: 5);

    expect(controller.remainingSeconds, 5);
    expect(controller.status, ActivationStatus.idle);

    controller.tick();
    controller.tick();
    expect(controller.remainingSeconds, 3);
    expect(controller.status, ActivationStatus.idle);

    controller.tick();
    controller.tick();
    controller.tick();
    expect(controller.remainingSeconds, 0);
    expect(controller.status, ActivationStatus.expired);
    expect(controller.errorMessage, ActivationController.expiredMessage);
  });

  test('submit exitoso marca success', () async {
    final controller = _controller(FakeActivationService());

    final ok = await controller.submit('123456');

    expect(ok, isTrue);
    expect(controller.status, ActivationStatus.success);
    expect(controller.errorMessage, isNull);
  });

  test('código inválido conserva el tiempo y muestra mensaje accionable',
      () async {
    final service = FakeActivationService()
      ..activateError = ApiException(code: 'INVALID_OTP', message: 'srv');
    final controller = _controller(service);

    final ok = await controller.submit('000000');

    expect(ok, isFalse);
    expect(controller.status, ActivationStatus.idle);
    expect(controller.errorMessage, ActivationController.invalidMessage);
    // El contador NO se reinicia: el código sigue vigente.
    expect(controller.remainingSeconds, 600);
  });

  test('código expirado marca expirado y habilita el reenvío', () async {
    final service = FakeActivationService()
      ..activateError = ApiException(code: 'EXPIRED_OTP', message: 'srv');
    final controller = _controller(service);

    final ok = await controller.submit('123456');

    expect(ok, isFalse);
    expect(controller.status, ActivationStatus.expired);
    expect(controller.errorMessage, ActivationController.expiredMessage);
    // Accionable: la espera de reenvío se libera para pedir uno nuevo.
    expect(controller.canResend, isTrue);
  });

  test('límite de reenvíos marca limitReached', () async {
    final service = FakeActivationService()
      ..activateError = ApiException(code: 'RESEND_LIMIT', message: 'srv');
    final controller = _controller(service);

    final ok = await controller.submit('123456');

    expect(ok, isFalse);
    expect(controller.status, ActivationStatus.limitReached);
    expect(controller.errorMessage, ActivationController.limitMessage);
    expect(controller.canResend, isFalse);
  });

  test('reenvío reinicia el contador y aplica la espera de 30 s', () async {
    final service = FakeActivationService()
      ..resendResult = const ResendResult(
        userId: 'u-1',
        resendCount: 1,
        expiresInSeconds: 600,
      );
    final controller = _controller(service, wait: 30);

    // Agotar la espera inicial para poder reenviar.
    for (var i = 0; i < 30; i++) {
      controller.tick();
    }
    expect(controller.canResend, isTrue);

    final ok = await controller.resend();

    expect(ok, isTrue);
    expect(controller.resendCount, 1);
    expect(controller.remainingSeconds, 600);
    expect(controller.resendCooldownSeconds, 30);
    expect(controller.canResend, isFalse);
    expect(controller.infoMessage, ActivationController.resentMessage);

    for (var i = 0; i < 30; i++) {
      controller.tick();
    }
    expect(controller.canResend, isTrue);
  });
}
