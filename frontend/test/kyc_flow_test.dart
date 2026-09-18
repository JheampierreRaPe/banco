import 'dart:typed_data';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:flutter_test/flutter_test.dart';

/// Doble manual del [KycService] (sin dependencias externas).
class FakeKycService implements KycService {
  FakeKycService({
    this.challengeResult = const KycChallenge(
      token: 'tok-abc',
      steps: ['front', 'blink'],
      expiresIn: 300,
    ),
    this.submitResult = const KycSubmitResult(
      overallResult: true,
      detailCode: 'OK',
    ),
  });

  KycChallenge challengeResult;
  KycSubmitResult submitResult;
  Object? throwOnChallenge;
  Object? throwOnSubmit;
  int challengeCalls = 0;
  int submitCalls = 0;

  @override
  Future<KycChallenge> challenge() async {
    challengeCalls++;
    final error = throwOnChallenge;
    if (error != null) throw error;
    return challengeResult;
  }

  @override
  Future<KycSubmitResult> submit({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required Map<String, List<Uint8List>> framesByTask,
  }) async {
    submitCalls++;
    final error = throwOnSubmit;
    if (error != null) throw error;
    return submitResult;
  }
}

void main() {
  test('happy path: challenge -> captura por tarea -> submit exitoso',
      () async {
    final service = FakeKycService();
    final controller = KycFlowController(service: service);
    addTearDown(controller.dispose);

    await controller.loadChallenge();
    expect(controller.token, 'tok-abc');
    expect(controller.currentStep, 'front');

    await controller.captureAndResolveCurrentTask();
    expect(controller.taskPassed('front'), isTrue);
    expect(controller.currentStep, 'blink'); // avanzo solo con passed:true

    await controller.captureAndResolveCurrentTask();
    expect(controller.readyToSubmit, isTrue);

    await controller.submit();
    expect(controller.result?.overallResult, isTrue);
    expect(controller.result?.detailCode, 'OK');
    expect(service.submitCalls, 1);
  });

  test('passed:false reintenta la MISMA tarea sin avanzar', () async {
    final service = FakeKycService();
    var calls = 0;
    final controller = KycFlowController(
      service: service,
      taskEvaluator: (task, frames) async => ++calls >= 2,
    );
    addTearDown(controller.dispose);

    await controller.loadChallenge();

    await controller.captureAndResolveCurrentTask(); // passed:false
    expect(controller.taskPassed('front'), isFalse);
    expect(controller.currentStep, 'front'); // misma tarea
    expect(controller.attemptsOf('front'), 1);
    expect(controller.errorMessage, contains('misma tarea'));

    await controller.captureAndResolveCurrentTask(); // passed:true
    expect(controller.taskPassed('front'), isTrue);
    expect(controller.currentStep, 'blink');
    expect(controller.attemptsOf('front'), 2);
  });

  test('fallo de red en submit mantiene desafio, paso y frames', () async {
    final service = FakeKycService()..throwOnSubmit = ApiException.network();
    final controller = KycFlowController(service: service);
    addTearDown(controller.dispose);

    await controller.loadChallenge();
    await controller.captureAndResolveCurrentTask();
    await controller.captureAndResolveCurrentTask();
    expect(controller.readyToSubmit, isTrue);

    await controller.submit();

    expect(controller.result, isNull);
    expect(controller.errorMessage, contains('Sin conexion'));
    // Estado intacto: no se pierde token ni progreso.
    expect(controller.token, 'tok-abc');
    expect(controller.steps, ['front', 'blink']);
    expect(controller.readyToSubmit, isTrue);
    expect(controller.framesCountOf('front'), greaterThan(0));
  });

  test('fallo de red en loadChallenge mantiene el desafio previo', () async {
    final service = FakeKycService();
    final controller = KycFlowController(service: service);
    addTearDown(controller.dispose);

    await controller.loadChallenge();
    expect(controller.token, 'tok-abc');

    service.throwOnChallenge = ApiException.network();
    await controller.loadChallenge();

    expect(controller.errorMessage, contains('Sin conexion'));
    expect(controller.token, 'tok-abc');
    expect(controller.steps, isNotEmpty);
  });
}
