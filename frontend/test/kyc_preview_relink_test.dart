import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:banca_online/features/kyc/camera_frame_source.dart';
import 'package:banca_online/features/kyc/kyc_camera_preview.dart';
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

/// Doble de [CameraController] SIN plataforma (F-T24): identifica su lente,
/// registra `dispose` y falla si se usa despues.
class _FakeCameraController extends CameraController {
  _FakeCameraController({required CameraLensDirection lens, required this.tag})
      : _lens = lens,
        super(
          lens == CameraLensDirection.front ? _front : _back,
          ResolutionPreset.low,
        );

  final CameraLensDirection _lens;
  final String tag;
  bool disposed = false;
  bool usedAfterDispose = false;

  CameraLensDirection get lens => _lens;

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

/// Fuente real con dobles que registra las llamadas al preview por tarea.
class _TraceSource extends CameraFrameSource {
  factory _TraceSource({int burstFrames = 2, KycBurstDelay? burstDelay}) {
    final created = <_FakeCameraController>[];
    return _TraceSource._(
      created,
      burstFrames: burstFrames,
      burstDelay: burstDelay,
    );
  }

  _TraceSource._(
    this.created, {
    super.burstFrames = 2,
    KycBurstDelay? burstDelay,
  }) : super(
          frameInterval: Duration.zero,
          burstDelay: burstDelay ?? (_) async {},
          camerasLoader: () async =>
              const <CameraDescription>[_front, _back],
          controllerFactory: (description, resolution) {
            final controller = _FakeCameraController(
              lens: description.lensDirection,
              tag: description.name,
            );
            created.add(controller);
            return controller;
          },
        );

  final List<_FakeCameraController> created;
  final List<String> previewCalls = <String>[];

  bool get anyUsedAfterDispose =>
      created.any((controller) => controller.usedAfterDispose);

  @override
  Future<CameraController> previewControllerForTask(String task) {
    previewCalls.add(task);
    return super.previewControllerForTask(task);
  }
}

_FakeCameraController _currentFront(_TraceSource source) {
  final current = source.previewController;
  expect(current, isNotNull, reason: 'debe haber un controller vigente');
  expect(source.isControllerCurrent(current!), isTrue);
  final fake = current as _FakeCameraController;
  expect(fake.lens, CameraLensDirection.front);
  return fake;
}

void main() {
  group('F-T28: re-ligado del preview al controller vigente', () {
    testWidgets(
        'documento (trasera) -> liveness (frontal) re-liga el preview a la '
        'lente frontal vigente', (tester) async {
      final source = _TraceSource();
      addTearDown(source.dispose);
      final controller = KycFlowController(
        service: const _FakeKycService(),
        frameSource: source,
        documentValidator: (image) async =>
            const KycDocumentValidation(isValid: true),
      );
      addTearDown(controller.dispose);
      await controller.loadChallenge();

      final router = GoRouter(
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

      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      // Documento: preview en vivo con la lente trasera.
      expect(find.byType(CameraPreview), findsOneWidget);
      expect(source.generation, 1);
      expect(
        (source.previewController! as _FakeCameraController).lens,
        CameraLensDirection.back,
      );

      await tester
          .ensureVisible(find.byKey(const Key('captureDocumentButton')));
      await tester.tap(find.byKey(const Key('captureDocumentButton')));
      await tester.pumpAndSettle();

      // Liveness: el preview se re-liga a la lente frontal vigente (nueva
      // generacion), sin placeholder ni superficie del controller anterior.
      expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
      expect(find.byType(CameraPreview), findsOneWidget);
      expect(find.byType(KycPreviewPlaceholder), findsNothing);
      expect(source.previewCalls, contains('front'));
      expect(source.generation, greaterThanOrEqualTo(2));
      _currentFront(source);
      expect(source.anyUsedAfterDispose, isFalse);
      expect(tester.takeException(), isNull);
    });

    testWidgets(
        'cambio de tarea re-liga el preview a la lente vigente (flujo real)',
        (tester) async {
      final source = _TraceSource();
      addTearDown(source.dispose);

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: KycCameraPreview(task: 'document', source: source),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byType(CameraPreview), findsOneWidget);
      expect(
        (source.previewController! as _FakeCameraController).lens,
        CameraLensDirection.back,
      );

      // Cambia la tarea (documento -> liveness): `didUpdateWidget` re-liga el
      // preview a la nueva lente. La ligadura ocurre por el flujo real (cambio
      // de tarea), no por realimentacion de la fuente (F-T32 elimino el
      // listener de generacion).
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: KycCameraPreview(task: 'front', source: source),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byType(CameraPreview), findsOneWidget);
      expect(find.byType(KycPreviewPlaceholder), findsNothing);
      final previewWidget =
          tester.widget<CameraPreview>(find.byType(CameraPreview));
      final current = _currentFront(source);
      expect(identical(previewWidget.controller, current), isTrue,
          reason: 'el preview apunta al controller vigente');
      expect(source.previewCalls, contains('front'));
      expect(source.anyUsedAfterDispose, isFalse);
      expect(tester.takeException(), isNull);
    });

    testWidgets(
        'frontal: monta el preview con un controller inicializado y vigente '
        '(el area NO queda vacia)', (tester) async {
      final source = _TraceSource();
      addTearDown(source.dispose);

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: KycCameraPreview(task: 'front', source: source),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // `CameraPreview` devuelve `Container()` (area vacia/negra) si el
      // controller no esta inicializado: el mostrado debe estarlo y ser el
      // vigente de la fuente.
      final preview =
          tester.widget<CameraPreview>(find.byType(CameraPreview));
      expect(preview.controller.value.isInitialized, isTrue);
      expect(source.isControllerCurrent(preview.controller), isTrue);
      expect(find.byType(KycPreviewPlaceholder), findsNothing);
      expect(tester.takeException(), isNull);
    });

    testWidgets(
        'cambio de lente durante una rafaga no cierra el controller en uso '
        '(regresion F-T24)', (tester) async {
      // El cambio de lente en medio de la rafaga NO ocurre en el flujo real,
      // pero la seguridad F-T24 debe conservarse: la apertura de la otra lente
      // espera a que drene la captura en vez de cerrar el controller en uso.
      final gate = Completer<void>();
      late final _TraceSource source;
      var switchRequested = false;
      source = _TraceSource(
        burstFrames: 2,
        burstDelay: (_) async {
          if (!switchRequested) {
            switchRequested = true;
            unawaited(source.previewControllerForTask('document'));
          }
          await gate.future;
        },
      );
      addTearDown(source.dispose);

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: KycCameraPreview(task: 'front', source: source),
          ),
        ),
      );
      await tester.pumpAndSettle();

      final front = source.previewController! as _FakeCameraController;
      final capture = source.captureFramesForTask('front');
      // Deja que la rafaga tome el primer frame y quede esperando el gate.
      await tester.pump();
      expect(front.disposed, isFalse,
          reason: 'no se cierra un controller con una captura en curso');

      gate.complete();
      final frames = await capture;

      expect(frames.length, 2);
      expect(source.anyUsedAfterDispose, isFalse);
      expect(tester.takeException(), isNull);
    });

    testWidgets('fallback mock intacto: placeholder sin CameraPreview',
        (tester) async {
      final controller = KycFlowController(
        service: const _FakeKycService(),
        frameSource: MockKycFrameSource(),
      );
      addTearDown(controller.dispose);
      await controller.loadChallenge();

      await tester.pumpWidget(
        MaterialApp(
          home: KycTaskPage(controller: controller, prepDuration: Duration.zero),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byType(KycPreviewPlaceholder), findsOneWidget);
      expect(find.byType(KycCameraPreview), findsNothing);
      expect(find.byType(CameraPreview), findsNothing);
      final button = tester.widget<FilledButton>(
        find.byKey(const Key('kycCaptureButton')),
      );
      expect(button.onPressed, isNotNull);
    });
  });

  group('F-T28: sin PII/persistencia en el preview (CA-05)', () {
    testWidgets(
        'el ciclo documento -> liveness con dobles no loguea frames/base64/PII',
        (tester) async {
      // Verificacion robusta de la SALIDA real de depuracion (no de strings
      // del fuente): override de `debugPrint` + captura de `print` via Zone,
      // de modo que cubre logging directo o indirecto (ayudantes, wrappers).
      final logs = <String>[];
      final originalDebugPrint = debugPrint;
      debugPrint = (String? message, {int? wrapWidth}) {
        if (message != null) logs.add(message);
      };
      try {
        await runZoned(
          () async {
            // Prueba de humo del arnes: la captura de `debugPrint` no es un
            // no-op; el sentinel se descarta antes de inspeccionar el ciclo.
            debugPrint('KYC-CA05 sentinel');
            expect(logs, contains('KYC-CA05 sentinel'));
            logs.clear();

            final source = _TraceSource();
            addTearDown(source.dispose);
            final controller = KycFlowController(
              service: const _FakeKycService(),
              frameSource: source,
              documentValidator: (image) async =>
                  const KycDocumentValidation(isValid: true),
            );
            addTearDown(controller.dispose);
            await controller.loadChallenge();

            final router = GoRouter(
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

            await tester.pumpWidget(MaterialApp.router(routerConfig: router));
            await tester.pumpAndSettle();

            // Documento (trasera) con preview en vivo.
            expect(find.byType(CameraPreview), findsOneWidget);

            await tester.ensureVisible(
              find.byKey(const Key('captureDocumentButton')),
            );
            await tester.tap(find.byKey(const Key('captureDocumentButton')));
            await tester.pumpAndSettle();

            // El ciclo se completo (documento -> liveness, con captura real de
            // bytes y cambio de generacion/controller): aun asi, sin payload.
            expect(source.generation, greaterThanOrEqualTo(2));
            expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
          },
          zoneSpecification: ZoneSpecification(
            print: (self, parent, zone, line) => logs.add(line),
          ),
        );

        // Ninguna linea capturada debe exponer el frame (JPEG/base64) ni un
        // data URI de imagen. `data:image` cubre el prefijo tipico; el patron
        // detecta cadenas largas con alfabeto base64 (payload de bytes).
        final payloadPattern = RegExp(r'[A-Za-z0-9+/]{64,}={0,2}');
        for (final line in logs) {
          final lower = line.toLowerCase();
          expect(lower.contains('data:image'), isFalse,
              reason: 'no debe loguearse un data URI de imagen: $line');
          expect(lower.contains('base64'), isFalse,
              reason: 'no debe loguearse base64: $line');
          expect(payloadPattern.hasMatch(line), isFalse,
              reason: 'posible payload/frame en log: $line');
        }
      } finally {
        // Restaurar DENTRO del cuerpo: `_verifyInvariants` de flutter_test
        // corre antes de los `addTearDown`, y una variable debug de
        // foundation cambiada haria fallar el test.
        debugPrint = originalDebugPrint;
      }
    });

    test(
        'KycCameraPreview no persiste frames a disco ni los imprime '
        '(red de seguridad estatica)', () {
      final content =
          File('lib/features/kyc/kyc_camera_preview.dart').readAsStringSync();
      expect(content.contains('base64'), isFalse);
      expect(content.contains('writeAsBytes'), isFalse);
      expect(content.contains('File('), isFalse);
      expect(content.contains('debugPrint('), isFalse);
      expect(content.contains('print('), isFalse);
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
