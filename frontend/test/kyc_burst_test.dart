import 'dart:typed_data';

import 'package:banca_online/features/kyc/camera_frame_source.dart';
import 'package:banca_online/features/kyc/kyc_frame_source.dart';
import 'package:flutter_test/flutter_test.dart';

Uint8List _jpeg(int seed) => Uint8List.fromList([
      0xFF, 0xD8, 0xFF, 0xE0,
      ...List<int>.filled(60, seed & 0xFF),
    ]);

void main() {
  group('captureBurstFrames (seam sin tiempo real, F-T23)', () {
    test('devuelve >1 frame y respeta los intervalos inyectados', () async {
      final delays = <Duration>[];
      var captured = 0;

      final frames = await captureBurstFrames(
        captureOne: () async => _jpeg(++captured),
        frames: 5,
        interval: const Duration(milliseconds: 180),
        delay: (d) async => delays.add(d),
      );

      expect(frames.length, 5);
      // Una espera antes de cada frame posterior al primero.
      expect(delays.length, 4);
      expect(
        delays.every((d) => d == const Duration(milliseconds: 180)),
        isTrue,
      );
    });

    test('valida cada frame (basura -> FormatException)', () async {
      await expectLater(
        captureBurstFrames(
          captureOne: () async => Uint8List.fromList([1, 2, 3]),
          frames: 2,
          delay: (_) async {},
        ),
        throwsA(isA<FormatException>()),
      );
    });

    test('frames < 1 es invalido', () {
      expect(
        () => captureBurstFrames(
          captureOne: () async => _jpeg(1),
          frames: 0,
        ),
        throwsA(isA<ArgumentError>()),
      );
    });
  });

  group('CameraFrameSource: default de rafaga (F-T23)', () {
    test('10-15 frames a 150-200 ms (~2.5 s)', () {
      final source = CameraFrameSource();
      addTearDown(source.dispose);

      expect(source.burstFrames, inInclusiveRange(10, 15));
      expect(
        source.frameInterval.inMilliseconds,
        inInclusiveRange(150, 200),
      );
      final totalMs =
          source.frameInterval.inMilliseconds * (source.burstFrames - 1);
      expect(totalMs, inInclusiveRange(1500, 3000));
    });

    test('la rafaga es inyectable para tests sin camara', () {
      final source = CameraFrameSource(
        burstFrames: 3,
        frameInterval: const Duration(milliseconds: 1),
        burstDelay: (_) async {},
      );
      addTearDown(source.dispose);
      expect(source.burstFrames, 3);
      expect(source.frameInterval, const Duration(milliseconds: 1));
    });
  });

  group('MockKycFrameSource documento', () {
    test('captureDocumentFrame entrega UNA foto en memoria', () async {
      final source = MockKycFrameSource();
      addTearDown(source.dispose);
      final bytes = await source.captureDocumentFrame();
      expect(isSupportedImageBytes(bytes), isTrue);
      expect(bytes.length, greaterThan(8));
    });
  });
}
