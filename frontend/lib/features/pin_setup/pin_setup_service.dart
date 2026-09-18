// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública (`api:`) y no pueden
// ser formales inicializadores de campos privados (`this._api` los volvería
// parámetros privados e inutilizables desde otros archivos).
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/errors/error_messages.dart';
import 'package:banca_online/core/http/api_client.dart';

/// Resultado de `POST /auth/pin/setup`.
///
/// Shape: `{ "data": { "user_id": ..., "status": ... }, "meta": {...} }`.
class PinSetupResult {
  const PinSetupResult({required this.userId, required this.status});

  final String userId;
  final String status;
}

/// Contrato del feature de creación de PIN (seam testeable con fakes).
abstract class PinSetupService {
  /// Crea el PIN consumiendo un OTP ACTIVATION válido y sin usar.
  ///
  /// Lanza [ApiException] con `code` `INVALID_SETUP_CODE` (401, exista o no
  /// el usuario), `PIN_ALREADY_SET` (409), validación (422) o de red.
  Future<PinSetupResult> setup({
    required String userRef,
    required String code,
    required String pin,
  });
}

/// Implementación HTTP de [PinSetupService] sobre [ApiClient].
///
/// - Usa `POST` genérico (NO idempotente): crear el PIN no mueve dinero, así
///   que la regla 5 de docs/16 (`Idempotency-Key`) no aplica (convención de
///   features §4: solo el dinero usa `postWithIdempotency`).
/// - Nunca registra el PIN en logs (docs/16 reglas 7 y 10).
class HttpPinSetupService implements PinSetupService {
  HttpPinSetupService({required ApiClient api}) : _api = api;

  final ApiClient _api;

  static const String setupPath = '/auth/pin/setup';

  @override
  Future<PinSetupResult> setup({
    required String userRef,
    required String code,
    required String pin,
  }) async {
    final response = await _api.post<dynamic>(
      setupPath,
      data: <String, dynamic>{
        'user_ref': userRef,
        'code': code,
        'pin': pin,
      },
    );
    final data = _dataOf(response.data);
    return PinSetupResult(
      userId: _string(data, 'user_id'),
      status: _string(data, 'status'),
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
}
