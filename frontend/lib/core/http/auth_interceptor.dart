import 'package:dio/dio.dart';

import '../session/session_repository.dart';

/// Adjunta `Authorization: Bearer` y renueva el access token ante 401.
///
/// Contrato `POST /auth/refresh` (docs/05 §6.1; shape del request/response
/// verificado en `backend/tests/test_sessions.py`):
/// request `{"refresh_token": ...}` → `200 {data: {access_token,
/// refresh_token, token_type, session_id, expires_in}, meta: {...}}`;
/// refresh revocado/expirado/desconocido → `401 {error: {code:
/// INVALID_REFRESH | REFRESH_EXPIRED | REFRESH_REUSED | SESSION_INACTIVE}}`.
///
/// Flujo:
/// 1. `onRequest` adjunta el access en memoria (getter síncrono).
/// 2. `onError` ante 401 (UNA sola vez por request; nunca para las propias
///    rutas `/auth/refresh` ni `/auth/logout`): pide el par nuevo con el
///    refresh, lo guarda con `saveSession` y reintenta la petición original
///    UNA vez con el token nuevo.
/// 3. Si no hay refresh o la renovación falla, limpia todo con
///    `clearOnInvalidRefresh` y avisa vía [onSessionExpired]; `go_router`
///    (`refreshListenable`) redirige a `/login` al notificar.
///
/// Nunca loguea tokens (docs/16 reglas 7 y 10).
class AuthInterceptor extends Interceptor {
  AuthInterceptor(
    this._session, {
    Dio? refreshDio,
    Future<void> Function()? onSessionExpired,
  }) {
    _refreshDio = refreshDio;
    _onSessionExpired = onSessionExpired;
  }

  final SessionRepository _session;
  late final Dio? _refreshDio;
  late final Future<void> Function()? _onSessionExpired;

  /// Dio principal: lo asigna [ApiClient.create] para reintentar tras renovar.
  Dio? retryDio;

  static const _retryKey = 'auth_interceptor.retried';
  static const _refreshPath = '/auth/refresh';
  static const _logoutPath = '/auth/logout';

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    final token = _session.currentAccessToken;
    if (token != null && token.isNotEmpty) {
      options.headers['Authorization'] = 'Bearer $token';
    }
    handler.next(options);
  }

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) async {
    if (!_shouldAttemptRefresh(err)) {
      return handler.next(err);
    }
    final refreshToken = await _session.readRefreshToken();
    if (refreshToken == null || refreshToken.isEmpty) {
      await _expireSession();
      return handler.next(err);
    }
    try {
      final tokens = await _refreshTokens(err.requestOptions, refreshToken);
      await _session.saveSession(
        accessToken: tokens.accessToken,
        refreshToken: tokens.refreshToken,
      );
      final request = err.requestOptions
        ..headers['Authorization'] = 'Bearer ${tokens.accessToken}'
        ..extra[_retryKey] = true;
      final response = await (retryDio ?? _isolatedDio(request)).fetch(request);
      return handler.resolve(response);
    } catch (_) {
      await _expireSession();
      return handler.next(err);
    }
  }

  bool _shouldAttemptRefresh(DioException err) {
    if (err.response?.statusCode != 401) return false;
    if (err.requestOptions.extra[_retryKey] == true) return false;
    final path = err.requestOptions.path;
    if (path == _refreshPath || path == _logoutPath) return false;
    return true;
  }

  Future<({String accessToken, String refreshToken})> _refreshTokens(
    RequestOptions failedRequest,
    String refreshToken,
  ) async {
    final client = _refreshDio ?? _isolatedDio(failedRequest);
    final response = await client.post(
      _refreshPath,
      data: {'refresh_token': refreshToken},
    );
    final data = response.data;
    final payload = data is Map<String, dynamic>
        ? data['data'] as Map<String, dynamic>?
        : null;
    final access = payload?['access_token']?.toString();
    final rotated = payload?['refresh_token']?.toString();
    if (access == null ||
        access.isEmpty ||
        rotated == null ||
        rotated.isEmpty) {
      throw const FormatException('respuesta de refresh sin tokens');
    }
    return (accessToken: access, refreshToken: rotated);
  }

  /// Dio aislado (sin interceptores, evita recursión) para el refresh o el
  /// reintento cuando [retryDio]/[_refreshDio] no se inyectaron.
  Dio _isolatedDio(RequestOptions request) => Dio(
        BaseOptions(
          baseUrl: request.baseUrl,
          headers: const {'Content-Type': 'application/json'},
        ),
      );

  Future<void> _expireSession() async {
    await _session.clearOnInvalidRefresh();
    await _onSessionExpired?.call();
  }
}
