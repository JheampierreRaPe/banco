import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:uuid/uuid.dart';

/// Dio base sin salir a red. El interceptor de captura debe agregarse DESPUES
/// de crear el [ApiClient] (los interceptores `onRequest` de dio corren en
/// orden: Auth -> RequestId -> captura).
Dio _bareDio() => Dio(BaseOptions(baseUrl: 'http://localhost'));

void _captureLast(
  Dio dio,
  void Function(RequestOptions options) onRequest,
) {
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        onRequest(options);
        handler.resolve(
          Response(requestOptions: options, statusCode: 200, data: {'ok': true}),
        );
      },
    ),
  );
}

void main() {
  test('interceptores agregan Authorization y X-Request-Id', () async {
    RequestOptions? captured;
    final dio = _bareDio();

    final session = InMemorySessionRepository(
      initialAccessToken: 'abc123',
    );
    final client = ApiClient.create(
      session: session,
      baseUrl: 'http://localhost/api/v1',
      dioOverride: dio,
    );
    _captureLast(dio, (o) => captured = o);

    await client.get('/ping');

    expect(captured, isNotNull);
    expect(captured!.headers['Authorization'], 'Bearer abc123');
    final requestId = captured!.headers['X-Request-Id']?.toString();
    expect(requestId, isNotNull);
    // Debe ser un uuid valido.
    expect(() => Uuid.parse(requestId!), returnsNormally);
  });

  test('sin sesion no se envia Authorization pero si X-Request-Id', () async {
    RequestOptions? captured;
    final dio = _bareDio();

    final session = InMemorySessionRepository();
    final client = ApiClient.create(
      session: session,
      baseUrl: 'http://localhost/api/v1',
      dioOverride: dio,
    );
    _captureLast(dio, (o) => captured = o);

    await client.get('/ping');

    expect(captured!.headers.containsKey('Authorization'), isFalse);
    expect(captured!.headers['X-Request-Id'], isNotNull);
  });

  test('postWithIdempotency agrega Idempotency-Key uuid', () async {
    RequestOptions? captured;
    final dio = _bareDio();

    final session = InMemorySessionRepository();
    final client = ApiClient.create(
      session: session,
      baseUrl: 'http://localhost/api/v1',
      dioOverride: dio,
    );
    _captureLast(dio, (o) => captured = o);

    await client.postWithIdempotency('/transfers/own', data: {'monto': 100});

    final key = captured!.headers['Idempotency-Key']?.toString();
    expect(key, isNotNull);
    expect(() => Uuid.parse(key!), returnsNormally);
  });

  test('postWithIdempotency reutiliza la clave si se provee', () async {
    RequestOptions? captured;
    final dio = _bareDio();

    final session = InMemorySessionRepository();
    final client = ApiClient.create(
      session: session,
      baseUrl: 'http://localhost/api/v1',
      dioOverride: dio,
    );
    _captureLast(dio, (o) => captured = o);

    await client.postWithIdempotency(
      '/transfers/own',
      data: {'monto': 100},
      idempotencyKey: 'clave-fija-123',
    );

    expect(captured!.headers['Idempotency-Key'], 'clave-fija-123');
  });

  test('error de red se mapea a mensaje en espanol', () {
    final dioError = DioException(
      requestOptions: RequestOptions(path: '/x'),
      type: DioExceptionType.connectionError,
      error: 'sin conexion',
    );
    final e = ApiException.fromDioException(dioError);

    expect(e.code, 'NETWORK_ERROR');
    expect(e.isNetworkError, isTrue);
    expect(e.message, contains('Sin conexion'));
  });

  test('error.code del backend se mapea a mensaje UI', () {
    final dioError = DioException(
      requestOptions: RequestOptions(path: '/transfers/own'),
      response: Response(
        requestOptions: RequestOptions(path: '/transfers/own'),
        statusCode: 422,
        data: {
          'error': {
            'code': 'INSUFFICIENT_FUNDS',
            'message': 'Saldo insuficiente para completar la operacion',
            'request_id': 'req-1',
          },
        },
      ),
      type: DioExceptionType.badResponse,
    );
    final e = ApiException.fromDioException(dioError);

    expect(e.code, 'INSUFFICIENT_FUNDS');
    expect(e.message, contains('Saldo insuficiente'));
    expect(e.requestId, 'req-1');
  });
}
