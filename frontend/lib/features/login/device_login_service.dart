// Servicio HTTP del flujo "iniciar sesion en este dispositivo" (F-T56).
//
// Consume los endpoints de E1-T45/E1-T46 sobre [ApiClient] (que ya incluye
// el prefijo `/api/v1` en su `baseUrl`):
// - `POST /auth/login/device/request` con `{email, doc_type, document_number}`
//   -> siempre 200 con `data: {accepted, ttl_seconds, resend_wait_seconds}`,
//   exista o no la cuenta (anti-oraculo).
// - `POST /auth/login/device/complete` con `{email, doc_type,
//   document_number, code, pin, device_id, device_public_key, platform?,
//   biometric_type?, device_info?}` -> exito con `data: {access_token,
//   refresh_token, token_type, session_id, expires_in, user_ref,
//   biometric_enabled}` y abre sesion. `expires_in` es el TTL del refresh
//   en segundos (no del access token); se acepta el alias
//   `refresh_expires_in` si el backend lo expone con ese nombre.
//
// Cliente delgado (docs/19): no decide si la cuenta existe, si el documento
// coincide ni si el OTP/PIN son validos; solo transporta. Nunca loguea
// email, documento, OTP, PIN, tokens, `user_ref` ni `device_public_key`
// (docs/16 reglas 7 y 9).
// ignore_for_file: prefer_initializing_formals
// Razon: los parametros del constructor son API publica (`api:`) y no pueden
// ser formales inicializadores de campos privados.

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/errors/error_messages.dart';
import 'package:banca_online/core/http/api_client.dart';

/// Resultado de `POST /auth/login/device/request` (OTP `LOGIN`).
class DeviceLoginRequestResult {
  const DeviceLoginRequestResult({
    required this.accepted,
    required this.ttlSeconds,
    required this.resendWaitSeconds,
  });

  /// Siempre `true` en 200 (exista o no la cuenta).
  final bool accepted;

  /// Vigencia del OTP en segundos.
  final int ttlSeconds;

  /// Espera minima entre reenvios en segundos.
  final int resendWaitSeconds;
}

/// Resultado de `POST /auth/login/device/complete` (E1-T46).
///
/// Abre sesion: devuelve los tokens + la referencia del usuario para
/// persistir (`SessionIdentityStore`, F-T20) y dejar que la guarda navegue
/// a `/home`.
class DeviceLoginCompleteResult {
  const DeviceLoginCompleteResult({
    required this.accessToken,
    this.refreshToken,
    required this.userRef,
    this.sessionId = '',
    this.expiresIn = 0,
    this.biometricEnabled,
  });

  /// Access token de la sesion concedida.
  final String accessToken;

  /// Refresh token rotativo (puede ser `null` segun el backend).
  final String? refreshToken;

  /// Referencia del usuario (`user_ref`) para persistir.
  final String userRef;

  /// Identificador de la sesion (informativo).
  final String sessionId;

  /// TTL del refresh en segundos (informativo; el backend lo expone como
  /// `expires_in`, con alias aceptado `refresh_expires_in`).
  final int expiresIn;

  /// Consentimiento biometrico vigente informado por el servidor (E1-T39).
  /// `null`/ausente = no sincronizar (se conserva la cache previa).
  final bool? biometricEnabled;
}

/// Contrato del flujo de login en este dispositivo (seam testeable con
/// fakes; sin backend en F-T56 se prueba contra este contrato).
abstract class DeviceLoginService {
  /// Paso 1: solicita (o reutiliza) el OTP `LOGIN` para [email] + documento.
  /// Siempre 200 en exito (mensaje neutro, sin revelar existencia).
  ///
  /// Lanza [ApiException] con `code` `RATE_LIMITED` / `VALIDATION_ERROR` o
  /// de red (`NETWORK_ERROR` / `TIMEOUT_ERROR`).
  Future<DeviceLoginRequestResult> requestOtp({
    required String email,
    required String docType,
    required String documentNumber,
  });

  /// Paso 3: valida OTP + PIN de forma atomica y asocia el dispositivo.
  ///
  /// Lanza [ApiException] con `code` `INVALID_LOGIN` (401) /
  /// `ACCOUNT_LOCKED` (423) / `RATE_LIMITED` (429) / `VALIDATION_ERROR`
  /// (422) o de red.
  Future<DeviceLoginCompleteResult> complete({
    required String email,
    required String docType,
    required String documentNumber,
    required String code,
    required String pin,
    required String deviceId,
    required String devicePublicKey,
    String? platform,
    String? biometricType,
    Map<String, dynamic>? deviceInfo,
  });
}

/// Implementacion HTTP de [DeviceLoginService] sobre [ApiClient].
///
/// POST generico (NO idempotente): el login no mueve dinero, asi que la
/// regla 5 de docs/16 (`Idempotency-Key`) no aplica.
class HttpDeviceLoginService implements DeviceLoginService {
  HttpDeviceLoginService({required ApiClient api}) : _api = api;

  final ApiClient _api;

  /// Rutas relativas (`ApiClient` ya incluye `/api/v1` en su `baseUrl`).
  static const String requestPath = '/auth/login/device/request';
  static const String completePath = '/auth/login/device/complete';

  @override
  Future<DeviceLoginRequestResult> requestOtp({
    required String email,
    required String docType,
    required String documentNumber,
  }) async {
    final response = await _api.post<dynamic>(
      requestPath,
      data: <String, dynamic>{
        'email': email,
        'doc_type': docType,
        'document_number': documentNumber,
      },
    );
    final data = _dataOf(response.data);
    return DeviceLoginRequestResult(
      accepted: _bool(data, 'accepted', fallback: true),
      ttlSeconds: _int(data, 'ttl_seconds'),
      resendWaitSeconds: _int(data, 'resend_wait_seconds'),
    );
  }

  @override
  Future<DeviceLoginCompleteResult> complete({
    required String email,
    required String docType,
    required String documentNumber,
    required String code,
    required String pin,
    required String deviceId,
    required String devicePublicKey,
    String? platform,
    String? biometricType,
    Map<String, dynamic>? deviceInfo,
  }) async {
    final body = <String, dynamic>{
      'email': email,
      'doc_type': docType,
      'document_number': documentNumber,
      'code': code,
      'pin': pin,
      'device_id': deviceId,
      'device_public_key': devicePublicKey,
    };
    if (platform != null && platform.isNotEmpty) {
      body['platform'] = platform;
    }
    if (biometricType != null && biometricType.isNotEmpty) {
      body['biometric_type'] = biometricType;
    }
    if (deviceInfo != null && deviceInfo.isNotEmpty) {
      body['device_info'] = deviceInfo;
    }
    final response = await _api.post<dynamic>(completePath, data: body);
    final data = _dataOf(response.data);
    return DeviceLoginCompleteResult(
      accessToken: _string(data, 'access_token'),
      refreshToken: _optionalString(data, 'refresh_token'),
      userRef: _string(data, 'user_ref'),
      sessionId: _optionalString(data, 'session_id') ?? '',
      expiresIn: _optionalInt(data, 'expires_in') ??
          _optionalInt(data, 'refresh_expires_in') ??
          0,
      biometricEnabled: _optionalBool(data, 'biometric_enabled'),
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
    final value = _optionalString(data, key);
    if (value == null || value.isEmpty) {
      throw ApiException(code: 'UNKNOWN', message: messageForCode('UNKNOWN'));
    }
    return value;
  }

  String? _optionalString(Map<String, dynamic> data, String key) {
    final value = data[key];
    if (value == null) return null;
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
    final parsed = _optionalBool(data, key);
    if (parsed == null) return fallback;
    return parsed;
  }

  bool? _optionalBool(Map<String, dynamic> data, String key) {
    final value = data[key];
    if (value == null) return null;
    if (value is bool) return value;
    if (value is num) return value != 0;
    final text = '$value'.toLowerCase();
    if (text == 'true' || text == '1') return true;
    if (text == 'false' || text == '0') return false;
    return null;
  }
}
