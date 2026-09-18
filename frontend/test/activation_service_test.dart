import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/activation/activation_service.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

/// Cliente cuyo `dio` resuelve [json] con 200 y captura el cuerpo enviado.
ApiClient _successClient({
  required InMemorySessionRepository session,
  required Map<String, dynamic> json,
  Map<String, dynamic>? capturedBody,
}) {
  final dio = Dio(BaseOptions(baseUrl: 'http://localhost'));
  final client = ApiClient.create(
    session: session,
    baseUrl: 'http://localhost/api/v1',
    dioOverride: dio,
  );
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
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

/// Cliente cuyo `dio` falla con `{ "error": { "code": ... } }` (docs/05 §4).
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
  test('activate envía user_ref+code y parsea user_id/status sin tocar sesión',
      () async {
    final session = InMemorySessionRepository();
    final captured = <String, dynamic>{};
    final service = HttpActivationService(
      api: _successClient(
        session: session,
        capturedBody: captured,
        json: {
          'data': {'user_id': 'u-1', 'status': 'ACTIVE'},
          'meta': {'request_id': 'r-1'},
        },
      ),
      session: session,
    );

    final result = await service.activate(userRef: 'u-1', code: '123456');

    expect(captured['user_ref'], 'u-1');
    expect(captured['code'], '123456');
    expect(result.userId, 'u-1');
    expect(result.status, 'ACTIVE');
    expect(result.isActive, isTrue);
    // El backend E1-T10 no devuelve tokens: no se guarda sesión.
    expect(session.isAuthenticated, isFalse);
  });

  test('activate guarda sesión si la respuesta trae tokens (defensivo)',
      () async {
    final session = InMemorySessionRepository();
    final service = HttpActivationService(
      api: _successClient(
        session: session,
        json: {
          'data': {
            'user_id': 'u-1',
            'status': 'ACTIVE',
            'access_token': 'access-abc',
            'refresh_token': 'refresh-abc',
          },
          'meta': {'request_id': 'r-1'},
        },
      ),
      session: session,
    );

    await service.activate(userRef: 'u-1', code: '123456');

    expect(session.isAuthenticated, isTrue);
    expect(session.currentAccessToken, 'access-abc');
    expect(await session.readRefreshToken(), 'refresh-abc');
  });

  test('activate propaga INVALID_OTP como ApiException', () async {
    final service = HttpActivationService(
      api: _failingClient(code: 'INVALID_OTP', status: 400),
      session: InMemorySessionRepository(),
    );

    expect(
      () => service.activate(userRef: 'u-1', code: '000000'),
      throwsA(
        isA<ApiException>().having((e) => e.code, 'code', 'INVALID_OTP'),
      ),
    );
  });

  test('resend envía user_ref y parsea resend_count/expires_in', () async {
    final session = InMemorySessionRepository();
    final captured = <String, dynamic>{};
    final service = HttpActivationService(
      api: _successClient(
        session: session,
        capturedBody: captured,
        json: {
          'data': {'user_id': 'u-1', 'resend_count': 1, 'expires_in': 600},
          'meta': {'request_id': 'r-2'},
        },
      ),
      session: session,
    );

    final result = await service.resend(userRef: 'u-1');

    expect(captured['user_ref'], 'u-1');
    expect(captured.containsKey('channel'), isFalse);
    expect(result.resendCount, 1);
    expect(result.expiresInSeconds, 600);
  });

  test('resend incluye channel cuando se provee', () async {
    final session = InMemorySessionRepository();
    final captured = <String, dynamic>{};
    final service = HttpActivationService(
      api: _successClient(
        session: session,
        capturedBody: captured,
        json: {
          'data': {'user_id': 'u-1', 'resend_count': 2, 'expires_in': 600},
          'meta': {'request_id': 'r-3'},
        },
      ),
      session: session,
    );

    await service.resend(userRef: 'u-1', channel: 'sms');

    expect(captured['channel'], 'sms');
  });

  test('resend propaga RESEND_LIMIT como ApiException', () async {
    final service = HttpActivationService(
      api: _failingClient(code: 'RESEND_LIMIT', status: 429),
      session: InMemorySessionRepository(),
    );

    expect(
      () => service.resend(userRef: 'u-1'),
      throwsA(
        isA<ApiException>().having((e) => e.code, 'code', 'RESEND_LIMIT'),
      ),
    );
  });

  test('usa rutas relativas (baseUrl ya incluye /api/v1, sin duplicar)',
      () async {
    final session = InMemorySessionRepository();
    final paths = <String>[];
    final dio = Dio(BaseOptions(baseUrl: 'http://localhost'));
    final client = ApiClient.create(
      session: session,
      baseUrl: 'http://localhost/api/v1',
      dioOverride: dio,
    );
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          paths.add(options.path);
          if (options.path == HttpActivationService.activatePath) {
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'data': {'user_id': 'u-1', 'status': 'ACTIVE'},
                  'meta': {'request_id': 'r-1'},
                },
              ),
            );
          } else {
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'data': {
                    'user_id': 'u-1',
                    'resend_count': 1,
                    'expires_in': 600
                  },
                  'meta': {'request_id': 'r-2'},
                },
              ),
            );
          }
        },
      ),
    );
    final service = HttpActivationService(api: client, session: session);

    await service.activate(userRef: 'u-1', code: '123456');
    await service.resend(userRef: 'u-1');

    expect(HttpActivationService.activatePath, '/auth/activate');
    expect(HttpActivationService.resendPath, '/auth/otp/resend');
    expect(paths, ['/auth/activate', '/auth/otp/resend']);
  });
}
