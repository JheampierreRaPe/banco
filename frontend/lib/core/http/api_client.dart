import 'package:dio/dio.dart';
import 'package:uuid/uuid.dart';

import '../config/app_config.dart';
import '../errors/api_exception.dart';
import '../session/session_repository.dart';
import 'auth_interceptor.dart';
import 'request_id_interceptor.dart';

/// Cliente HTTP base (`dio`) con interceptores y formato docs/05.
///
/// - `Authorization: Bearer` desde [SessionRepository].
/// - `X-Request-Id` (uuid por request).
/// - `Idempotency-Key` (uuid) solo en POST que mueven dinero, via
///   [postWithIdempotency].
/// - Timeouts cortos (hilo no bloqueado, docs/16 §7).
/// - Errores mapeados a [ApiException] (mensajes UI en espanol).
class ApiClient {
  ApiClient({required this._dio, required this._uuid});

  final Dio _dio;
  final Uuid _uuid;

  /// Fabrica por defecto. [dioOverride] y [refreshDioOverride] solo existen
  /// para tests (evitan salir a red).
  factory ApiClient.create({
    required SessionRepository session,
    String? baseUrl,
    Dio? dioOverride,
    Uuid? uuid,
    Dio? refreshDioOverride,
    Future<void> Function()? onSessionExpired,
  }) {
    final resolvedUuid = uuid ?? const Uuid();
    final dio = dioOverride ??
        Dio(
          BaseOptions(
            baseUrl: baseUrl ?? AppConfig.apiUrl,
            connectTimeout: const Duration(seconds: 10),
            receiveTimeout: const Duration(seconds: 15),
            sendTimeout: const Duration(seconds: 15),
            headers: const {'Content-Type': 'application/json'},
          ),
        );
    // Cliente aislado (sin interceptores) para POST /auth/refresh: evita
    // recursión del interceptor sobre la propia renovación.
    final refreshDio = refreshDioOverride ??
        Dio(
          BaseOptions(
            baseUrl: dio.options.baseUrl,
            connectTimeout: const Duration(seconds: 10),
            receiveTimeout: const Duration(seconds: 15),
            sendTimeout: const Duration(seconds: 15),
            headers: const {'Content-Type': 'application/json'},
          ),
        );
    final auth = AuthInterceptor(
      session,
      refreshDio: refreshDio,
      onSessionExpired: onSessionExpired,
    );
    dio.interceptors.addAll([
      auth,
      RequestIdInterceptor(uuid: resolvedUuid),
    ]);
    auth.retryDio = dio;
    return ApiClient(dio: dio, uuid: resolvedUuid);
  }

  /// Acceso de solo lectura al `dio` subyacente (para features).
  Dio get dio => _dio;

  /// POST idempotente para operaciones que mueven dinero (docs/05 §3).
  ///
  /// Genera un `Idempotency-Key` uuid v4 salvo que se pase [idempotencyKey]
  /// (p. ej. reintento del mismo intento de pago: reutilizar la misma clave).
  Future<Response<T>> postWithIdempotency<T>(
    String path, {
    Object? data,
    Map<String, dynamic>? queryParameters,
    Options? options,
    CancelToken? cancelToken,
    String? idempotencyKey,
  }) async {
    try {
      return await _dio.post<T>(
        path,
        data: data,
        queryParameters: queryParameters,
        cancelToken: cancelToken,
        options: (options ?? Options()).copyWith(
          headers: {
            ...?options?.headers,
            'Idempotency-Key': idempotencyKey ?? _uuid.v4(),
          },
        ),
      );
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
  }

  /// GET con mapeo de errores a [ApiException].
  Future<Response<T>> get<T>(
    String path, {
    Map<String, dynamic>? queryParameters,
    Options? options,
    CancelToken? cancelToken,
  }) async {
    try {
      return await _dio.get<T>(
        path,
        queryParameters: queryParameters,
        options: options,
        cancelToken: cancelToken,
      );
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
  }

  /// POST generico (NO idempotente). Para mover dinero usar
  /// [postWithIdempotency].
  Future<Response<T>> post<T>(
    String path, {
    Object? data,
    Map<String, dynamic>? queryParameters,
    Options? options,
    CancelToken? cancelToken,
  }) async {
    try {
      return await _dio.post<T>(
        path,
        data: data,
        queryParameters: queryParameters,
        options: options,
        cancelToken: cancelToken,
      );
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
  }
}
