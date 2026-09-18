import 'dart:typed_data';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/kyc/kyc_error_handler.dart';
import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/kyc_status_pages.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Doble manual del [KycService] con challenge programable.
class FakeKycService implements KycService {
  FakeKycService({
    this.challengeResult = const KycChallenge(
      token: 'tok-1',
      steps: ['front', 'blink'],
      expiresIn: 300,
    ),
  });

  KycChallenge challengeResult;
  Object? throwOnChallenge;
  int challengeCalls = 0;

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
  }) async =>
      const KycSubmitResult(overallResult: true, detailCode: 'OK');
}

void main() {
  group('E1-T06 controller: token expirado pide challenge nuevo', () {
    test('refresh conserva token nuevo + tareas ya pasadas', () async {
      final service = FakeKycService();
      final controller = KycFlowController(service: service);
      addTearDown(controller.dispose);

      await controller.loadChallenge();
      await controller.captureAndResolveCurrentTask(); // front pasa
      expect(controller.taskPassed('front'), isTrue);

      // El token expira: el servidor emite un challenge nuevo con los mismos
      // pasos; el progreso ya pasado se conserva.
      service.challengeResult = const KycChallenge(
        token: 'tok-2',
        steps: ['front', 'blink'],
        expiresIn: 300,
      );
      await controller.refreshChallengePreservingProgress();

      expect(service.challengeCalls, 2);
      expect(controller.token, 'tok-2');
      expect(controller.taskPassed('front'), isTrue);
      expect(controller.framesCountOf('front'), greaterThan(0));
      expect(controller.currentStep, 'blink');
      expect(controller.lastError, isNull);
    });

    test('401 en captura sugiere refreshChallenge sin perder el estado',
        () async {
      final service = FakeKycService();
      var first = true;
      final controller = KycFlowController(
        service: service,
        taskEvaluator: (task, frames) async {
          if (first) {
            first = false;
            throw ApiException(
              code: 'TOKEN_EXPIRED',
              message: 'mensaje base',
              serverMessage: 'challenge token expired',
              statusCode: 401,
            );
          }
          return true;
        },
      );
      addTearDown(controller.dispose);

      await controller.loadChallenge();
      await controller.captureAndResolveCurrentTask();

      expect(controller.lastError?.action, KycErrorAction.refreshChallenge);
      // Estado intacto: no se pierde token ni paso.
      expect(controller.token, 'tok-1');
      expect(controller.currentStep, 'front');
    });

    test('pasos distintos conservan solo la interseccion (documentado)',
        () async {
      final service = FakeKycService();
      final controller = KycFlowController(service: service);
      addTearDown(controller.dispose);

      await controller.loadChallenge();
      await controller.captureAndResolveCurrentTask();
      expect(controller.taskPassed('front'), isTrue);

      service.challengeResult = const KycChallenge(
        token: 'tok-3',
        steps: ['profile', 'smile'],
        expiresIn: 300,
      );
      await controller.refreshChallengePreservingProgress();

      expect(controller.token, 'tok-3');
      expect(controller.readyToSubmit, isFalse);
      expect(controller.currentStep, 'profile');
    });
  });

  group('E1-T06 controller: limite de reintentos deriva a revision', () {
    test('tras 3 passed:false hay folio y derivacion (default)', () async {
      final service = FakeKycService();
      final controller = KycFlowController(
        service: service,
        taskEvaluator: (task, frames) async => false,
      );
      addTearDown(controller.dispose);

      await controller.loadChallenge();
      await controller.captureAndResolveCurrentTask();
      await controller.captureAndResolveCurrentTask();
      expect(controller.needsManualReviewAny, isFalse);

      await controller.captureAndResolveCurrentTask();
      expect(controller.attemptsOf('front'), 3);
      expect(controller.needsManualReview('front'), isTrue);
      expect(controller.needsManualReviewAny, isTrue);
      expect(controller.lastError?.action, KycErrorAction.manualReview);
      expect(controller.manualReviewFolio, startsWith('KYC-'));
      // Sigue sin avanzar de tarea sin exito del servidor.
      expect(controller.currentStep, 'front');
    });

    test('limite configurable: con max 1 el primer fallo ya deriva', () async {
      final service = FakeKycService();
      final controller = KycFlowController(
        service: service,
        maxAttemptsPerTask: 1,
        taskEvaluator: (task, frames) async => false,
      );
      addTearDown(controller.dispose);

      await controller.loadChallenge();
      await controller.captureAndResolveCurrentTask();

      expect(controller.needsManualReviewAny, isTrue);
      expect(controller.manualReviewFolio, isNotNull);
    });
  });

  group('E1-T06 controller: fallo persistente muestra motivo del servidor', () {
    test('lastError conserva serverReason en lugar de un generico', () async {
      final service = FakeKycService();
      final controller = KycFlowController(
        service: service,
        taskEvaluator: (task, frames) async {
          throw ApiException(
            code: 'BIOMETRIC_FAILED',
            message: 'mensaje ui base',
            serverMessage: 'LOW_QUALITY: rostro poco iluminado',
            statusCode: 422,
          );
        },
      );
      addTearDown(controller.dispose);

      await controller.loadChallenge();
      await controller.captureAndResolveCurrentTask();

      expect(
        controller.lastError?.serverReason,
        contains('LOW_QUALITY'),
      );
      expect(controller.lastError?.action, KycErrorAction.retryTask);
    });
  });

  group('E1-T06 pantallas de estado', () {
    testWidgets('error accionable muestra mensaje + motivo + reintento',
        (tester) async {
      var retried = false;
      const error = KycErrorInfo(
        code: 'BIOMETRIC_FAILED',
        message: 'No pudimos verificar tu identidad. Vuelve a intentarlo.',
        action: KycErrorAction.retryTask,
        serverReason: 'LOW_QUALITY',
      );
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: KycActionableErrorView(
              error: error,
              onRetry: () => retried = true,
            ),
          ),
        ),
      );

      expect(find.textContaining('Vuelve a intentarlo'), findsOneWidget);
      // Motivo del servidor visible, no generico.
      expect(find.text('Motivo: LOW_QUALITY'), findsOneWidget);

      await tester.tap(find.text('Reintentar'));
      await tester.pump();
      expect(retried, isTrue);
    });

    testWidgets('revision manual muestra folio y motivo', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: KycManualReviewPage(
            folio: 'KYC-20260918-0001',
            reason: 'LOW_QUALITY',
          ),
        ),
      );

      expect(find.text('KYC-20260918-0001'), findsOneWidget);
      expect(find.text('Motivo: LOW_QUALITY'), findsOneWidget);
      expect(
        find.textContaining('revisión manual'),
        findsWidgets,
      );
    });
  });
}
