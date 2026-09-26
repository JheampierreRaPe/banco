// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública (`api:`, `session:`)
// y no pueden ser formales inicializadores de campos privados (`this._api`
// los volvería parámetros privados e inutilizables desde otros archivos).
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/errors/error_messages.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/session_repository.dart';

/// Resultado de `POST /auth/activate` (docs/05 §6.1, HU02).
///
/// Shape verificado contra `backend/tests/test_activation.py`:
/// `{ "data": { "user_id": ..., "status": "ACTIVE" }, "meta": {...} }`.
/// El backend (E1-T10) NO devuelve tokens: la cuenta queda `ACTIVE` y el
/// primer inicio de sesión ocurre vía login (biometría/PIN, F-T03).
class ActivationResult {
  const ActivationResult({required this.userId, required this.status});

  final String userId;
  final String status;

  bool get isActive => status == 'ACTIVE';
}

/// Resultado de `POST /auth/otp/resend`.
///
/// Shape verificado contra `backend/tests/test_activation.py`:
/// `{ "data": { "user_id": ..., "resend_count": N, "expires_in": S }, ... }`.
class ResendResult {
  const ResendResult({
    required this.userId,
    required this.resendCount,
    required this.expiresInSeconds,
  });

  final String userId;
  final int resendCount;
  final int expiresInSeconds;
}

/// Contrato del feature de activación (seam testeable con fakes).
abstract class ActivationService {
  /// Valida el OTP y activa la cuenta.
  ///
  /// Lanza [ApiException] con `code` `INVALID_OTP` / `EXPIRED_OTP` /
  /// `RATE_LIMITED` o de red (`NETWORK_ERROR` / `TIMEOUT_ERROR`).
  Future<ActivationResult> activate({
    required String userRef,
    required String code,
  });

  /// Reenvía un nuevo OTP (invalida el anterior).
  ///
  /// Lanza [ApiException] con `code` `INVALID_OTP` / `RESEND_LIMIT` /
  /// `RATE_LIMITED` o de red.
  Future<ResendResult> resend({required String userRef, String? channel});
}

/// Implementación HTTP de [ActivationService] sobre [ApiClient].
///
/// - Usa `POST` genérico (NO idempotente): la activación no mueve dinero, así
///   que la regla 5 de docs/16 (`Idempotency-Key`) no aplica (convención de
///   features §4: solo el dinero usa `postWithIdempotency`).
/// - Si la respuesta trajera tokens (`access_token` / `refresh_token`), los
///   guarda en [SessionRepository]; hoy el backend no los devuelve, así que
///   es un camino defensivo hacia compatibilidad futura (verificado el shape
///   en `backend/tests/test_activation.py`: solo `user_id` + `status`).
class HttpActivationService implements ActivationService {
  HttpActivationService({
    required ApiClient api,
    required SessionRepository session,
  })  : _api = api,
        _session = session;

  final ApiClient _api;
  final SessionRepository _session;

  static const String activatePath = '/auth/activate';
  static const String resendPath = '/auth/otp/resend';

  @override
  Future<ActivationResult> activate({
    required String userRef,
    required String code,
  }) async {
    final response = await _api.post<dynamic>(
      activatePath,
      data: <String, dynamic>{'user_ref': userRef, 'code': code},
    );
    final data = _dataOf(response.data);
    final result = ActivationResult(
      userId: _string(data, 'user_id'),
      status: _string(data, 'status'),
    );
    // Camino defensivo: solo se toca la sesión si hay tokens en la respuesta.
    final accessToken =
        _optionalString(data, const ['access_token', 'accessToken']);
    if (accessToken != null && accessToken.isNotEmpty) {
      await _session.saveSession(
        accessToken: accessToken,
        refreshToken:
            _optionalString(data, const ['refresh_token', 'refreshToken']),
      );
    }
    return result;
  }

  @override
  Future<ResendResult> resend({required String userRef, String? channel}) async {
    final body = <String, dynamic>{'user_ref': userRef};
    if (channel != null && channel.isNotEmpty) {
      body['channel'] = channel;
    }
    final response = await _api.post<dynamic>(resendPath, data: body);
    final data = _dataOf(response.data);
    return ResendResult(
      userId: _string(data, 'user_id'),
      resendCount: _int(data, 'resend_count'),
      expiresInSeconds: _int(data, 'expires_in'),
    );
  }

  /// Extrae `data` del sobre docs/05 `{ "data": {...}, "meta": {...} }`.
  Map<String, dynamic> _dataOf(Object? body) {
    if (body is Map && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    throw ApiException(code: 'UNKNOWN', message: messageForCode('UNKNOWN'));
  }

  String _string(Map<String, dynamic> data, String key) {
    final value = data[key];
    if (value == null || '$value'.isEmpty) {
      throw ApiException(code: 'UNKNOWN', message: messageForCode('UNKNOWN'));
    }
    return '$value';
  }

  String? _optionalString(Map<String, dynamic> data, List<String> keys) {
    for (final key in keys) {
      final value = data[key];
      if (value != null && '$value'.isNotEmpty) return '$value';
    }
    return null;
  }

  int _int(Map<String, dynamic> data, String key) {
    final value = data[key];
    if (value is num) return value.toInt();
    final parsed = int.tryParse('$value');
    if (parsed == null) {
      throw ApiException(code: 'UNKNOWN', message: messageForCode('UNKNOWN'));
    }
    return parsed;
  }
}
