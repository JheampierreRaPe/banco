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
  KycApplicant? lastApplicant;
  String? lastDocumentNumber;
  String? lastDocumentType;

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
    required KycApplicant applicant,
    required Map<String, List<Uint8List>> framesByTask,
  }) async {
    submitCalls++;
    lastApplicant = applicant;
    lastDocumentNumber = documentNumber;
    lastDocumentType = documentType;
    final error = throwOnSubmit;
    if (error != null) throw error;
    return submitResult;
  }
}

/// Doble del servicio ampliado (E1-T29): evalua pasos y captura el submit
/// con documento sin salir a red.
class _FakeEvaluationService implements KycEvaluationService {
  final List<KycTaskEvaluation> results = [];
  int _index = 0;
  int evaluateCalls = 0;
  int submitCalls = 0;
  int validateCalls = 0;
  KycDocumentValidation validateResult =
      const KycDocumentValidation(isValid: true);
  Uint8List? lastValidatedImage;
  Uint8List? lastDocumentImage;
  Map<String, List<Uint8List>>? lastFramesByTask;

  @override
  Future<KycTaskEvaluation> evaluate({
    required String challengeToken,
    required String step,
    required List<Uint8List> frames,
  }) async {
    evaluateCalls++;
    if (_index < results.length) return results[_index++];
    return KycTaskEvaluation(step: step, passed: true);
  }

  @override
  Future<KycDocumentValidation> validateDocument({
    required Uint8List image,
  }) async {
    validateCalls++;
    lastValidatedImage = image;
    return validateResult;
  }

  @override
  Future<KycSubmitResult> submitWithDocument({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required KycApplicant applicant,
    required Map<String, List<Uint8List>> framesByTask,
    Uint8List? documentImage,
  }) async {
    submitCalls++;
    lastFramesByTask = framesByTask;
    lastDocumentImage = documentImage;
    return const KycSubmitResult(overallResult: true, detailCode: 'OK');
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

    controller.setApplicant(const KycApplicant(
      firstName: 'Ana',
      lastName: 'Perez',
      email: 'ana@example.com',
    ));
    controller.setDocument(type: 'Pasaporte', number: 'AB123456');
    await controller.submit();
    expect(controller.result?.overallResult, isTrue);
    expect(controller.result?.detailCode, 'OK');
    expect(service.submitCalls, 1);
    // El controlador propaga el payload ampliado (F-T19).
    expect(service.lastApplicant?.email, 'ana@example.com');
    expect(service.lastDocumentNumber, 'AB123456');
    expect(service.lastDocumentType, 'Pasaporte');
  });

  test('submit sin datos de titular no llama al servicio', () async {
    final service = FakeKycService();
    final controller = KycFlowController(service: service);
    addTearDown(controller.dispose);

    await controller.loadChallenge();
    await controller.captureAndResolveCurrentTask();
    await controller.captureAndResolveCurrentTask();
    expect(controller.readyToSubmit, isTrue);

    await controller.submit();

    expect(service.submitCalls, 0);
    expect(controller.result, isNull);
    expect(controller.errorMessage, contains('titular'));
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

    controller.setApplicant(const KycApplicant(
      firstName: 'Ana',
      lastName: 'Perez',
      email: 'ana@example.com',
    ));
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

  test('evaluacion real: passed:false conserva el reason y reintenta', () async {
    final evaluation = _FakeEvaluationService()
      ..results.addAll(const [
        KycTaskEvaluation(step: 'front', passed: false, reason: 'MIN_FRAMES'),
        KycTaskEvaluation(step: 'front', passed: true),
      ]);
    final controller = KycFlowController(
      service: FakeKycService(),
      evaluationService: evaluation,
    );
    addTearDown(controller.dispose);

    await controller.loadChallenge();
    await controller.captureAndResolveCurrentTask();
    expect(controller.currentStep, 'front'); // no avanza
    expect(controller.lastError?.serverReason, 'MIN_FRAMES');
    expect(controller.errorMessage, contains('misma tarea'));

    await controller.captureAndResolveCurrentTask();
    expect(controller.taskPassed('front'), isTrue);
    expect(evaluation.evaluateCalls, 2);
  });

  test('captura documento y submit envia documentImage + todos los frames',
      () async {
    final evaluation = _FakeEvaluationService();
    final controller = KycFlowController(
      service: FakeKycService(),
      evaluationService: evaluation,
    );
    addTearDown(controller.dispose);

    await controller.loadChallenge();
    expect(controller.hasDocumentImage, isFalse);

    await controller.captureDocument();
    expect(controller.hasDocumentImage, isTrue);

    await controller.captureAndResolveCurrentTask(); // front
    await controller.captureAndResolveCurrentTask(); // blink
    controller.setApplicant(const KycApplicant(
      firstName: 'Ana',
      lastName: 'Perez',
      email: 'ana@example.com',
    ));
    await controller.submit();

    expect(evaluation.submitCalls, 1);
    expect(evaluation.lastDocumentImage, controller.documentImage);
    expect(
      evaluation.lastFramesByTask?['front']?.length,
      greaterThan(1),
    );
    expect(controller.result?.overallResult, isTrue);
  });

  test('validateDocument envia la imagen capturada y expone is_valid/issues',
      () async {
    final evaluation = _FakeEvaluationService()
      ..validateResult =
          const KycDocumentValidation(isValid: false, issues: ['BLURRY']);
    final controller = KycFlowController(
      service: FakeKycService(),
      evaluationService: evaluation,
    );
    addTearDown(controller.dispose);

    await controller.loadChallenge();
    await controller.captureDocument();
    expect(controller.hasDocumentImage, isTrue);

    final accepted = await controller.validateDocument();

    expect(accepted, isFalse);
    expect(evaluation.validateCalls, 1);
    expect(evaluation.lastValidatedImage, controller.documentImage);
    expect(controller.documentValidation?.isValid, isFalse);
    expect(controller.documentIssues, ['BLURRY']);
  });

  test('fallo de red al validar conserva la captura y permite reintentar',
      () async {
    var calls = 0;
    final controller = KycFlowController(
      service: FakeKycService(),
      documentValidator: (image) async {
        calls++;
        if (calls == 1) throw ApiException.network();
        return const KycDocumentValidation(isValid: true);
      },
    );
    addTearDown(controller.dispose);

    await controller.loadChallenge();
    await controller.captureDocument();
    final image = controller.documentImage;

    final first = await controller.validateDocument();
    expect(first, isFalse);
    expect(controller.documentValidationError, contains('Sin conexion'));
    expect(controller.hasDocumentImage, isTrue);
    expect(controller.documentImage, image);

    final second = await controller.validateDocument();
    expect(second, isTrue);
    expect(controller.documentValidationError, isNull);
    expect(controller.hasDocumentImage, isTrue);
  });
}
