import 'package:dio/dio.dart';

import 'error_messages.dart';

/// Error normalizado del backend (docs/05 §4) o de red.
///
/// - Negocio: `{ "error": { "code", "message", "details", "request_id" } }`.
/// - Red/timeout: sin respuesta; se mapea a `NETWORK_ERROR`/`TIMEOUT_ERROR`.
class ApiException implements Exception {
  ApiException({
    required this.code,
    required this.message,
    this.serverMessage,
    this.statusCode,
    this.requestId,
  });

  /// Codigo de negocio (`INSUFFICIENT_FUNDS`, ...) o de transporte
  /// (`NETWORK_ERROR`, `TIMEOUT_ERROR`, `SERVER_ERROR`, `UNKNOWN`).
  final String code;

  /// Mensaje listo para UI (espanol).
  final String message;

  /// Mensaje original del servidor (depuracion, nunca PII en logs).
  final String? serverMessage;

  /// HTTP status si hubo respuesta.
  final int? statusCode;

  /// `request_id` para correlacion, si vino en la respuesta.
  final String? requestId;

  /// `true` si es fallo de conectividad (para pantallas de error de red).
  bool get isNetworkError => code == 'NETWORK_ERROR' || code == 'TIMEOUT_ERROR';

  factory ApiException.fromDioException(DioException e) {
    // 1) Sin respuesta: red o timeout.
    if (e.type == DioExceptionType.connectionTimeout ||
        e.type == DioExceptionType.sendTimeout ||
        e.type == DioExceptionType.receiveTimeout) {
      return ApiException(
        code: 'TIMEOUT_ERROR',
        message: messageForCode('TIMEOUT_ERROR'),
        serverMessage: e.message,
      );
    }
    if (e.type == DioExceptionType.connectionError ||
        e.type == DioExceptionType.unknown &&
            e.response == null &&
            e.error != null) {
      return ApiException(
        code: 'NETWORK_ERROR',
        message: messageForCode('NETWORK_ERROR'),
        serverMessage: e.message,
      );
    }

    // 2) Con respuesta: formato docs/05 `{ "error": {...} }`.
    final data = e.response?.data;
    if (data is Map<String, dynamic> && data['error'] is Map) {
      final error = Map<String, dynamic>.from(data['error'] as Map);
      final code = (error['code'] as Object?)?.toString() ?? 'UNKNOWN';
      final serverMessage = (error['message'] as Object?)?.toString();
      final requestId = (error['request_id'] as Object?)?.toString();
      return ApiException(
        code: code,
        message: messageForCode(code),
        serverMessage: serverMessage,
        statusCode: e.response?.statusCode,
        requestId: requestId,
      );
    }

    // 3) HTTP sin cuerpo `error`: mapear por status.
    final status = e.response?.statusCode;
    if (status == 401) {
      return ApiException(
        code: 'NOT_AUTHORIZED',
        message: messageForCode('NOT_AUTHORIZED'),
        statusCode: status,
      );
    }
    if (status == 404) {
      return ApiException(
        code: 'NOT_FOUND',
        message: messageForCode('NOT_FOUND'),
        statusCode: status,
      );
    }
    if (status != null && status >= 500) {
      return ApiException(
        code: 'SERVER_ERROR',
        message: messageForCode('SERVER_ERROR'),
        statusCode: status,
      );
    }
    return ApiException(
      code: 'UNKNOWN',
      message: messageForCode('UNKNOWN'),
      serverMessage: e.message,
      statusCode: status,
    );
  }

  /// Error de red generico (para tests y uso manual).
  factory ApiException.network() => ApiException(
        code: 'NETWORK_ERROR',
        message: messageForCode('NETWORK_ERROR'),
      );

  @override
  String toString() => 'ApiException($code): $message';
}
