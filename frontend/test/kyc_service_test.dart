import 'dart:convert';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';

/// Dio base sin salir a red (mismo patron que `api_client_test.dart`).
Dio _bareDio() => Dio(BaseOptions(baseUrl: 'http://localhost'));

/// Responde [body] a cualquier request y captura sus opciones.
void _respond(
  Dio dio,
  Object? Function(RequestOptions options) body, {
  void Function(RequestOptions options)? onRequest,
}) {
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        onRequest?.call(options);
        handler.resolve(
          Response(requestOptions: options, statusCode: 200, data: body(options)),
        );
      },
    ),
  );
}

HttpKycService _service(Dio dio) => HttpKycService(
      ApiClient.create(
        session: InMemorySessionRepository(),
        baseUrl: 'http://localhost/api/v1',
        dioOverride: dio,
      ),
    );

void main() {
  test('challenge parsea token, steps y expires_in del envelope', () async {
    final dio = _bareDio();
    final service = _service(dio);
    _respond(
      dio,
      (_) => {
        'data': {
          'token': 'tok-abc',
          'steps': ['front', 'blink', 'turn_left'],
          'expires_in': 300,
        },
        'meta': {'request_id': 'req-1'},
      },
    );

    final challenge = await service.challenge();

    expect(challenge.token, 'tok-abc');
    expect(challenge.steps, ['front', 'blink', 'turn_left']);
    expect(challenge.expiresIn, 300);
  });

  test('submit envia document.number + applicant y parsea user_id', () async {
    final dio = _bareDio();
    final service = _service(dio);
    RequestOptions? captured;
    _respond(
      dio,
      (_) => {
        'data': {
          'overall_result': true,
          'detail_code': 'OK',
          'distance': 0.12,
          'user_id': 'user-123',
          'status': 'ACTIVE',
          'account_id': 'acc-9',
        },
        'meta': {'request_id': 'req-2'},
      },
      onRequest: (o) => captured = o,
    );

    final result = await service.submit(
      challengeToken: 'tok-abc',
      documentType: 'DNI',
      documentNumber: '12345678',
      applicant: const KycApplicant(
        firstName: 'Ana',
        lastName: 'Perez',
        email: 'ana@example.com',
        phone: '999888777',
      ),
      framesByTask: {
        'front': [Uint8List.fromList([1, 2, 3])],
        'blink': [Uint8List.fromList([4, 5, 6])],
      },
    );

    expect(result.overallResult, isTrue);
    expect(result.detailCode, 'OK');
    expect(result.userId, 'user-123');
    expect(result.status, 'ACTIVE');
    expect(result.accountId, 'acc-9');
    final data = captured!.data as Map<String, dynamic>;
    expect(data['challenge_token'], 'tok-abc');
    final document = data['document'] as Map<String, dynamic>;
    expect(document['type'], 'DNI');
    expect(document['number'], '12345678');
    expect(document['image_b64'], isNotEmpty);
    final applicant = data['applicant'] as Map<String, dynamic>;
    expect(applicant['first_name'], 'Ana');
    expect(applicant['last_name'], 'Perez');
    expect(applicant['email'], 'ana@example.com');
    expect(applicant['phone'], '999888777');
    final segments = data['segments'] as List;
    expect(segments.length, 2);
    expect((segments.first as Map)['task'], 'front');
  });

  test('submit mapea Pasaporte a PASSPORT y DNI/CE tal cual', () async {
    final dio = _bareDio();
    final service = _service(dio);
    final sentTypes = <String>[];
    _respond(
      dio,
      (_) => {
        'data': {'overall_result': true, 'detail_code': 'OK'},
      },
      onRequest: (o) {
        final doc = (o.data as Map)['document'] as Map;
        sentTypes.add(doc['type'] as String);
      },
    );

    Future<void> send(String type) => service.submit(
          challengeToken: 'tok-abc',
          documentType: type,
          documentNumber: '12345678',
          applicant: const KycApplicant(
            firstName: 'Ana',
            lastName: 'Perez',
            email: 'ana@example.com',
          ),
          framesByTask: {
            'front': [Uint8List.fromList([1, 2, 3])],
          },
        );

    await send('Pasaporte');
    await send('DNI');
    await send('CE');
    expect(sentTypes, ['PASSPORT', 'DNI', 'CE']);
  });

  test('submit sin user_id no rompe el parseo (fallback legacy)', () async {
    final dio = _bareDio();
    final service = _service(dio);
    _respond(
      dio,
      (_) => {
        'data': {'overall_result': true, 'detail_code': 'OK'},
      },
    );

    final result = await service.submit(
      challengeToken: 'tok-abc',
      documentType: 'DNI',
      documentNumber: '12345678',
      applicant: const KycApplicant(
        firstName: 'Ana',
        lastName: 'Perez',
        email: 'ana@example.com',
      ),
      framesByTask: {
        'front': [Uint8List.fromList([1, 2, 3])],
      },
    );

    expect(result.overallResult, isTrue);
    expect(result.userId, isNull);
    expect(result.status, isNull);
    expect(result.accountId, isNull);
  });

  test('evaluate envia frames_b64 y parsea passed/reason', () async {
    final dio = _bareDio();
    final service = _service(dio);
    RequestOptions? captured;
    _respond(
      dio,
      (_) => {
        'data': {
          'step': 'blink',
          'passed': false,
          'reason': 'NO_BLINK',
          'frames_analyzed': 9,
          'details': {'movement': 0.01},
        },
      },
      onRequest: (o) => captured = o,
    );

    final evaluation = await service.evaluate(
      challengeToken: 'tok-abc',
      step: 'blink',
      frames: [
        Uint8List.fromList([1, 2, 3]),
        Uint8List.fromList([4, 5, 6]),
        Uint8List.fromList([7, 8, 9]),
      ],
    );

    expect(evaluation.step, 'blink');
    expect(evaluation.passed, isFalse);
    expect(evaluation.reason, 'NO_BLINK');
    expect(evaluation.framesAnalyzed, 9);
    final data = captured!.data as Map<String, dynamic>;
    expect(data['challenge_token'], 'tok-abc');
    expect(data['step'], 'blink');
    expect((data['frames_b64'] as List).length, 3);
    expect(captured!.path, HttpKycService.evaluatePath);
  });

  test('evaluate sin frames no llama al endpoint', () {
    final dio = _bareDio();
    final service = _service(dio);
    expect(
      () => service.evaluate(
        challengeToken: 'tok-abc',
        step: 'front',
        frames: const [],
      ),
      throwsA(isA<ArgumentError>()),
    );
  });

  test('submit envia TODOS los frames por segmento en frames_b64', () async {
    final dio = _bareDio();
    final service = _service(dio);
    RequestOptions? captured;
    _respond(
      dio,
      (_) => {
        'data': {'overall_result': true, 'detail_code': 'OK'},
      },
      onRequest: (o) => captured = o,
    );

    final front = [
      Uint8List.fromList([1, 2, 3]),
      Uint8List.fromList([4, 5, 6]),
      Uint8List.fromList([7, 8, 9]),
    ];
    await service.submit(
      challengeToken: 'tok-abc',
      documentType: 'DNI',
      documentNumber: '12345678',
      applicant: const KycApplicant(
        firstName: 'Ana',
        lastName: 'Perez',
        email: 'ana@example.com',
      ),
      framesByTask: {
        'front': front,
        'blink': [Uint8List.fromList([10, 11, 12])],
      },
    );

    final data = captured!.data as Map<String, dynamic>;
    final segments = data['segments'] as List;
    final first = segments.first as Map;
    expect((first['frames_b64'] as List).length, 3);
    expect(
      (first['frames_b64'] as List).cast<String>(),
      front.map(base64Encode).toList(),
    );
    // `frames_b64` es la fuente unica: no se duplica el primer frame en
    // `image_b64`.
    expect(first.containsKey('image_b64'), isFalse);
  });

  test('submitWithDocument usa la foto real en document.image_b64', () async {
    final dio = _bareDio();
    final service = _service(dio);
    RequestOptions? captured;
    _respond(
      dio,
      (_) => {
        'data': {
          'overall_result': false,
          'detail_code': 'LIVENESS_FAILED',
          'failed_step': 'blink',
          'overall_reason': 'NO_BLINK',
          'steps_verified': ['front'],
          'steps_total': ['front', 'blink'],
          'step_results': {
            'front': {'passed': true},
            'blink': {'passed': false, 'reason': 'NO_BLINK'},
          },
        },
      },
      onRequest: (o) => captured = o,
    );

    final documentBytes = Uint8List.fromList([0xFF, 0xD8, 0xFF, 0xEE, 1, 2, 3]);
    final result = await service.submitWithDocument(
      challengeToken: 'tok-abc',
      documentType: 'DNI',
      documentNumber: '12345678',
      applicant: const KycApplicant(
        firstName: 'Ana',
        lastName: 'Perez',
        email: 'ana@example.com',
      ),
      framesByTask: {
        'front': [Uint8List.fromList([1, 2, 3])],
      },
      documentImage: documentBytes,
    );

    final data = captured!.data as Map<String, dynamic>;
    final document = data['document'] as Map<String, dynamic>;
    expect(document['image_b64'], base64Encode(documentBytes));
    // La UI dispone del paso y motivo exactos (E1-T29).
    expect(result.failedStep, 'blink');
    expect(result.failureReason, 'NO_BLINK');
    expect(result.stepsVerified, ['front']);
    expect(result.stepsTotal, ['front', 'blink']);
    expect(result.stepResults['blink']?.passed, isFalse);
    expect(result.stepResults['blink']?.reason, 'NO_BLINK');
  });

  test('error del backend se propaga como ApiException', () {
    final dio = _bareDio();
    final service = _service(dio);
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          handler.reject(
            DioException(
              requestOptions: options,
              type: DioExceptionType.badResponse,
              response: Response(
                requestOptions: options,
                statusCode: 503,
                data: {
                  'error': {
                    'code': 'KYC_UNAVAILABLE',
                    'message': 'fuera de servicio',
                  },
                },
              ),
            ),
          );
        },
      ),
    );

    expect(service.challenge(), throwsA(isA<ApiException>()));
  });

  test('validateDocument envia image_b64 al endpoint y parsea issues',
      () async {
    final dio = _bareDio();
    final service = _service(dio);
    RequestOptions? captured;
    _respond(
      dio,
      (_) => {
        'data': {
          'is_valid': false,
          'issues': ['BLURRY', 'GLARE'],
          'checks': {'focus': 0.1},
        },
        'meta': {'request_id': 'req-3'},
      },
      onRequest: (o) => captured = o,
    );

    final image = Uint8List.fromList([0xFF, 0xD8, 0xFF, 1, 2, 3]);
    final result = await service.validateDocument(image: image);

    expect(result.isValid, isFalse);
    expect(result.issues, ['BLURRY', 'GLARE']);
    expect(captured!.path, HttpKycService.documentValidatePath);
    final data = captured!.data as Map<String, dynamic>;
    expect(data['image_b64'], base64Encode(image));
  });

  test('validateDocument valido con issues vacios/ausentes no rompe', () async {
    final dio = _bareDio();
    final service = _service(dio);
    _respond(dio, (_) => {
          'data': {'is_valid': true},
        });

    final result = await service.validateDocument(
      image: Uint8List.fromList([1, 2, 3]),
    );

    expect(result.isValid, isTrue);
    expect(result.issues, isEmpty);
    expect(result.checks, isEmpty);
  });

  test('validateDocument no registra el base64 ni la imagen en logs', () async {
    final dio = _bareDio();
    final service = _service(dio);
    _respond(dio, (_) => {
          'data': {'is_valid': true, 'issues': const <String>[]},
        });

    final logs = <String>[];
    final previous = debugPrint;
    debugPrint = (String? message, {int? wrapWidth}) {
      if (message != null) logs.add(message);
    };
    addTearDown(() => debugPrint = previous);

    final image = Uint8List.fromList([0xFF, 0xD8, 0xFF, 9, 8, 7]);
    await service.validateDocument(image: image);

    expect(logs.join('\n'), isNot(contains(base64Encode(image))));
  });
}
