import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/pin_setup/pin_setup_service.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

ApiClient _successClient({
  required Map<String, dynamic> json,
  Map<String, dynamic>? capturedBody,
  List<String>? capturedPaths,
}) {
  final dio = Dio(BaseOptions(baseUrl: 'http://localhost'));
  final client = ApiClient.create(
    session: InMemorySessionRepository(),
    baseUrl: 'http://localhost/api/v1',
    dioOverride: dio,
  );
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        capturedPaths?.add(options.path);
        if (capturedBody != null && options.data is Map) {
          capturedBody.addAll(Map<String, dynamic>.from(options.data as Map));
        }
        handler.resolve(
          Response(requestOptions: options, statusCode: 200, data: json),
        );
      },
    ),
  );
  return client;
}

ApiClient _failingClient({required String code, required int status}) {
  final dio = Dio(BaseOptions(baseUrl: 'http://localhost'));
  final client = ApiClient.create(
    session: InMemorySessionRepository(),
    baseUrl: 'http://localhost/api/v1',
    dioOverride: dio,
  );
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        handler.reject(
          DioException(
            requestOptions: options,
            type: DioExceptionType.badResponse,
            response: Response(
              requestOptions: options,
              statusCode: status,
              data: {
                'error': {
                  'code': code,
                  'message': 'mensaje del servidor',
                  'request_id': 'req-1',
                },
              },
            ),
          ),
        );
      },
    ),
  );
  return client;
}

void main() {
  test('setup envía user_ref+code+pin y parsea user_id/status', () async {
    final captured = <String, dynamic>{};
    final paths = <String>[];
    final service = HttpPinSetupService(
      api: _successClient(
        capturedBody: captured,
        capturedPaths: paths,
        json: {
          'data': {'user_id': 'u-1', 'status': 'PIN_SET'},
          'meta': {'request_id': 'r-1'},
        },
      ),
    );

    final result = await service.setup(
      userRef: 'u-1',
      code: '123456',
      pin: '1234',
    );

    expect(captured['user_ref'], 'u-1');
    expect(captured['code'], '123456');
    expect(captured['pin'], '1234');
    expect(result.userId, 'u-1');
    expect(result.status, 'PIN_SET');
  });

  test('usa ruta relativa /auth/pin/setup (baseUrl ya incluye /api/v1)',
      () async {
    final paths = <String>[];
    final service = HttpPinSetupService(
      api: _successClient(
        capturedPaths: paths,
        json: {
          'data': {'user_id': 'u-1', 'status': 'PIN_SET'},
          'meta': {'request_id': 'r-1'},
        },
      ),
    );

    await service.setup(userRef: 'u-1', code: '123456', pin: '1234');

    expect(HttpPinSetupService.setupPath, '/auth/pin/setup');
    expect(paths, ['/auth/pin/setup']);
  });

  test('setup propaga INVALID_SETUP_CODE como ApiException', () async {
    final service = HttpPinSetupService(
      api: _failingClient(code: 'INVALID_SETUP_CODE', status: 401),
    );

    expect(
      () => service.setup(userRef: 'u-1', code: '000000', pin: '1234'),
      throwsA(
        isA<ApiException>()
            .having((e) => e.code, 'code', 'INVALID_SETUP_CODE'),
      ),
    );
  });

  test('setup propaga PIN_ALREADY_SET como ApiException', () async {
    final service = HttpPinSetupService(
      api: _failingClient(code: 'PIN_ALREADY_SET', status: 409),
    );

    expect(
      () => service.setup(userRef: 'u-1', code: '123456', pin: '1234'),
      throwsA(
        isA<ApiException>()
            .having((e) => e.code, 'code', 'PIN_ALREADY_SET'),
      ),
    );
  });
}
