// Abstraccion de biometria local (F-T03, lector real en F-T46).
//
// [SystemBiometricReader] usa `local_auth` (huella/Face ID del SO) con
// fallback a PIN ante cualquier estado no confirmado. El resto del feature
// programa contra [BiometricReader], con [FakeBiometricReader] para tests y
// desarrollo.
// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública (`auth:`,
// `canCheckBiometrics:`, ...) y no pueden ser formales inicializadores de
// campos privados (misma convención que `pin_setup_service.dart`).
library;

import 'package:local_auth/local_auth.dart';

/// Resultado de un intento de autenticacion biometrica.
class BiometricAuthResult {
  const BiometricAuthResult._({required this.authenticated});

  /// El usuario supero el desafio biometrico del SO.
  const BiometricAuthResult.authenticated() : this._(authenticated: true);

  /// Biometria no disponible, cancelada o fallida: la app debe ofrecer PIN
  /// (fallback que implementa E1-T16, no este feature).
  const BiometricAuthResult.requiresPinFallback()
      : this._(authenticated: false);

  final bool authenticated;

  /// `true` cuando hay que dirigir al usuario al login con PIN.
  bool get requiresPinFallback => !authenticated;
}

/// Lee la biometria del dispositivo (Face ID / huella del telefono).
///
/// El dispositivo NO ejecuta liveness ni reconocimiento propios (D07, docs/13
/// §1.2): solo desbloquea con el biometrico del SO para autorizar la firma
/// del `nonce` con la clave en almacenamiento seguro.
abstract class BiometricReader {
  /// `true` si el dispositivo puede pedir biometria ahora mismo.
  Future<bool> isAvailable();

  /// Pide al SO el desafio biometrico con el [reason] visible al usuario.
  /// Devuelve [BiometricAuthResult.authenticated] solo si el SO lo confirma;
  /// cualquier otro caso (no disponible, cancelado, fallido) devuelve
  /// [BiometricAuthResult.requiresPinFallback].
  Future<BiometricAuthResult> authenticate({required String reason});
}

/// Lector del SO con `local_auth` (F-T46).
///
/// Reglas (brief F-T46 + docs/16):
///  - `isAvailable()` refleja el dispositivo: `canCheckBiometrics &&
///    isDeviceSupported()` (sin hardware o sin biometria enrolada -> `false`).
///  - `authenticate` pide SOLO biometrico del SO (`biometricOnly: true`);
///    cualquier estado no confirmado (no disponible, denegado, cancelado por
///    el usuario, error o excepcion del plugin) devuelve
///    [BiometricAuthResult.requiresPinFallback]. Nunca lanza hacia la UI y
///    nunca bloquea: no hay lockout por biometria (el PIN es el fallback).
///  - Nunca registra el resultado biometrico (docs/16 reglas 7 y 10).
///
/// Testeabilidad: los seams `[canCheckBiometrics]`, `[isDeviceSupported]` y
/// `[authenticateWithReason]` inyectan el plugin en tests (asi no tocan
/// canales de plataforma); en produccion se usan con el valor por defecto
/// (`null` = `LocalAuthentication` real). [FakeBiometricReader] se mantiene
/// para el resto de la suite.
class SystemBiometricReader implements BiometricReader {
  SystemBiometricReader({
    LocalAuthentication? auth,
    Future<bool> Function()? canCheckBiometrics,
    Future<bool> Function()? isDeviceSupported,
    Future<bool> Function({required String reason})? authenticateWithReason,
  })  : _auth = auth ?? LocalAuthentication(),
        _canCheckBiometrics = canCheckBiometrics,
        _isDeviceSupported = isDeviceSupported,
        _authenticateWithReason = authenticateWithReason;

  final LocalAuthentication _auth;
  final Future<bool> Function()? _canCheckBiometrics;
  final Future<bool> Function()? _isDeviceSupported;
  final Future<bool> Function({required String reason})?
      _authenticateWithReason;

  @override
  Future<bool> isAvailable() async {
    try {
      final canCheck = _canCheckBiometrics != null
          ? await _canCheckBiometrics()
          : await _auth.canCheckBiometrics;
      if (!canCheck) return false;
      return _isDeviceSupported != null
          ? await _isDeviceSupported()
          : await _auth.isDeviceSupported();
    } catch (_) {
      // Fallo del plugin (sin canal, permiso denegado, etc.): no disponible,
      // el controlador cae a PIN.
      return false;
    }
  }

  @override
  Future<BiometricAuthResult> authenticate({required String reason}) async {
    try {
      final ok = _authenticateWithReason != null
          ? await _authenticateWithReason(reason: reason)
          : await _auth.authenticate(
              localizedReason: reason,
              options: const AuthenticationOptions(biometricOnly: true),
            );
      return ok
          ? const BiometricAuthResult.authenticated()
          : const BiometricAuthResult.requiresPinFallback();
    } catch (_) {
      // Cancelacion, denegacion, error inesperado: cae a PIN sin bloquear.
      return const BiometricAuthResult.requiresPinFallback();
    }
  }
}

/// Doble manual para tests y desarrollo (sin hardware ni plugins).
class FakeBiometricReader implements BiometricReader {
  /// [available] controla [isAvailable]; [succeeds] controla si
  /// [authenticate] aprueba (cuando hay disponibilidad).
  FakeBiometricReader({this._available = true, this._succeeds = true});

  final bool _available;
  final bool _succeeds;

  /// Veces que se invoco [authenticate] (para aserciones en tests).
  int authenticateCalls = 0;

  /// Ultimo `reason` recibido (para aserciones en tests).
  String? lastReason;

  @override
  Future<bool> isAvailable() async => _available;

  @override
  Future<BiometricAuthResult> authenticate({required String reason}) async {
    authenticateCalls++;
    lastReason = reason;
    if (!_available || !_succeeds) {
      return const BiometricAuthResult.requiresPinFallback();
    }
    return const BiometricAuthResult.authenticated();
  }
}
