// Abstraccion de biometria local (F-T03).
//
// `local_auth` NO esta en `pubspec.yaml` y esta tarea tiene prohibido
// modificarlo: [SystemBiometricReader] es el punto de integracion documentado
// (el orquestador agrega la dependencia y lo implementa con `local_auth`).
// El resto del feature programa contra [BiometricReader], con
// [FakeBiometricReader] para tests y desarrollo.
library;

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

/// Punto de integracion con `local_auth` (PENDIENTE de dependencia).
///
/// Cuando el orquestador agregue `local_auth: ^...` a `pubspec.yaml`,
/// implementar aqui:
/// ```dart
/// import 'package:local_auth/local_auth.dart';
///
/// class SystemBiometricReader implements BiometricReader {
///   SystemBiometricReader([LocalAuthentication? auth])
///       : _auth = auth ?? LocalAuthentication();
///
///   final LocalAuthentication _auth;
///
///   @override
///   Future<bool> isAvailable() async {
///     try {
///       return await _auth.canCheckBiometrics && await _auth.isDeviceSupported();
///     } catch (_) {
///       return false;
///     }
///   }
///
///   @override
///   Future<BiometricAuthResult> authenticate({required String reason}) async {
///     try {
///       final ok = await _auth.authenticate(
///         localizedReason: reason,
///         options: const AuthenticationOptions(biometricOnly: true),
///       );
///       return ok
///           ? const BiometricAuthResult.authenticated()
///           : const BiometricAuthResult.requiresPinFallback();
///     } catch (_) {
///       return const BiometricAuthResult.requiresPinFallback();
///     }
///   }
/// }
/// ```
/// Mientras tanto se comporta como "no disponible" para no bloquear el resto
/// del flujo (el controlador cae a fallback PIN).
class SystemBiometricReader implements BiometricReader {
  @override
  Future<bool> isAvailable() async => false;

  @override
  Future<BiometricAuthResult> authenticate({required String reason}) async =>
      const BiometricAuthResult.requiresPinFallback();
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
