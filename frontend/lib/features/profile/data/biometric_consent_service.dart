import '../../../core/http/api_client.dart';

/// Contrato del consentimiento biometrico (F-T52, E1-T39).
///
/// Seam para tests de widgets con mock: el servidor es la autoridad, el
/// cliente solo envia la intencion (`enabled`) y refleja lo que responde.
abstract class BiometricConsentServiceBase {
  /// Fija el consentimiento y devuelve el `biometric_enabled` autoritativo
  /// del servidor (`200 {"data": {"biometric_enabled": bool}}`).
  Future<bool> setConsent({required bool enabled});
}

/// Consentimiento biometrico sobre [ApiClient] (`docs/05` §6.1, E1-T39).
///
/// - `POST /auth/biometric/consent` (Bearer: el cliente ya adjunta el JWT de
///   la sesion activa) con body `{"enabled": true|false}` (base `/api/v1` ya
///   incluida en el cliente).
/// - No mueve dinero: POST generico, sin `Idempotency-Key`.
/// - Nunca loguea el flag con datos ni tokens (docs/16 reglas 7 y 10).
class HttpBiometricConsentService implements BiometricConsentServiceBase {
  HttpBiometricConsentService({required this.api});

  final ApiClient api;

  @override
  Future<bool> setConsent({required bool enabled}) async {
    final resp = await api.post(
      '/auth/biometric/consent',
      data: {'enabled': enabled},
    );
    final data = resp.data;
    if (data is Map<String, dynamic>) {
      final inner = data['data'];
      if (inner is Map && inner['biometric_enabled'] is bool) {
        return inner['biometric_enabled'] as bool;
      }
    }
    throw const FormatException('Respuesta de consentimiento invalida');
  }
}
