import 'dart:typed_data';

import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_result_page.dart';
import 'package:banca_online/features/kyc/presentation/kyc_start_page.dart';
import 'package:banca_online/features/kyc/presentation/kyc_task_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

class FakeKycService implements KycService {
  const FakeKycService();

  @override
  Future<KycChallenge> challenge() async => const KycChallenge(
        token: 'tok-abc',
        steps: ['front', 'blink'],
        expiresIn: 300,
      );

  @override
  Future<KycSubmitResult> submit({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required Map<String, List<Uint8List>> framesByTask,
  }) async =>
      const KycSubmitResult(overallResult: true, detailCode: 'OK');
}

/// Router de prueba con las 3 paginas compartiendo [controller].
GoRouter _testRouter(KycFlowController controller) => GoRouter(
      initialLocation: '/kyc',
      routes: [
        GoRoute(
          path: '/kyc',
          builder: (context, state) => KycStartPage(controller: controller),
        ),
        GoRoute(
          path: '/kyc/task',
          builder: (context, state) => KycTaskPage(controller: controller),
        ),
        GoRoute(
          path: '/kyc/result',
          builder: (context, state) => KycResultPage(controller: controller),
        ),
      ],
    );

Future<void> _pumpKyc(
  WidgetTester tester,
  KycFlowController controller,
) async {
  await tester.pumpWidget(
    MaterialApp.router(routerConfig: _testRouter(controller)),
  );
  await tester.pumpAndSettle();
}

Future<void> _completeStartPage(WidgetTester tester) async {
  await tester.enterText(
    find.byKey(const Key('docNumberField')),
    '12345678',
  );
  await tester.tap(find.text('Continuar'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('start valida numero vacio y no pide desafio', (tester) async {
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await tester.tap(find.text('Continuar'));
    await tester.pumpAndSettle();

    expect(find.text('Ingresa el numero de documento'), findsOneWidget);
    expect(controller.challenge, isNull);
  });

  testWidgets('flujo completo happy path hasta resultado exitoso',
      (tester) async {
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);

    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Paso 2 de 2'), findsOneWidget);

    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    expect(find.text('Enviar verificacion'), findsOneWidget);

    await tester.tap(find.text('Enviar verificacion'));
    await tester.pumpAndSettle();
    expect(find.text('Verificacion exitosa'), findsOneWidget);
  });

  testWidgets('reintento tras passed:false conserva la misma tarea',
      (tester) async {
    var calls = 0;
    final controller = KycFlowController(
      service: const FakeKycService(),
      taskEvaluator: (task, frames) async => ++calls >= 2,
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);

    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    // Sigue en la misma tarea con mensaje de reintento.
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
    expect(find.textContaining('misma tarea'), findsOneWidget);

    await tester.tap(find.text('Reintentar captura'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Paso 2 de 2'), findsOneWidget);
  });

  testWidgets('resultado fallido muestra el motivo del servidor',
      (tester) async {
    final controller = KycFlowController(service: _FailingSubmitService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Enviar verificacion'));
    await tester.pumpAndSettle();

    expect(find.textContaining('Motivo: LOW_QUALITY'), findsOneWidget);
    expect(find.text('Reintentar verificacion'), findsOneWidget);
  });
}

class _FailingSubmitService implements KycService {
  @override
  Future<KycChallenge> challenge() async => const KycChallenge(
        token: 'tok-abc',
        steps: ['front', 'blink'],
        expiresIn: 300,
      );

  @override
  Future<KycSubmitResult> submit({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required Map<String, List<Uint8List>> framesByTask,
  }) async =>
      const KycSubmitResult(overallResult: false, detailCode: 'LOW_QUALITY');
}
