import 'dart:typed_data';

import 'package:banca_online/features/kyc/camera_frame_source.dart';
import 'package:banca_online/features/kyc/kyc_camera_preview.dart';
import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_document_page.dart';
import 'package:camera/camera.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

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

/// Doble de [CameraController] SIN plataforma: registra el preset con el que
/// se construyo, cuenta `takePicture` y falla si se le usa tras `dispose`.
class _FakeCameraController extends CameraController {
  _FakeCameraController({
    required CameraLensDirection lens,
    required this.preset,
  }) : super(
          lens == CameraLensDirection.front ? _front : _back,
          preset,
        );

  final ResolutionPreset preset;
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

/// Fuente real con dobles: registra el preset por lente y las capturas.
class _RecordingSource {
  _RecordingSource({
    ResolutionPreset documentResolution = ResolutionPreset.high,
    List<CameraDescription> cameras = const <CameraDescription>[_front, _back],
  }) {
    source = CameraFrameSource(
      documentResolution: documentResolution,
      burstFrames: 2,
      frameInterval: Duration.zero,
      burstDelay: (_) async {},
      camerasLoader: () async => cameras,
      controllerFactory: (description, resolution) {
        presetsByLens
            .putIfAbsent(description.name, () => <ResolutionPreset>[])
            .add(resolution);
        final controller = _FakeCameraController(
          lens: description.lensDirection,
          preset: resolution,
        );
        controllers.add(controller);
        return controller;
      },
    );
  }

  final List<_FakeCameraController> controllers = <_FakeCameraController>[];
  final Map<String, List<ResolutionPreset>> presetsByLens =
      <String, List<ResolutionPreset>>{};
  late final CameraFrameSource source;

  int get pictures =>
      controllers.fold<int>(0, (sum, controller) => sum + controller.pictures);

  bool get anyUsedAfterDispose =>
      controllers.any((controller) => controller.usedAfterDispose);
}

void main() {
  group('CameraFrameSource: resolucion del documento (F-T27)', () {
    test('por defecto documento = high y liveness = medium', () {
      final source = CameraFrameSource();
      addTearDown(source.dispose);
      expect(source.documentResolution, ResolutionPreset.high);
      expect(source.resolution, ResolutionPreset.medium);
    });

    test('la captura del DOCUMENTO abre el controller con preset alto',
        () async {
      final rec = _RecordingSource();
      addTearDown(rec.source.dispose);

      await rec.source.captureDocumentFrame();

      expect(rec.presetsByLens['fake-back'], <ResolutionPreset>[
        ResolutionPreset.high,
      ]);
    });

    test('el LIVENESS conserva medium', () async {
      final rec = _RecordingSource();
      addTearDown(rec.source.dispose);

      await rec.source.previewControllerForTask('front');

      expect(rec.presetsByLens['fake-front'], <ResolutionPreset>[
        ResolutionPreset.medium,
      ]);
    });

    test('documentResolution es inyectable (veryHigh)', () async {
      final rec = _RecordingSource(
        documentResolution: ResolutionPreset.veryHigh,
      );
      addTearDown(rec.source.dispose);

      await rec.source.captureDocumentFrame();

      expect(rec.presetsByLens['fake-back'], <ResolutionPreset>[
        ResolutionPreset.veryHigh,
      ]);
    });
  });

  group('KycDocumentPage: re-captura con preview visible (F-T27)', () {
    testWidgets(
        'retake re-monta el preview en vivo y permite capturar de nuevo',
        (tester) async {
      final rec = _RecordingSource();
      addTearDown(rec.source.dispose);
      final controller = KycFlowController(
        service: const _FakeKycService(),
        frameSource: rec.source,
        documentValidator: (image) async =>
            const KycDocumentValidation(isValid: false, issues: ['BLURRY']),
      );
      addTearDown(controller.dispose);
      await controller.loadChallenge();

      await tester.pumpWidget(
        MaterialApp(home: KycDocumentPage(controller: controller)),
      );
      await tester.pumpAndSettle();

      // Preview en vivo montado antes de capturar.
      expect(find.byType(KycCameraPreview), findsOneWidget);

      await tester
          .ensureVisible(find.byKey(const Key('captureDocumentButton')));
      await tester.tap(find.byKey(const Key('captureDocumentButton')));
      await tester.pumpAndSettle();

      // Invalido: issues visibles, preview retirado y UNA captura hecha.
      expect(find.byKey(const Key('kycDocumentIssues')), findsOneWidget);
      expect(find.byType(KycCameraPreview), findsNothing);
      expect(rec.pictures, 1);

      // UNA sola accion de re-captura: no hay CTA duplicado con
      // comportamientos distintos (el bug del validador).
      expect(find.byKey(const Key('recaptureDocumentButton')), findsOneWidget);
      expect(find.byKey(const Key('captureDocumentButton')), findsNothing);
      expect(find.text('Volver a capturar'), findsOneWidget);
      expect(find.text('Volver a capturar documento'), findsNothing);

      // Retake: limpia captura/validacion y re-monta el preview SIN capturar.
      await tester
          .ensureVisible(find.byKey(const Key('recaptureDocumentButton')));
      await tester.tap(find.byKey(const Key('recaptureDocumentButton')));
      await tester.pumpAndSettle();
      expect(find.byType(KycCameraPreview), findsOneWidget);
      expect(controller.hasDocumentImage, isFalse);
      expect(find.byKey(const Key('kycDocumentIssues')), findsNothing);
      expect(rec.pictures, 1);

      // Tras el reset, la MISMA accion cambia a captura normal.
      expect(find.byKey(const Key('captureDocumentButton')), findsOneWidget);
      expect(find.byKey(const Key('recaptureDocumentButton')), findsNothing);

      // Nueva captura habilitada: SEGUNDA foto, sin tocar controller disposed.
      await tester
          .ensureVisible(find.byKey(const Key('captureDocumentButton')));
      await tester.tap(find.byKey(const Key('captureDocumentButton')));
      await tester.pumpAndSettle();
      expect(rec.pictures, 2);
      expect(rec.anyUsedAfterDispose, isFalse);
      expect(tester.takeException(), isNull);
    });
  });
}

class _FakeKycService implements KycService {
  const _FakeKycService();

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
