// Orquestador del login biometrico (F-T03, HU03 CA-01/CA-02).
//
// Secuencia: challenge -> biometrico -> firma -> facial, sobre [ApiClient]
// directo (docs/05 §6.1). La pantalla (UX, PIN de contingencia, timer de
// inactividad) la monta E1-T16; este controlador solo expone estado.
//
// Si la biometria falla o no esta disponible, el estado cae a
// [BiometricLoginState.pinFallback] con [pinFallbackRequired] en `true` y un
// mensaje que dirige al PIN. El PIN NO se implementa aqui.
library;

import 'package:flutter/foundation.dart';

import '../../core/errors/api_exception.dart';
import '../../core/http/api_client.dart';
import 'biometric_service.dart';

/// Estado del login biometrico.
enum BiometricLoginState {
  idle,
  requestingChallenge,
  awaitingBiometric,
  submitting,
  success,
  pinFallback,
  error,
}

/// Orquesta `challenge -> biometric -> sign -> facial` (HU03).
class LoginController extends ChangeNotifier {
  LoginController({required this._api, required this._biometrics});

  final ApiClient _api;
  final BiometricService _biometrics;

  /// Rutas relativas (el `baseUrl` de `ApiClient` ya incluye `/api/v1`).
  static const String challengePath = '/auth/login/challenge';
  static const String facialPath = '/auth/login/facial';

  BiometricLoginState _state = BiometricLoginState.idle;
  String? _errorMessage;
  Map<String, dynamic>? _sessionResult;

  BiometricLoginState get state => _state;
  String? get errorMessage => _errorMessage;
  Map<String, dynamic>? get sessionResult => _sessionResult;

  bool get busy =>
      _state == BiometricLoginState.requestingChallenge ||
      _state == BiometricLoginState.awaitingBiometric ||
      _state == BiometricLoginState.submitting;

  /// `true` cuando hay que dirigir al usuario al login con PIN (CA-02).
  bool get pinFallbackRequired => _state == BiometricLoginState.pinFallback;

  bool get succeeded => _state == BiometricLoginState.success;

  /// Ejecuta el login biometrico completo.
  ///
  /// [reason] es el texto que el SO muestra al pedir el biometrico.
  /// Nunca envia la firma si su formato es invalido (se detecta en cliente).
  Future<void> loginWithBiometrics({
    required String userRef,
    required String deviceId,
    required String reason,
  }) async {
    if (busy) return;
    _errorMessage = null;
    _sessionResult = null;
    try {
      // 1. Challenge: el servidor emite el `nonce` de un solo uso.
      _state = BiometricLoginState.requestingChallenge;
      notifyListeners();
      final challenge = await _api.post(
        challengePath,
        data: {'user_ref': userRef, 'device_id': deviceId},
      );
      final nonce = _dataOf(challenge.data)['nonce'] as String?;

      if (nonce == null || nonce.isEmpty) {
        _fail('El servidor no emitio un desafio valido. Reintenta.');
        return;
      }

      // 2. Biometrico del dispositivo (D07: validacion local, sin IA).
      _state = BiometricLoginState.awaitingBiometric;
      notifyListeners();
      final auth = await _biometrics.authenticate(reason: reason);
      if (!auth.authenticated) {
        _state = BiometricLoginState.pinFallback;
        _errorMessage =
            'Biometria no disponible o cancelada. Continua con tu PIN.';
        notifyListeners();
        return;
      }

      // 3. Firma del nonce con el secreto del dispositivo.
      final signature = await _biometrics.signNonce(nonce);
      if (!BiometricService.isValidSignatureFormat(signature)) {
        _fail('Firma generada con formato invalido. No se envio al servidor.');
        return;
      }

      // 4. Facial: el servidor valida la firma y abre sesion.
      _state = BiometricLoginState.submitting;
      notifyListeners();
      final facial = await _api.post(
        facialPath,
        data: {
          'nonce': nonce,
          'device_id': deviceId,
          'signature': signature,
        },
      );
      _sessionResult = _dataOf(facial.data);
      _state = BiometricLoginState.success;
      notifyListeners();
    } on ApiException catch (e) {
      _fail(e.message);
    }
  }

  void _fail(String message) {
    _state = BiometricLoginState.error;
    _errorMessage = message;
    notifyListeners();
  }

  /// Extrae el `data` del envelope docs/05 `{data, meta}`.
  static Map<String, dynamic> _dataOf(Object? body) {
    if (body is Map && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    throw const FormatException('Envelope de login sin campo data');
  }

  /// Vuelve a `idle` (p. ej. al salir de la pantalla de login).
  void reset() {
    _state = BiometricLoginState.idle;
    _errorMessage = null;
    _sessionResult = null;
    notifyListeners();
  }
}
