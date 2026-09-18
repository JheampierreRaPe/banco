import 'dart:convert';
import 'dart:typed_data';

import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_frame_capture.dart';
import 'package:banca_online/features/kyc/kyc_frame_source.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:flutter_test/flutter_test.dart';

/// Fuentes falsas para los caminos de error de camara (sin plugin `camera`,
/// aptas para CI).
class _UnavailableSource implements KycFrameSource {
  @override
  Future<List<Uint8List>> captureFramesForTask(String task) =>
      throw KycCameraUnavailable('test: sin cámara');

  @override
  Future<void> dispose() async {}
}

class _DeniedSource implements KycFrameSource {
  @override
  Future<List<Uint8List>> captureFramesForTask(String task) =>
      throw KycCameraPermissionDenied('test: permiso denegado, reintenta');

  @override
  Future<void> dispose() async {}
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
    required Map<String, List<Uint8List>> framesByTask,
  }) async =>
      const KycSubmitResult(overallResult: true, detailCode: 'OK');
}

Uint8List _jpeg(int size) => Uint8List.fromList([
      0xFF, 0xD8, 0xFF, 0xE0, // SOI + APP0: magic JPEG
      ...List<int>.filled(size - 4, 0x41),
    ]);

void main() {
  group('KycCameraLens por tarea', () {
    test('liveness usa frontal por defecto', () {
      for (final task in ['front', 'blink', 'smile', 'turn_left', 'nod']) {
        expect(preferredLensForTask(task), KycCameraLens.front,
            reason: task);
        expect(isDocumentTask(task), isFalse, reason: task);
      }
    });

    test('documento usa trasera', () {
      for (final task in [
        'document_front',
        'doc_back',
        'dni',
        'passport',
        'id_card'
      ]) {
        expect(preferredLensForTask(task), KycCameraLens.back, reason: task);
        expect(isDocumentTask(task), isTrue, reason: task);
      }
    });
  });

  group('validacion de bytes (espeja al backend)', () {
    test('mock historico sigue pasando magic PNG', () {
      final frames = generateMockFrames(task: 'front');
      expect(frames.length, kMockFramesPerTask);
      for (final f in frames) {
        expect(isSupportedImageBytes(f), isTrue);
      }
    });

    test('JPEG y PNG validos pasan; basura y cortos no', () {
      expect(isSupportedImageBytes(_jpeg(64)), isTrue);
      expect(
        isSupportedImageBytes(
          Uint8List.fromList([0x89, 0x50, 0x4E, 0x47, 0x00]),
        ),
        isTrue,
      );
      expect(isSupportedImageBytes(Uint8List.fromList([1, 2, 3, 4])),
          isFalse);
      expect(isSupportedImageBytes(Uint8List.fromList([0xFF, 0xD8])),
          isFalse);
      expect(isSupportedImageBytes(Uint8List(0)), isFalse);
    });

    test('validateFrameBytes: vacio, sobredimension y formato', () {
      expect(() => validateFrameBytes(Uint8List(0)),
          throwsA(isA<ArgumentError>()));
      expect(
        () => validateFrameBytes(_jpeg(16), maxBytes: 8),
        throwsA(isA<ArgumentError>()),
      );
      expect(
        () => validateFrameBytes(Uint8List.fromList([1, 2, 3, 4, 5])),
        throwsA(isA<FormatException>()),
      );
      validateFrameBytes(_jpeg(64)); // no lanza
    });

    test('encodeFrameToBase64 hace round-trip', () {
      final bytes = _jpeg(64);
      final b64 = encodeFrameToBase64(bytes);
      expect(b64, base64Encode(bytes));
      expect(base64Decode(b64), bytes);
    });
  });

  group('MockKycFrameSource', () {
    test('entrega los frames mock en memoria', () async {
      final source = MockKycFrameSource();
      addTearDown(source.dispose);
      final frames = await source.captureFramesForTask('front');
      expect(frames.length, kMockFramesPerTask);
      expect(frames.first.length, greaterThan(8));
    });
  });

  group('KycFlowController con fuente inyectada', () {
    test('camara no disponible: fallback al mock y avanza', () async {
      final controller = KycFlowController(
        service: _FakeKycService(),
        frameSource: _UnavailableSource(),
      );
      addTearDown(controller.dispose);

      await controller.loadChallenge();
      await controller.captureAndResolveCurrentTask();

      expect(controller.taskPassed('front'), isTrue);
      expect(controller.framesCountOf('front'), kMockFramesPerTask);
      expect(controller.currentStep, 'blink');
    });

    test('permiso denegado: mensaje + misma tarea para reintentar',
        () async {
      final controller = KycFlowController(
        service: _FakeKycService(),
        frameSource: _DeniedSource(),
      );
      addTearDown(controller.dispose);

      await controller.loadChallenge();
      await controller.captureAndResolveCurrentTask();

      expect(controller.taskPassed('front'), isFalse);
      expect(controller.currentStep, 'front');
      expect(controller.attemptsOf('front'), 0);
      expect(controller.errorMessage, contains('reintenta'));
      expect(controller.framesCountOf('front'), 0);
    });
  });
}
