import 'dart:convert';
import 'dart:typed_data';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/secure_key_value_storage.dart';
import 'package:banca_online/core/session/secure_session_repository.dart';
import 'package:banca_online/core/session/session_service.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

ResponseBody _json(Object body, int status) => ResponseBody.fromString(
      jsonEncode(body),
      status,
      headers: {Headers.contentTypeHeader: [Headers.jsonContentType]},
    );

/// Backend simulado a nivel de transporte (el 401 atraviesa `onError`):
/// `/accounts` responde 401 con el access viejo y 200 con el renovado;
/// `/auth/logout` revoca (200 idempotente).
class FakeBackendAdapter implements HttpClientAdapter {
  FakeBackendAdapter({List<String>? seenAuth}) : seenAuth = seenAuth ?? [];

  final List<String> seenAuth;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    if (options.path == '/accounts') {
      final auth = options.headers['Authorization']?.toString() ?? '';
      seenAuth.add(auth);
      if (auth == 'Bearer expired-access') {
        return _json(
          {
            'error': {'code': 'TOKEN_EXPIRED', 'message': 'Access expirado'}
          },
          401,
        );
      }
      return _json({
        'data': {'ok': true}
      }, 200);
    }
    if (options.path == '/auth/logout') {
      return _json({
        'data': {'revoked': true, 'session_id': 's1'},
        'meta': {'request_id': 'req-1'},
      }, 200);
    }
    return _json({
      'error': {'code': 'NOT_FOUND', 'message': 'no existe'}
    }, 404);
  }

  @override
  void close({bool force = false}) {}
}

/// Servidor de refresh simulado con el shape real de `POST /auth/refresh`
/// (`{"refresh_token"}` → `{data: {access_token, refresh_token, ...}}`;
/// refresh desconocido → 401 `INVALID_REFRESH`).
class FakeRefreshAdapter implements HttpClientAdapter {
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    if (options.path == '/auth/refresh') {
      final presented = (options.data as Map<String, dynamic>)['refresh_token'];
      if (presented == 'valid-refresh') {
        return _json({
          'data': {
            'access_token': 'new-access',
            'refresh_token': 'new-refresh',
            'token_type': 'Bearer',
            'session_id': 's1',
            'expires_in': 900,
          },
          'meta': {'request_id': 'req-1'},
        }, 200);
      }
      return _json({
        'error': {'code': 'INVALID_REFRESH', 'message': 'Refresh inválido'}
      }, 401);
    }
    return _json({
      'error': {'code': 'NOT_FOUND', 'message': 'no existe'}
    }, 404);
  }

  @override
  void close({bool force = false}) {}
}

Dio _dioWith(FakeBackendAdapter backend) =>
    Dio(BaseOptions(baseUrl: 'http://localhost'))..httpClientAdapter = backend;

Dio _refreshDio() => Dio(BaseOptions(baseUrl: 'http://localhost'))
  ..httpClientAdapter = FakeRefreshAdapter();

void main() {
  test('401 por access expirado -> refresh -> reintento exitoso UNA vez',
      () async {
    final session = InMemorySessionRepository();
    await session.saveSession(
      accessToken: 'expired-access',
      refreshToken: 'valid-refresh',
    );

    final backend = FakeBackendAdapter();
    final client = ApiClient.create(
      session: session,
      baseUrl: 'http://localhost',
      dioOverride: _dioWith(backend),
      refreshDioOverride: _refreshDio(),
    );

    final response = await client.get<dynamic>('/accounts');

    expect(response.statusCode, 200);
    // Intento original + UN reintento con el token nuevo.
    expect(backend.seenAuth, ['Bearer expired-access', 'Bearer new-access']);
    expect(session.currentAccessToken, 'new-access');
    expect(await session.readRefreshToken(), 'new-refresh');
  });

  test('refresh inválido -> limpieza total + aviso de expiración', () async {
    final session = InMemorySessionRepository();
    await session.saveSession(
      accessToken: 'expired-access',
      refreshToken: 'revoked-refresh',
    );
    await session.getOrCreateDeviceSecret();

    var expiredCalls = 0;
    final client = ApiClient.create(
      session: session,
      baseUrl: 'http://localhost',
      dioOverride: _dioWith(FakeBackendAdapter()),
      refreshDioOverride: _refreshDio(),
      onSessionExpired: () async => expiredCalls++,
    );

    await expectLater(
      client.get<dynamic>('/accounts'),
      throwsA(isA<ApiException>()),
    );
    expect(session.isAuthenticated, isFalse);
    expect(await session.readRefreshToken(), isNull);
    expect(expiredCalls, 1);
  });

  test('401 sin refresh guardado -> limpia y avisa sin renovar', () async {
    final session = InMemorySessionRepository(
      initialAccessToken: 'expired-access',
    );

    var expiredCalls = 0;
    final client = ApiClient.create(
      session: session,
      baseUrl: 'http://localhost',
      dioOverride: _dioWith(FakeBackendAdapter()),
      refreshDioOverride: _refreshDio(),
      onSessionExpired: () async => expiredCalls++,
    );

    await expectLater(
      client.get<dynamic>('/accounts'),
      throwsA(isA<ApiException>()),
    );
    expect(session.isAuthenticated, isFalse);
    expect(expiredCalls, 1);
  });

  test('logout: revoca en servidor y limpia el storage', () async {
    final storage = InMemorySecureStorage();
    final session = SecureSessionRepository(storage: storage);
    await session.saveSession(accessToken: 'a', refreshToken: 'r');
    final secret = await session.getOrCreateDeviceSecret();

    final backend = FakeBackendAdapter();
    await SessionService(dio: _dioWith(backend), session: session).logout();

    expect(session.isAuthenticated, isFalse);
    expect(await session.readRefreshToken(), isNull);
    expect(
      storage.debugValues.containsKey(SecureSessionRepository.accessTokenKey),
      isFalse,
    );
    // El dispositivo se conserva para futuros logins (seam F-T03).
    expect(await session.getOrCreateDeviceSecret(), secret);
  });

  test('logout best-effort: sin red igual limpia local', () async {
    final session = InMemorySessionRepository();
    await session.saveSession(accessToken: 'a', refreshToken: 'r');

    final dio = Dio(BaseOptions(baseUrl: 'http://localhost'))
      ..httpClientAdapter = _FailingAdapter();

    await SessionService(dio: dio, session: session).logout();

    expect(session.isAuthenticated, isFalse);
    expect(await session.readRefreshToken(), isNull);
  });
}

/// Adaptador que siempre falla (caída de red).
class _FailingAdapter implements HttpClientAdapter {
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) {
    throw DioException(
      requestOptions: options,
      type: DioExceptionType.connectionError,
    );
  }

  @override
  void close({bool force = false}) {}
}
