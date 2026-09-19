import 'dart:typed_data';

import 'package:banca_online/features/kyc/camera_frame_source.dart';
import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_frame_source.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_document_page.dart';
import 'package:banca_online/features/kyc/presentation/kyc_task_page.dart';
import 'package:camera/camera.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

const CameraDescription _front = CameraDescription(
  name: 'fake-front',
  lensDirection: CameraLensDirection.front,
  sensorOrientation: 0,
);

const CameraDescription _back = CameraDescription(
  name: 'fake-back',
  lensDirection: CameraLensDirection.back,
  sensorOrientation: 0,
);

/// Doble de [CameraController] SIN plataforma: registra `takePicture`/`dispose`
/// y falla con el error real del plugin si se le usa tras `dispose`.
///
/// Reproduce en CI el defecto "A CameraController was used after being
/// disposed." sin camara fisica (F-T24).
class _FakeCameraController extends CameraController {
  _FakeCameraController({required CameraLensDirection lens})
      : super(
          lens == CameraLensDirection.front ? _front : _back,
          ResolutionPreset.low,
        );

  bool disposed = false;
  bool usedAfterDispose = false;
  int pictures = 0;

  @override
  Future<void> initialize() async {
    value = value.copyWith(
      isInitialized: true,
      previewSize: const Size(720, 1280),
    );
  }

  @override
  Future<XFile> takePicture() async {
    if (disposed) {
      usedAfterDispose = true;
      throw CameraException(
        'Disposed CameraController',
        'A CameraController was used after being disposed.',
      );
    }
    pictures++;
    return XFile.fromData(
      Uint8List.fromList(<int>[
        0xFF, 0xD8, 0xFF, 0xE0,
        ...List<int>.filled(32, 0x7A),
      ]),
      name: 'frame.jpg',
      mimeType: 'image/jpeg',
    );
  }

  @override
  Widget buildPreview() => const SizedBox.shrink();

  @override
  Future<void> dispose() async {
    disposed = true;
    await super.dispose();
  }
}

CameraFrameSource _source({
  int burstFrames = 3,
  KycBurstDelay? burstDelay,
  List<_FakeCameraController>? created,
}) =>
    CameraFrameSource(
      burstFrames: burstFrames,
      frameInterval: Duration.zero,
      burstDelay: burstDelay ?? (_) async {},
      camerasLoader: () async => const <CameraDescription>[_front, _back],
      controllerFactory: (description, resolution) {
        final controller = _FakeCameraController(
          lens: description.lensDirection,
        );
        created?.add(controller);
        return controller;
      },
    );

void main() {
  group('CameraFrameSource: ciclo de vida seguro (F-T24)', () {
    test(
        'regresion: recrear el preview durante la rafaga no usa un controller disposeado',
        () async {
      final created = <_FakeCameraController>[];
      late final CameraFrameSource source;
      var previewRecreations = 0;
      source = _source(
        created: created,
        // Cambio de lente (documento -> tareas) EN MEDIO de la rafaga, como
        // cuando el preview se re-crea mientras la captura sigue en curso.
        burstDelay: (_) async {
          previewRecreations++;
          await source.previewControllerForTask('document');
        },
      );
      addTearDown(source.dispose);

      final frames = await source.captureFramesForTask('front');

      expect(frames.length, 3);
      expect(previewRecreations, 2);
      expect(
        created.any((c) => c.usedAfterDispose),
        isFalse,
        reason: 'ningun takePicture debe tocar un controller disposeado',
      );
      // El controller de la rafaga (frontal) se cierra solo al terminar.
      expect(created.first.disposed, isTrue);
    });

    test(
        'apertura concurrente: dos previewControllerForTask comparten un solo controller',
        () async {
      var created = 0;
      final source = CameraFrameSource(
        camerasLoader: () async => const <CameraDescription>[_front],
        controllerFactory: (description, resolution) {
          created++;
          return _FakeCameraController(lens: description.lensDirection);
        },
      );
      addTearDown(source.dispose);

      final first = source.previewControllerForTask('front');
      final second = source.previewControllerForTask('front');
      final c1 = await first;
      final c2 = await second;

      expect(identical(c1, c2), isTrue);
      expect(created, 1, reason: 'el mutex evita crear dos controllers');
      expect(source.generation, 1);
    });

    test('cambio de lente cierra el controller anterior y publica uno nuevo',
        () async {
      final source = _source();
      addTearDown(source.dispose);

      final back = await source.previewControllerForTask('document');
      expect(source.generation, 1);
      final front = await source.previewControllerForTask('front');

      expect(identical(back, front), isFalse);
      expect(source.generation, 2);
      expect(source.isControllerCurrent(front), isTrue);
      expect(source.isControllerCurrent(back), isFalse);
      // El retiro se cierra de forma diferida, sin tocarlo en uso.
      await Future<void>.delayed(Duration.zero);
      expect((back as _FakeCameraController).disposed, isTrue);
    });

    test(
        're-captura: alternar documento/frontal entre capturas cambia de generacion sin excepcion',
        () async {
      final created = <_FakeCameraController>[];
      final source = _source(created: created);
      addTearDown(source.dispose);

      // Documento (trasera) -> preview frontal -> re-captura documento: la
      // generacion sube y ningun takePicture debe tocar un controller cerrado.
      final first = await source.captureDocumentFrame();
      expect(first, isNotEmpty);
      expect(source.generation, 1);

      await source.previewControllerForTask('front');
      expect(source.generation, 2);

      final second = await source.captureDocumentFrame();
      expect(second, isNotEmpty);
      expect(source.generation, 3);

      expect(
        created.any((c) => c.usedAfterDispose),
        isFalse,
        reason: 'la re-captura no debe usar un controller disposeado',
      );
    });

    test(
        'dispose de la fuente: la operacion posterior lanza KycCameraUnavailable y no toca el controller',
        () async {
      final source = _source();
      final controller = await source.previewControllerForTask('front');

      await source.dispose();
      expect((controller as _FakeCameraController).disposed, isTrue);

      await expectLater(
        source.captureFramesForTask('front'),
        throwsA(isA<KycCameraUnavailable>()),
      );
      await expectLater(
        source.previewControllerForTask('front'),
        throwsA(isA<KycCameraUnavailable>()),
      );
      expect(controller.usedAfterDispose, isFalse);
    });
  });

  group('Navegacion documento -> tareas (F-T24)', () {
    testWidgets(
        'documento (trasera) -> tareas (frontal) y cambio de paso sin error',
        (tester) async {
      final created = <_FakeCameraController>[];
      final source = _source(burstFrames: 2, created: created);
      addTearDown(source.dispose);
      final controller = KycFlowController(
        service: _FakeKycService(),
        frameSource: source,
      );
      addTearDown(controller.dispose);
      await controller.loadChallenge();

      await tester.pumpWidget(
        MaterialApp.router(routerConfig: _router(controller)),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);

      // Captura del documento con lente trasera y navega a las tareas.
      final captureDocument = find.byKey(const Key('captureDocumentButton'));
      await tester.ensureVisible(captureDocument);
      await tester.pumpAndSettle();
      await tester.tap(captureDocument);
      await tester.pumpAndSettle();
      expect(find.text('Prueba de vida'), findsOneWidget);
      expect(tester.takeException(), isNull);

      // Paso 1 (frontal): capturar y avanzar de paso sin excepcion.
      final capture = find.text('Capturar');
      await tester.ensureVisible(capture);
      await tester.pumpAndSettle();
      await tester.tap(capture);
      await tester.pumpAndSettle();
      expect(find.textContaining('Paso 2 de 2'), findsOneWidget);
      expect(tester.takeException(), isNull);

      expect(
        created.any((c) => c.usedAfterDispose),
        isFalse,
        reason: 'la navegacion documento -> tareas no debe usar un disposed',
      );
    });
  });
}

GoRouter _router(KycFlowController controller) => GoRouter(
      initialLocation: '/kyc/document',
      routes: [
        GoRoute(
          path: '/kyc/document',
          builder: (context, state) =>
              KycDocumentPage(controller: controller),
        ),
        GoRoute(
          path: '/kyc/task',
          builder: (context, state) => KycTaskPage(
            controller: controller,
            prepDuration: Duration.zero,
          ),
        ),
      ],
    );

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
