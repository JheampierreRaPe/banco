import 'dart:typed_data';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:dio/dio.dart';
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

  test('submit envia la forma del proxy y parsea overall_result', () async {
    final dio = _bareDio();
    final service = _service(dio);
    RequestOptions? captured;
    _respond(
      dio,
      (_) => {
        'data': {'overall_result': true, 'detail_code': 'OK'},
        'meta': {'request_id': 'req-2'},
      },
      onRequest: (o) => captured = o,
    );

    final result = await service.submit(
      challengeToken: 'tok-abc',
      documentType: 'DNI',
      documentNumber: '12345678',
      framesByTask: {
        'front': [Uint8List.fromList([1, 2, 3])],
        'blink': [Uint8List.fromList([4, 5, 6])],
      },
    );

    expect(result.overallResult, isTrue);
    expect(result.detailCode, 'OK');
    final data = captured!.data as Map<String, dynamic>;
    expect(data['challenge_token'], 'tok-abc');
    final document = data['document'] as Map<String, dynamic>;
    expect(document['type'], 'DNI');
    expect(document['image_b64'], isNotEmpty);
    final segments = data['segments'] as List;
    expect(segments.length, 2);
    expect((segments.first as Map)['task'], 'front');
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
}
