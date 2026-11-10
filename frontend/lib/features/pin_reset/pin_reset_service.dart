// Servicio HTTP del restablecimiento de PIN (F-T43, HU02/HU04).
//
// Consume los endpoints vigentes de `docs/05` §6.1 sobre [ApiClient] (que ya
// incluye el prefijo `/api/v1` en su `baseUrl`):
// - `POST /auth/recovery/request` con `{email}` -> siempre 200 con
//   `data: {accepted, ttl_seconds, resend_wait_seconds}`, exista o no el
//   email (anti-oráculo; reutiliza el `PENDING` vigente con cooldown). El OTP
//   es solo email (sin SMS). NO se usa `/auth/otp/resend` (ese es de
//   activación).
// - `POST /auth/pin-reset` con `{email, doc_number, doc_type, code, pin}` ->
//   éxito con
//   `data: {user_ref, pin_set: true}` (consume el OTP `RECOVERY`, fija el PIN
//   y NO abre sesión ni emite tokens; la única sesión la abre
//   `POST /auth/login/pin`). Error único de negocio: `401 INVALID_PIN_RESET`
//   (genérico: cubre email no registrado, DNI que no coincide, sin OTP,
//   código incorrecto, OTP vencido y OTP bloqueado).
//
// Cliente delgado (docs/19): no decide si el email existe, si el DNI
// coincide ni si el OTP/PIN son válidos; solo transporta. Nunca loguea
// email, DNI, OTP, PIN ni `user_ref` (docs/16 reglas 7 y 9).
// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública (`api:`) y no pueden
// ser formales inicializadores de campos privados.

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/errors/error_messages.dart';
import 'package:banca_online/core/http/api_client.dart';

/// Resultado de `POST /auth/recovery/request` (OTP `RECOVERY`).
class PinResetRequestResult {
  const PinResetRequestResult({
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

/// Resultado de `POST /auth/pin-reset` (E1-T34/SCR-005).
///
/// NO abre sesión: solo devuelve la referencia del usuario para continuar a
/// `/login?userRef=` (en el flujo recuperación/pin-reset la sesión NO se
/// abre ahí; se abre al autenticarse en login —PIN o biometría—). Sin
/// `access_token`/`refresh_token`/`session_id`.
class PinResetResult {
  const PinResetResult({required this.userRef, required this.pinSet});

  /// Referencia del usuario para persistir (`SessionIdentityStore`, F-T20)
  /// y navegar a `/login?userRef=<user_ref>`.
  final String userRef;

  /// Siempre `true` en éxito.
  final bool pinSet;
}

/// Contrato del feature de restablecimiento de PIN (seam testeable con
/// fakes).
abstract class PinResetService {
  /// Solicita (o reutiliza) el OTP `RECOVERY` para [email]. Siempre 200 en
  /// éxito (mensaje neutro).
  ///
  /// Lanza [ApiException] con `code` `RATE_LIMITED` / `VALIDATION_ERROR` o
  /// de red (`NETWORK_ERROR` / `TIMEOUT_ERROR`).
  Future<PinResetRequestResult> requestOtp({required String email});

  /// Fija el PIN consumiendo el OTP `RECOVERY`. Sin abrir sesión.
  ///
  /// Lanza [ApiException] con `code` `INVALID_PIN_RESET` (401, único error
  /// genérico de email/DNI/OTP) / `RATE_LIMITED` / `VALIDATION_ERROR` (422:
  /// email/PIN malformados o PIN débil) o de red.
  Future<PinResetResult> resetPin({
    required String email,
    required String docNumber,
    required String code,
    required String pin,
    String docType = 'DNI',
  });
}

/// Implementación HTTP de [PinResetService] sobre [ApiClient].
///
/// POST genérico (NO idempotente): el reset no mueve dinero, así que la
/// regla 5 de docs/16 (`Idempotency-Key`) no aplica.
class HttpPinResetService implements PinResetService {
  HttpPinResetService({required ApiClient api}) : _api = api;

  final ApiClient _api;

  /// Rutas relativas (`ApiClient` ya incluye `/api/v1` en su `baseUrl`).
  static const String requestPath = '/auth/recovery/request';
  static const String resetPath = '/auth/pin-reset';

  @override
  Future<PinResetRequestResult> requestOtp({required String email}) async {
    final response = await _api.post<dynamic>(
      requestPath,
      data: <String, dynamic>{'email': email},
    );
    final data = _dataOf(response.data);
    return PinResetRequestResult(
      accepted: _bool(data, 'accepted', fallback: true),
      ttlSeconds: _int(data, 'ttl_seconds'),
      resendWaitSeconds: _int(data, 'resend_wait_seconds'),
    );
  }

  @override
  Future<PinResetResult> resetPin({
    required String email,
    required String docNumber,
    required String code,
    required String pin,
    String docType = 'DNI',
  }) async {
    final response = await _api.post<dynamic>(
      resetPath,
      data: <String, dynamic>{
        'email': email,
        'doc_number': docNumber,
        'doc_type': docType,
        'code': code,
        'pin': pin,
      },
    );
    final data = _dataOf(response.data);
    return PinResetResult(
      userRef: _string(data, 'user_ref'),
      pinSet: _bool(data, 'pin_set', fallback: true),
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
