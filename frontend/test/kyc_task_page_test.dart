import 'dart:async';
import 'dart:typed_data';

import 'package:banca_online/core/widgets/app_button.dart';
import 'package:banca_online/core/widgets/loading_view.dart';
import 'package:banca_online/features/kyc/camera_frame_source.dart';
import 'package:banca_online/features/kyc/kyc_camera_preview.dart';
import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_frame_capture.dart';
import 'package:banca_online/features/kyc/kyc_frame_source.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_capture_overlay.dart';
import 'package:banca_online/features/kyc/presentation/kyc_task_page.dart';
import 'package:camera/camera.dart';
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

/// Simula [CameraFrameSource] SIN camara real: la 1.ª tarea "abre la camara"
/// con latencia (como en el dispositivo) y las siguientes resuelven al toque,
/// reproduciendo la carrera del 2.º step. Lanza [KycCameraUnavailable] para
/// caer en el fallback mock documentado (el hijo avisa `true` igualmente).
class _SlowFirstPreviewSource extends CameraFrameSource {
  int previewCalls = 0;

  @override
  Future<CameraController> previewControllerForTask(String task) async {
    previewCalls++;
    if (previewCalls == 1) {
      await Future<void>.delayed(const Duration(milliseconds: 300));
    }
    throw KycCameraUnavailable();
  }

  @override
  Future<List<Uint8List>> captureFramesForTask(String task) async =>
      generateMockFrames(task: task);
}

/// Fuente real simulada que permite BLOQUEAR la ráfaga ([captureGate]) para
/// observar el estado `busy` de la página (F-T25). El preview cae al
/// placeholder mock (sin cámara), como en CI.
class _GateSource extends CameraFrameSource {
  Completer<void>? captureGate;

  @override
  Future<CameraController> previewControllerForTask(String task) async {
    throw KycCameraUnavailable();
  }

  @override
  Future<List<Uint8List>> captureFramesForTask(String task) async {
    final gate = captureGate;
    if (gate != null) await gate.future;
    return generateMockFrames(task: task);
  }
}

AppPrimaryButton _captureButton(WidgetTester tester) =>
    tester.widget<AppPrimaryButton>(
      find.byKey(const Key('kycCaptureButton')),
    );

/// Toca el botón de captura con scroll previo (página F-T38 más alta que el
/// viewport de pruebas).
Future<void> _tapCapture(WidgetTester tester, String label) async {
  await tester.ensureVisible(find.text(label));
  await tester.tap(find.text(label));
  await tester.pumpAndSettle();
}

/// Igual que [_tapCapture] pero con un solo `pump` (para observar los estados
/// intermedios `Capturando...`/`Verificando...` sin asentar).
Future<void> _tapCaptureNoSettle(WidgetTester tester, String label) async {
  await tester.ensureVisible(find.text(label));
  await tester.tap(find.text(label));
  await tester.pump();
}

void main() {
  testWidgets(
      'regresion: al pasar a la 2.ª tarea liveness el preview nuevo habilita Capturar',
      (tester) async {
    final src = _SlowFirstPreviewSource();
    addTearDown(src.dispose);
    final c = KycFlowController(service: _FakeKycService(), frameSource: src);
    addTearDown(c.dispose);
    await c.loadChallenge();

    await tester.pumpWidget(
      MaterialApp(
        home: KycTaskPage(controller: c, prepDuration: Duration.zero),
      ),
    );
    await tester.pumpAndSettle();

    // Step 1: preview disponible -> Capturar habilitado.
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
    expect(_captureButton(tester).onPressed, isNotNull);

    await _tapCapture(tester, 'Capturar');

    // Step 2: el preview nuevo debe volver a habilitar Capturar.
    expect(find.textContaining('Paso 2 de 2'), findsOneWidget);
    expect(_captureButton(tester).onPressed, isNotNull);
  });

  testWidgets(
      'UX: cuenta regresiva de preparacion por tarea antes de habilitar Capturar',
      (tester) async {
    final src = _SlowFirstPreviewSource();
    addTearDown(src.dispose);
    final c = KycFlowController(service: _FakeKycService(), frameSource: src);
    addTearDown(c.dispose);
    await c.loadChallenge();

    await tester.pumpWidget(
      MaterialApp(
        home: KycTaskPage(
          controller: c,
          prepDuration: const Duration(seconds: 2),
        ),
      ),
    );
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();

    expect(find.textContaining('Preparate'), findsOneWidget);
    expect(_captureButton(tester).onPressed, isNull);

    await tester.pump(const Duration(seconds: 2));
    await tester.pump();

    expect(find.textContaining('Preparate'), findsNothing);
    expect(_captureButton(tester).onPressed, isNotNull);
  });

  group('F-T25 preview en vivo durante busy', () {
    testWidgets(
        'busy mantiene el viewfinder, muestra overlay Capturando/Verificando '
        'y deshabilita Capturar (sin doble captura)', (tester) async {
      final src = _GateSource();
      addTearDown(src.dispose);
      final evalCompleter = Completer<KycTaskEvaluation>();
      final c = KycFlowController(
        service: _FakeKycService(),
        frameSource: src,
        detailedTaskEvaluator: (task, frames) => evalCompleter.future,
      );
      addTearDown(c.dispose);
      await c.loadChallenge();

      await tester.pumpWidget(
        MaterialApp(
          home: KycTaskPage(controller: c, prepDuration: Duration.zero),
        ),
      );
      await tester.pumpAndSettle();

      // Estado base: viewfinder (placeholder mock) montado, sin overlay.
      expect(find.byType(KycPreviewPlaceholder), findsOneWidget);
      expect(find.byType(KycCaptureOverlay), findsNothing);

      // Bloquear la ráfaga para observar la fase "Capturando...".
      src.captureGate = Completer<void>();
      await _tapCaptureNoSettle(tester, 'Capturar');

      expect(find.text('Capturando...'), findsOneWidget);
      expect(find.byType(KycPreviewPlaceholder), findsOneWidget);
      expect(find.byType(LoadingView), findsNothing);
      // La instrucción del paso permanece visible.
      expect(find.textContaining('Mira de frente'), findsOneWidget);
      // Sin doble captura: botón deshabilitado mientras busy.
      expect(_captureButton(tester).onPressed, isNull);

      // Liberar la ráfaga -> pasa a "Verificando..." (evaluación en curso).
      src.captureGate!.complete();
      src.captureGate = null;
      await tester.pump();
      await tester.pump();

      expect(find.text('Verificando...'), findsOneWidget);
      expect(find.byType(KycPreviewPlaceholder), findsOneWidget);
      expect(find.byType(LoadingView), findsNothing);
      expect(_captureButton(tester).onPressed, isNull);

      evalCompleter.complete(
        const KycTaskEvaluation(step: 'front', passed: true),
      );
      await tester.pumpAndSettle();

      expect(find.byType(KycCaptureOverlay), findsNothing);
      expect(find.textContaining('Paso 2 de 2'), findsOneWidget);
    });

    testWidgets(
        'tras un fallo se conserva la tarea, desaparece el overlay y se '
        'puede reintentar', (tester) async {
      var calls = 0;
      final src = _GateSource();
      addTearDown(src.dispose);
      final c = KycFlowController(
        service: _FakeKycService(),
        frameSource: src,
        detailedTaskEvaluator: (task, frames) async {
          calls++;
          return calls == 1
              ? const KycTaskEvaluation(
                  step: 'front',
                  passed: false,
                  reason: 'NO_BLINK',
                )
              : const KycTaskEvaluation(step: 'front', passed: true);
        },
      );
      addTearDown(c.dispose);
      await c.loadChallenge();

      await tester.pumpWidget(
        MaterialApp(
          home: KycTaskPage(controller: c, prepDuration: Duration.zero),
        ),
      );
      await tester.pumpAndSettle();

      await _tapCapture(tester, 'Capturar');
      await tester.pumpAndSettle();

      // Sigue en la MISMA tarea, con error visible y sin overlay.
      expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
      expect(find.byType(KycCaptureOverlay), findsNothing);
      expect(find.textContaining('Reintentar captura'), findsOneWidget);
      expect(_captureButton(tester).onPressed, isNotNull);

      await _tapCapture(tester, 'Reintentar captura');
      expect(find.textContaining('Paso 2 de 2'), findsOneWidget);
    });
  });

  group('instructionFor', () {
    test('cubre los pasos reales del microservicio', () {
      expect(KycTaskPage.instructionFor('arriba'), contains('arriba'));
      expect(KycTaskPage.instructionFor('abajo'), contains('abajo'));
      expect(KycTaskPage.instructionFor('izquierda'), contains('izquierda'));
      expect(KycTaskPage.instructionFor('derecha'), contains('derecha'));
      expect(KycTaskPage.instructionFor('parpadeo'), contains('Parpadea'));
    });

    test('mantiene las instrucciones existentes', () {
      expect(KycTaskPage.instructionFor('front'), contains('frente'));
      expect(KycTaskPage.instructionFor('blink'), contains('Parpadea'));
      expect(KycTaskPage.instructionFor('smile'), contains('Sonrie'));
      expect(KycTaskPage.instructionFor('turn_left'), contains('izquierda'));
    });
  });
}
