import 'dart:typed_data';

import 'package:banca_online/features/kyc/camera_frame_source.dart';
import 'package:banca_online/features/kyc/kyc_camera_preview.dart';import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_frame_source.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_task_page.dart';
import 'package:camera/camera.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

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
    required Map<String, List<Uint8List>> framesByTask,
  }) async =>
      const KycSubmitResult(overallResult: true, detailCode: 'OK');
}

GoRouter _router(KycFlowController c) => GoRouter(
      initialLocation: '/kyc/task',
      routes: [
        GoRoute(
          path: '/kyc/task',
          builder: (context, state) => KycTaskPage(controller: c),
        ),
      ],
    );

void main() {
  test('frameSource expuesto: por defecto es mock (sin preview real)', () {
    final c = KycFlowController(service: _FakeKycService());
    addTearDown(c.dispose);
    expect(c.frameSource, isA<MockKycFrameSource>());
  });

  testWidgets('con fuente mock: placeholder sin CameraPreview y Capturar activo',
      (tester) async {
    final c = KycFlowController(service: _FakeKycService());
    addTearDown(c.dispose);
    await c.loadChallenge();
    await tester.pumpWidget(MaterialApp.router(routerConfig: _router(c)));
    await tester.pumpAndSettle();

    // Placeholder histórico visible, sin preview real (no testeable en CI).
    expect(find.byType(KycPreviewPlaceholder), findsOneWidget);
    expect(find.byType(KycCameraPreview), findsNothing);
    expect(find.byType(CameraPreview), findsNothing);

    // El botón Capturar NO queda bloqueado por el preview con mock.
    final button = tester.widget<FilledButton>(
      find.ancestor(
        of: find.text('Capturar'),
        matching: find.byType(FilledButton),
      ),
    );
    expect(button.onPressed, isNotNull);
  });

  group('KycCameraPreview anti-bloqueo (fuente real con doubles)', () {
    testWidgets('error genérico crudo → placeholder mock, nunca spinner',
        (tester) async {
      final src = _GenericErrorSource();
      addTearDown(src.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: KycCameraPreview(task: 'front', source: src),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Iniciando cámara…'), findsNothing);
      expect(find.byType(KycPreviewPlaceholder), findsOneWidget);
      expect(find.text('Reintentar'), findsOneWidget);
      expect(src.calls, 1);
    });

    testWidgets('timeout simulado → unavailable con reintento', (tester) async {
      final src = _TimeoutSource();
      addTearDown(src.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: KycCameraPreview(task: 'front', source: src),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Iniciando cámara…'), findsNothing);
      expect(find.byType(KycPreviewPlaceholder), findsOneWidget);
      expect(find.text('Reintentar'), findsOneWidget);
    });

    testWidgets('permiso denegado → mensaje + reintento', (tester) async {
      final src = _DeniedSource();
      addTearDown(src.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: KycCameraPreview(task: 'front', source: src),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Iniciando cámara…'), findsNothing);
      expect(find.textContaining('Permiso'), findsOneWidget);
      expect(find.text('Reintentar'), findsOneWidget);
    });

    testWidgets('Reintentar re-ejecuta la inicialización sin atascarse',
        (tester) async {
      final src = _DeniedSource();
      addTearDown(src.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: KycCameraPreview(task: 'front', source: src),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(src.calls, 1);

      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();

      expect(src.calls, 2);
      expect(find.text('Iniciando cámara…'), findsNothing);
      expect(find.textContaining('Permiso'), findsOneWidget);
    });
  });

  test('openTimeout por defecto ≈ 15 s (anti spinner infinito)', () {
    final src = CameraFrameSource();
    addTearDown(src.dispose);
    expect(src.openTimeout, const Duration(seconds: 15));
  });
}

/// Simula un error CRUDO no-CameraException (p. ej. PlatformException /
/// MissingPluginException de availableCameras() en físico sin cámara).
class _GenericErrorSource extends CameraFrameSource {
  int calls = 0;

  @override
  Future<CameraController> previewControllerForTask(String task) async {
    calls++;
    throw Exception('boom crudo: canal de cámara no disponible');
  }
}

/// Simula el camino del timeout: la fuente real convierte el cuelgue en
/// [KycCameraUnavailable] con mensaje claro (ver [CameraFrameSource]).
class _TimeoutSource extends CameraFrameSource {
  @override
  Future<CameraController> previewControllerForTask(String task) async {
    await Future<void>.delayed(const Duration(milliseconds: 50));
    throw KycCameraUnavailable(
      'La cámara tardó demasiado en responder (15 s). '
      'Se usará captura simulada.',
    );
  }
}

class _DeniedSource extends CameraFrameSource {
  int calls = 0;

  @override
  Future<CameraController> previewControllerForTask(String task) async {
    calls++;
    throw KycCameraPermissionDenied();
  }
}
