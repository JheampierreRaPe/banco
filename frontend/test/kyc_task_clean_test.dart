import 'dart:typed_data';

import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_task_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

class _FakeKycService implements KycService {
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
    required KycApplicant applicant,
    required Map<String, List<Uint8List>> framesByTask,
  }) async =>
      const KycSubmitResult(overallResult: true, detailCode: 'OK');
}

Future<void> _tapTaskButton(WidgetTester tester, String label) async {
  await tester.ensureVisible(find.text(label));
  await tester.tap(find.text(label));
  await tester.pumpAndSettle();
}

void main() {
  group('F-T48 limpieza de kyc-task (CA-01/CA-02)', () {
    testWidgets('sin checklist: no muestra ni reserva espacio', (tester) async {
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await c.loadChallenge();

      await tester.pumpWidget(
        MaterialApp(
          home: KycTaskPage(controller: c, prepDuration: Duration.zero),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Buena iluminación'), findsNothing);
      expect(find.text('Rostro descubierto'), findsNothing);
      expect(find.textContaining('Prueba de vida'), findsNothing);
    });

    testWidgets('con tareas pendientes el boton de captura se mantiene',
        (tester) async {
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await c.loadChallenge();

      await tester.pumpWidget(
        MaterialApp(
          home: KycTaskPage(controller: c, prepDuration: Duration.zero),
        ),
      );
      await tester.pumpAndSettle();

      expect(
        find.byKey(const Key('kycCaptureButton')),
        findsOneWidget,
      );
      expect(find.text('Capturar'), findsOneWidget);
      expect(find.text('Enviar verificación'), findsNothing);
    });

    testWidgets('ultima foto: solo Enviar verificación, sin captura',
        (tester) async {
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await c.loadChallenge();
      await c.captureAndResolveCurrentTask();
      await c.captureAndResolveCurrentTask();
      expect(c.readyToSubmit, isTrue);

      await tester.pumpWidget(
        MaterialApp(
          home: KycTaskPage(controller: c, prepDuration: Duration.zero),
        ),
      );
      await tester.pumpAndSettle();

      expect(
        find.byKey(const Key('kycCaptureButton')),
        findsNothing,
      );
      expect(find.text('Reintentar captura'), findsNothing);
      expect(find.text('Enviar verificación'), findsOneWidget);
    });
  });

  group('F-T48 card de error sin reintento (CA-03)', () {
    testWidgets('sin boton de reintento en el card y con paso fallido',
        (tester) async {
      final c = KycFlowController(
        service: _FakeKycService(),
        detailedTaskEvaluator: (task, frames) async =>
            const KycTaskEvaluation(
          step: 'front',
          passed: false,
          reason: 'NO_BLINK',
        ),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();

      await tester.pumpWidget(
        MaterialApp(
          home: KycTaskPage(controller: c, prepDuration: Duration.zero),
        ),
      );
      await tester.pumpAndSettle();

      await _tapTaskButton(tester, 'Capturar');

      expect(find.text('No pudimos verificarte'), findsOneWidget);
      // El card no lleva acción de reintento.
      expect(find.text('Reintentar'), findsNothing);
      // Paso fallido visible y motivo traducido (no es duplicado).
      expect(find.byKey(const Key('kycFailedStep')), findsOneWidget);
      expect(
        tester.widget<Text>(find.byKey(const Key('kycFailedStep'))).data,
        contains('front'),
      );
      expect(find.byKey(const Key('kycReasonMessage')), findsOneWidget);
      expect(
        tester.widget<Text>(find.byKey(const Key('kycReasonMessage'))).data,
        contains('parpadeo'),
      );
      // El usuario no queda atascado: el botón principal sigue ofreciendo
      // el reintento de la misma tarea (no es la última foto).
      expect(find.text('Reintentar captura'), findsOneWidget);
    });

    testWidgets('motivo documento-registrado: se oculta la linea Motivo',
        (tester) async {
      final c = KycFlowController(
        service: _FakeKycService(),
        detailedTaskEvaluator: (task, frames) async =>
            const KycTaskEvaluation(
          step: 'front',
          passed: false,
          reason: 'DUPLICATE_DOCUMENT',
        ),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();

      await tester.pumpWidget(
        MaterialApp(
          home: KycTaskPage(controller: c, prepDuration: Duration.zero),
        ),
      );
      await tester.pumpAndSettle();

      await _tapTaskButton(tester, 'Capturar');

      expect(find.text('No pudimos verificarte'), findsOneWidget);
      expect(find.byKey(const Key('kycFailedStep')), findsOneWidget);
      expect(find.byKey(const Key('kycReasonMessage')), findsNothing);
      expect(find.text('Reintentar'), findsNothing);
      expect(find.text('Reintentar captura'), findsOneWidget);
    });
  });

  group('F-T48 swap solo-texto (CA-04)', () {
    test('arriba muestra el texto de abajo y viceversa', () {
      // Swap SOLO de presentación: el microservicio tiene el mapeo
      // invertido y NO se corrige (`kyc-service/` intacto).
      expect(
        KycTaskPage.instructionFor('arriba'),
        'Baja la cabeza y mira hacia abajo.',
      );
      expect(
        KycTaskPage.instructionFor('abajo'),
        'Levanta la cabeza y mira hacia arriba.',
      );
    });
  });
}
