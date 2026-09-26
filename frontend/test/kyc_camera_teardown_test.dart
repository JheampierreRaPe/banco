import 'dart:async';
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

/// Doble de [CameraController] SIN plataforma (F-T33): igual que el de
/// `kyc_camera_lifecycle_test.dart` (registra `initialize`/`dispose` y falla
/// si se le usa tras `dispose`), más una compuerta opcional ([pictureGate])
/// para simular una captura colgada (H-03).
class _FakeCameraController extends CameraController {
  _FakeCameraController({
    required CameraLensDirection lens,
    this.tag = '',
    this.timeline,
    this.pictureGate,
  }) : super(
          lens == CameraLensDirection.front ? _front : _back,
          ResolutionPreset.low,
        );

  final String tag;
  final List<String>? timeline;

  /// Si no es `null`, `takePicture` queda esperando esta compuerta (captura
  /// colgada para H-03). La comprobación de `disposed` se hace AL ENTRAR (como
  /// el plugin real: la llamada era válida cuando se hizo); reanudar tras la
  /// compuerta no toca el controller, así que un cierre forzado en medio no
  /// genera "used after dispose".
  final Completer<void>? pictureGate;

  bool disposed = false;
  bool usedAfterDispose = false;
  int pictures = 0;

  @override
  Future<void> initialize() async {
    timeline?.add('initialize:$tag');
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
    final gate = pictureGate;
    if (gate != null) await gate.future;
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
    timeline?.add('dispose:$tag');
    disposed = true;
    await super.dispose();
  }
}

CameraFrameSource _source({
  int burstFrames = 2,
  List<_FakeCameraController>? created,
  List<String>? timeline,
  Completer<void>? pictureGate,
  Duration? disposeTimeout,
  Duration? captureTimeout,
}) {
  var sequence = 0;
  final source = CameraFrameSource(
    burstFrames: burstFrames,
    frameInterval: Duration.zero,
    burstDelay: (_) async {},
    camerasLoader: () async => const <CameraDescription>[_front, _back],
    controllerFactory: (description, resolution) {
      final controller = _FakeCameraController(
        lens: description.lensDirection,
        tag: '${description.name}#${sequence++}',
        timeline: timeline,
        pictureGate: pictureGate,
      );
      created?.add(controller);
      return controller;
    },
  );
  if (disposeTimeout != null) source.disposeTimeout = disposeTimeout;
  if (captureTimeout != null) source.captureTimeout = captureTimeout;
  return source;
}

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

const _applicant = KycApplicant(
  firstName: 'Ana',
  lastName: 'Perez',
  email: 'ana@example.com',
);

void main() {
  group('F-T33: teardown de la sesion de camara', () {
    test('CA-01: el submit exitoso cierra la sesion sin perder el resultado',
        () async {
      final created = <_FakeCameraController>[];
      final source = _source(created: created);
      addTearDown(source.dispose);
      final controller = KycFlowController(
        service: _FakeKycService(),
        frameSource: source,
      );
      addTearDown(controller.dispose);
      await controller.loadChallenge();
      controller.setApplicant(_applicant);

      // Documento (trasera) + las dos tareas (frontal): hay sesión viva.
      await controller.captureDocument();
      await controller.captureAndResolveCurrentTask();
      await controller.captureAndResolveCurrentTask();
      expect(controller.readyToSubmit, isTrue);
      expect(created, isNotEmpty);

      await controller.submit();

      // El resultado sigue disponible para `/kyc/result`...
      expect(controller.result, isNotNull);
      expect(controller.result!.overallResult, isTrue);
      // ...pero ningún controller quedó vivo.
      await pumpEventQueue();
      expect(created.every((c) => c.disposed), isTrue,
          reason: 'el submit debe cerrar la sesion de camara');
      expect(created.any((c) => c.usedAfterDispose), isFalse);
    });

    testWidgets('CA-02: salir de /kyc/task libera la sesion', (tester) async {
      final created = <_FakeCameraController>[];
      final source = _source(created: created);
      addTearDown(source.dispose);
      final controller = KycFlowController(
        service: _FakeKycService(),
        frameSource: source,
      );
      addTearDown(controller.dispose);
      await controller.loadChallenge();

      final router = GoRouter(
        initialLocation: '/',
        routes: [
          GoRoute(
            path: '/',
            builder: (context, state) => Scaffold(
              body: TextButton(
                key: const Key('goTask'),
                onPressed: () => context.push('/kyc/task'),
                child: const Text('ir'),
              ),
            ),
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
      addTearDown(router.dispose);

      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.tap(find.byKey(const Key('goTask')));
      await tester.pumpAndSettle();
      expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
      expect(created, isNotEmpty,
          reason: 'el preview debe haber abierto la sesion frontal');
      expect(tester.takeException(), isNull);

      router.pop();
      await tester.pumpAndSettle();

      expect(created.every((c) => c.disposed), isTrue,
          reason: 'salir de /kyc/task debe cerrar la sesion');
      expect(created.any((c) => c.usedAfterDispose), isFalse);
      expect(tester.takeException(), isNull);
    });

    testWidgets('CA-02: salir de /kyc/document libera la sesion',
        (tester) async {
      final created = <_FakeCameraController>[];
      final source = _source(created: created);
      addTearDown(source.dispose);
      final controller = KycFlowController(
        service: _FakeKycService(),
        frameSource: source,
      );
      addTearDown(controller.dispose);
      await controller.loadChallenge();

      final router = GoRouter(
        initialLocation: '/',
        routes: [
          GoRoute(
            path: '/',
            builder: (context, state) => Scaffold(
              body: TextButton(
                key: const Key('goDocument'),
                onPressed: () => context.push('/kyc/document'),
                child: const Text('ir'),
              ),
            ),
          ),
          GoRoute(
            path: '/kyc/document',
            builder: (context, state) =>
                KycDocumentPage(controller: controller),
          ),
        ],
      );
      addTearDown(router.dispose);

      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.tap(find.byKey(const Key('goDocument')));
      await tester.pumpAndSettle();
      expect(find.textContaining('Escanea tu DNI'), findsOneWidget);
      expect(created, isNotEmpty,
          reason: 'el preview debe haber abierto la sesion trasera');
      expect(tester.takeException(), isNull);

      router.pop();
      await tester.pumpAndSettle();

      expect(created.every((c) => c.disposed), isTrue,
          reason: 'salir de /kyc/document debe cerrar la sesion');
      expect(created.any((c) => c.usedAfterDispose), isFalse);
      expect(tester.takeException(), isNull);
    });

    test('CA-03: reset() cierra la sesion previa y la siguiente abre una nueva',
        () async {
      final created = <_FakeCameraController>[];
      final source = _source(created: created);
      addTearDown(source.dispose);
      final controller = KycFlowController(
        service: _FakeKycService(),
        frameSource: source,
      );
      addTearDown(controller.dispose);
      await controller.loadChallenge();

      await source.previewControllerForTask('front');
      expect(created.length, 1);
      final previous = List<_FakeCameraController>.of(created);

      // Reintento: `reset()` + volver a `/kyc`.
      controller.reset();
      await pumpEventQueue();

      expect(previous.every((c) => c.disposed), isTrue,
          reason: 'el reintento no debe dejar la sesion previa viva');

      // La sesión nueva abre sin "used after dispose".
      await controller.loadChallenge();
      await controller.captureAndResolveCurrentTask();
      await pumpEventQueue();

      expect(created.length, greaterThan(previous.length),
          reason: 'debe abrirse una sesion nueva, no reutilizar la cerrada');
      expect(created.any((c) => c.usedAfterDispose), isFalse);
      expect(source.isInitialized, isTrue);
    });

    test(
        'H-03: closeSession no queda diferido por una captura colgada '
        'y no toca un controller cerrado', () async {
      final gate = Completer<void>();
      final created = <_FakeCameraController>[];
      final source = _source(
        created: created,
        pictureGate: gate,
        disposeTimeout: const Duration(milliseconds: 100),
      );
      addTearDown(source.dispose);

      // Ráfaga en curso cuya primera foto queda colgada en `takePicture`.
      final capture = source.captureFramesForTask('front');
      await Future<void>.delayed(const Duration(milliseconds: 50));
      expect(created.length, 1);

      // El teardown completa acotado (falla si cuelga más de 5 s) aunque la
      // captura siga en curso.
      await source.closeSession().timeout(const Duration(seconds: 5));
      expect(created.single.disposed, isTrue);

      // La ráfaga en curso no cuelga: al reanudarse, el segundo frame detecta
      // la sesión cerrada y falla con error tipado (sin usar tras dispose).
      gate.complete();
      await expectLater(
        capture,
        throwsA(isA<KycCameraUnavailable>()),
      );
      expect(created.single.usedAfterDispose, isFalse);
    });

    test('H-03: takePicture colgado falla con error tipado (timeout)',
        () async {
      final created = <_FakeCameraController>[];
      final source = _source(
        burstFrames: 1,
        created: created,
        pictureGate: Completer<void>(),
        captureTimeout: const Duration(milliseconds: 150),
      );
      addTearDown(source.dispose);

      // Regresión BAJO (F-T33): el timeout debe exponer el mensaje claro de
      // timeout ("tardó demasiado"), no el genérico "Falló la captura de
      // foto". Sin el `on KycCameraUnavailable { rethrow; }` el `catch (e)`
      // genérico re-envuelve y este assert falla.
      try {
        await source.captureFramesForTask('front');
        fail('debía lanzar KycCameraUnavailable por timeout de captura');
      } on KycCameraUnavailable catch (e) {
        expect(e.detail, contains('tardó demasiado'),
            reason: 'el mensaje de timeout debe llegar a la UI');
        expect(e.detail, isNot(contains('Falló la captura de foto')),
            reason: 'no debe re-envolverse con el mensaje genérico');
      }
      expect(created.single.usedAfterDispose, isFalse);
    });

    test(
        'regresion F-T32: tras el teardown el cambio de lente sigue siendo '
        'close-before-open (una sola sesion viva)', () async {
      final timeline = <String>[];
      final source = _source(timeline: timeline);
      addTearDown(source.dispose);

      await source.previewControllerForTask('document');
      await source.closeSession();
      await source.previewControllerForTask('front');

      expect(
        timeline,
        <String>[
          'initialize:fake-back#0',
          'dispose:fake-back#0',
          'initialize:fake-front#1',
        ],
      );
    });
  });
}
