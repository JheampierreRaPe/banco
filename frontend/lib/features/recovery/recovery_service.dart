// Servicio HTTP de recuperación de acceso (F-T29, HU04).
//
// Consume los endpoints de `E1-T31` sobre [ApiClient] (que ya incluye el
// prefijo `/api/v1` en su `baseUrl`):
// - `POST /auth/recovery/request` con `{email}` -> siempre 200 con
//   `data: {accepted, ttl_seconds, resend_wait_seconds}`, exista o no el
//   email (anti-oráculo). `429 RATE_LIMITED`, `422` de esquema.
// - `POST /auth/recovery/verify` con
//   `{email, code, device_id?, device_public_key?, platform?}` -> éxito con
//   `data: {user_ref, device_bound}` (E1-T33/SCR-005: verify YA NO abre
//   sesión ni emite tokens; la única sesión la abre `POST /auth/login/pin`).
//   Error único de código: `401 INVALID_RECOVERY_CODE` (genérico: cubre
//   código incorrecto, OTP vencido e intentos agotados; ya NO existe
//   `EXPIRED_OTP`).
//
// Cliente delgado (docs/19): no decide si el email existe ni si el OTP es
// válido; solo transporta lo que responde el backend. Nunca loguea email,
// OTP, tokens, `user_ref` ni `device_id` (docs/16 reglas 7 y 9).
// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública (`api:`) y no pueden
// ser formales inicializadores de campos privados.

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/errors/error_messages.dart';
import 'package:banca_online/core/http/api_client.dart';

/// Resultado de `POST /auth/recovery/request`.
class RecoveryRequestResult {
  const RecoveryRequestResult({
    required this.accepted,
    required this.ttlSeconds,
    required this.resendWaitSeconds,
  });

  /// Siempre `true` (200 exista o no el email).
  final bool accepted;

  /// Vigencia del OTP en segundos.
  final int ttlSeconds;

  /// Espera mínima entre reenvíos en segundos.
  final int resendWaitSeconds;
}

/// Resultado de `POST /auth/recovery/verify` (E1-T33/SCR-005).
///
/// Verify YA NO abre sesión: solo devuelve la referencia del usuario para
/// continuar a `/login?userRef=` (en el flujo recuperación/pin-reset la
/// sesión NO se abre ahí; se abre al autenticarse en login —PIN o
/// biometría—). Sin `access_token`/`refresh_token`/`session_id`.
class RecoveryVerifyResult {
  const RecoveryVerifyResult({
    required this.userRef,
    this.deviceBound = false,
  });

  /// Referencia del usuario para persistir (`SessionIdentityStore`, F-T20)
  /// y navegar a `/login?userRef=<user_ref>`.
  final String userRef;

  /// Best-effort en el backend: `true` solo si se REGISTRÓ un binding nuevo;
  /// `false` no bloquea la continuación al login.
  final bool deviceBound;
}

/// Contrato del feature de recuperación (seam testeable con fakes).
abstract class RecoveryService {
  /// Solicita el OTP para [email]. Siempre 200 en éxito (mensaje neutro).
  ///
  /// Lanza [ApiException] con `code` `RATE_LIMITED` / `VALIDATION_ERROR` o
  /// de red (`NETWORK_ERROR` / `TIMEOUT_ERROR`).
  Future<RecoveryRequestResult> request({required String email});

  /// Verifica el [code] para [email] ligando el dispositivo nuevo.
  ///
  /// Lanza [ApiException] con `code` `INVALID_RECOVERY_CODE` (único error
  /// genérico de código) / `RATE_LIMITED` / `VALIDATION_ERROR` o de red.
  Future<RecoveryVerifyResult> verify({
    required String email,
    required String code,
    String? deviceId,
    String? devicePublicKey,
    String? platform,
  });
}

/// Implementación HTTP de [RecoveryService] sobre [ApiClient].
///
/// POST genérico (NO idempotente): la recuperación no mueve dinero, así que
/// la regla 5 de docs/16 (`Idempotency-Key`) no aplica.
class HttpRecoveryService implements RecoveryService {
  HttpRecoveryService({required ApiClient api}) : _api = api;

  final ApiClient _api;

  /// Rutas relativas (`ApiClient` ya incluye `/api/v1` en su `baseUrl`).
  static const String requestPath = '/auth/recovery/request';
  static const String verifyPath = '/auth/recovery/verify';

  @override
  Future<RecoveryRequestResult> request({required String email}) async {
    final response = await _api.post<dynamic>(
      requestPath,
      data: <String, dynamic>{'email': email},
    );
    final data = _dataOf(response.data);
    return RecoveryRequestResult(
      accepted: _bool(data, 'accepted', fallback: true),
      ttlSeconds: _int(data, 'ttl_seconds'),
      resendWaitSeconds: _int(data, 'resend_wait_seconds'),
    );
  }

  @override
  Future<RecoveryVerifyResult> verify({
    required String email,
    required String code,
    String? deviceId,
    String? devicePublicKey,
    String? platform,
  }) async {
    final body = <String, dynamic>{'email': email, 'code': code};
    if (deviceId != null && deviceId.isNotEmpty) {
      body['device_id'] = deviceId;
    }
    if (devicePublicKey != null && devicePublicKey.isNotEmpty) {
      body['device_public_key'] = devicePublicKey;
    }
    if (platform != null && platform.isNotEmpty) {
      body['platform'] = platform;
    }
    final response = await _api.post<dynamic>(verifyPath, data: body);
    final data = _dataOf(response.data);
    return RecoveryVerifyResult(
      userRef: _string(data, 'user_ref'),
      deviceBound: _bool(data, 'device_bound', fallback: false),
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

  int _int(Map<String, dynamic> data, String key) {
    final parsed = _optionalInt(data, key);
    if (parsed == null) {
      throw ApiException(code: 'UNKNOWN', message: messageForCode('UNKNOWN'));
    }
    return parsed;
  }

  int? _optionalInt(Map<String, dynamic> data, String key) {
    final value = data[key];
    if (value == null) return null;
    if (value is num) return value.toInt();
    return int.tryParse('$value');
  }

  bool _bool(Map<String, dynamic> data, String key,
      {required bool fallback}) {
    final value = data[key];
    if (value == null) return fallback;
    if (value is bool) return value;
    if (value is num) return value != 0;
    final text = '$value'.toLowerCase();
    if (text == 'true' || text == '1') return true;
    if (text == 'false' || text == '0') return false;
    return fallback;
  }
}
