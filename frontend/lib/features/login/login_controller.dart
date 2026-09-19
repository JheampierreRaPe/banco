// Controlador delgado de la pantalla de login (E1-T16, HU03 CA-01/CA-02/CA-04).
//
// NO duplica a F-T03: el flujo `challenge -> biometrico -> firma -> facial`
// lo ejecuta el orquestador de biometria ([biometrics LoginController]); este
// controlador solo anade lo que E1-T16 exige alrededor:
//
//  - PIN de contingencia directo (`POST /auth/login/pin`) via [ApiClient].
//  - Guardado de sesion en [SessionRepository] tras el exito por CUALQUIERA
//    de las dos vias (luego la guarda de `app_router.dart` navega a `/home`).
//  - Temporizador visible de inactividad que al expirar limpia la sesion.
//  - Mensaje de error GENERICO unico para ambas vias: nunca filtra si fallo
//    el facial o el PIN (docs/16 reglas 7 y 9).
// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública (`api:`,
// `session:`, `biometricLogin:`) y no pueden ser formales inicializadores de
// campos privados (misma convención que `activation_controller.dart`).
library;

import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../core/errors/api_exception.dart';
import '../../core/http/api_client.dart';
import '../../core/session/session_identity_store.dart';
import '../../core/session/session_repository.dart';
import '../biometrics/login_controller.dart' as bio;

/// Inactividad en segundos antes del cierre automatico de sesion.
///
/// `session.inactivity_seconds` (brief E1-T16) no tiene constante en F-T02
/// (verificado: ninguna referencia en `lib/`), asi que se documenta aqui:
/// 180 s (3 min). Se inyecta otro valor por constructor solo en tests.
const int kLoginInactivityTimeoutSeconds = 180;

/// Controlador delgado del login con biometria + PIN + inactividad.
class LoginController extends ChangeNotifier {
  LoginController({
    required ApiClient api,
    required SessionRepository session,
    required bio.LoginController biometricLogin,
    SessionIdentityStore? identity,
    String? platform,
    String? biometricType,
    this.inactivityTimeoutSeconds = kLoginInactivityTimeoutSeconds,
  })  : _api = api,
        _session = session,
        _biometricLogin = biometricLogin,
        _identity = identity,
        _platform = platform,
        _biometricType = biometricType {
    _remainingSeconds = inactivityTimeoutSeconds;
  }

  final ApiClient _api;
  final SessionRepository _session;
  final bio.LoginController _biometricLogin;

  /// Store F-T20 para recordar el ultimo `user_ref` tras un login exitoso.
  /// Puede ser `null` (tests sin store): en ese caso no se persiste nada.
  final SessionIdentityStore? _identity;

  /// Plataforma explicita (`android`/`ios`); `null` = resolver del sistema.
  final String? _platform;

  /// Tipo biometrico (`FACE`/`FINGERPRINT`) si se conoce; `null` = omitir.
  final String? _biometricType;

  /// Tipos aceptados por el backend para `biometric_type`.
  static const String biometricTypeFace = 'FACE';
  static const String biometricTypeFingerprint = 'FINGERPRINT';

  /// Ruta relativa (`ApiClient` ya incluye `/api/v1` en su `baseUrl`).
  static const String pinPath = '/auth/login/pin';

  /// Mensaje UNICO para cualquier fallo de autenticacion (facial invalido o
  /// PIN mal): no filtra cual via fallo.
  static const String genericAuthErrorMessage =
      'No pudimos verificar tu identidad. Revisa tus datos e inténtalo de nuevo.';

  /// Mensaje cuando la biometria no esta disponible (dirige al PIN).
  static const String pinFallbackMessage =
      'Biometría no disponible. Ingresa tu PIN para continuar.';

  /// Mensaje al expirar la sesion por inactividad.
  static const String sessionExpiredMessage =
      'Tu sesión se cerró por inactividad. Inicia sesión de nuevo.';

  /// Segundos de inactividad configurados (180 s en produccion).
  final int inactivityTimeoutSeconds;

  bool _busy = false;
  bool _succeeded = false;
  bool _showPinFallback = false;
  bool _expired = false;
  String? _errorMessage;
  String? _infoMessage;
  int _remainingSeconds = 0;
  Timer? _timer;
  Future<void> Function()? _onExpired;

  bool get busy => _busy;
  bool get succeeded => _succeeded;

  /// `true` cuando hay que dirigir al usuario al PIN (CA-02).
  bool get showPinFallback => _showPinFallback;
  bool get expired => _expired;
  String? get errorMessage => _errorMessage;
  String? get infoMessage => _infoMessage;

  /// Segundos restantes del temporizador visible de inactividad.
  int get remainingSeconds => _remainingSeconds;

  /// Intento biometrico: delega TODO en F-T03 y solo traduce el resultado.
  ///
  /// - Exito -> guarda sesion y retorna `true` (la pagina navega a `/home`).
  /// - Biometria no disponible/cancelada -> senala el PIN, retorna `false`.
  /// - Cualquier otro fallo (incluido facial invalido) -> mensaje GENERICO.
  Future<bool> loginWithBiometrics({
    required String userRef,
    required String deviceId,
    required String reason,
  }) async {
    if (_busy) return false;
    _busy = true;
    _errorMessage = null;
    _infoMessage = null;
    _showPinFallback = false;
    notifyListeners();

    var ok = false;
    try {
      await _biometricLogin.loginWithBiometrics(
        userRef: userRef,
        deviceId: deviceId,
        reason: reason,
      );
      if (_biometricLogin.succeeded) {
        ok = await _saveSession(_biometricLogin.sessionResult);
        if (ok) {
          _succeeded = true;
          stopInactivityTimer();
          await _rememberUser(userRef);
        } else {
          _errorMessage = genericAuthErrorMessage;
        }
      } else if (_biometricLogin.pinFallbackRequired) {
        _showPinFallback = true;
        _infoMessage = pinFallbackMessage;
      } else {
        // Facial invalido, challenge roto, red, etc.: mensaje generico para no
        // filtrar que via fallo.
        _errorMessage = genericAuthErrorMessage;
      }
    } on ApiException {
      _errorMessage = genericAuthErrorMessage;
    } catch (_) {
      // Fallo inesperado (p. ej. derivar la clave del dispositivo o el secure
      // storage al guardar la sesion): mensaje generico sin filtrar detalles y
      // el usuario puede reintentar. El `_busy` se libera SIEMPRE en finally.
      _errorMessage = genericAuthErrorMessage;
    } finally {
      _busy = false;
      notifyListeners();
    }
    return ok;
  }

  /// PIN de contingencia directo (siempre visible como alternativa).
  ///
  /// El PIN viaja solo en el cuerpo del POST y nunca se loguea (docs/16
  /// reglas 7 y 10). Un PIN mal tambien produce el mensaje GENERICO.
  Future<bool> loginWithPin({
    required String userRef,
    required String deviceId,
    required String pin,
  }) async {
    if (_busy) return false;
    _busy = true;
    _errorMessage = null;
    _infoMessage = null;
    notifyListeners();

    var ok = false;
    try {
      if (pin.isEmpty) {
        _errorMessage = genericAuthErrorMessage;
      } else {
        // Binding best-effort: la clave PUBLICA del dispositivo + plataforma +
        // tipo biometrico (si se conoce). El secreto nunca sale del
        // almacenamiento seguro ni se loguea (docs/16 reglas 7 y 10).
        final data = <String, dynamic>{
          'user_ref': userRef,
          'device_id': deviceId,
          'pin': pin,
          'device_public_key': await _session.getOrCreateDeviceBindingKey(),
        };
        final platform = _effectivePlatform();
        if (platform != null) {
          data['platform'] = platform;
        }
        if (_biometricType != null && _biometricType.isNotEmpty) {
          data['biometric_type'] = _biometricType;
        }
        final res = await _api.post(pinPath, data: data);
        ok = await _saveSession(_dataOf(res.data));
        if (ok) {
          _succeeded = true;
          stopInactivityTimer();
          await _rememberUser(userRef);
        } else {
          _errorMessage = genericAuthErrorMessage;
        }
      }
    } on ApiException {
      _errorMessage = genericAuthErrorMessage;
    } catch (_) {
      // Fallo al derivar la clave del dispositivo (secreto corrupto) o del
      // secure storage: NO rompe el login de forma opaca; se trata como error
      // generico (sin filtrar) y se permite reintentar. `_busy` se libera
      // SIEMPRE en finally (hallazgo del validador).
      _errorMessage = genericAuthErrorMessage;
    } finally {
      _busy = false;
      notifyListeners();
    }
    return ok;
  }

  /// Arma el temporizador de inactividad. La pagina lo llama en `initState`
  /// y muestra [remainingSeconds]; cualquier interaccion llama a
  /// [notifyActivity] para reiniciarlo.
  ///
  /// [autoTick] en `false` no crea el `Timer` real (seam para tests: se
  /// avanza con [tick]). Al expirar limpia la sesion y llama a [onExpired]
  /// (la pagina navega a `/login`).
  void startInactivityTimer({
    Future<void> Function()? onExpired,
    bool autoTick = true,
  }) {
    stopInactivityTimer();
    _onExpired = onExpired;
    _remainingSeconds = inactivityTimeoutSeconds;
    if (autoTick) {
      _timer = Timer.periodic(const Duration(seconds: 1), (_) => tick());
    }
    notifyListeners();
  }

  void stopInactivityTimer() {
    _timer?.cancel();
    _timer = null;
  }

  /// Avanza un segundo el contador. Al llegar a cero marca [expired], limpia
  /// la sesion (best-effort) y avisa via el callback de
  /// [startInactivityTimer]. La limpieza es asincrona: en tests, esperar un
  /// `pump()`/`Future.delayed` tras el ultimo `tick()`.
  void tick() {
    if (_expired || _remainingSeconds <= 0) return;
    _remainingSeconds--;
    if (_remainingSeconds == 0) {
      _expired = true;
      _errorMessage = sessionExpiredMessage;
      stopInactivityTimer();
      notifyListeners();
      unawaited(_session.clearOnLogout().then((_) => _onExpired?.call()));
      return;
    }
    notifyListeners();
  }

  /// Reinicia el contador por actividad del usuario (toque o escritura).
  void notifyActivity() {
    if (_expired || _succeeded) return;
    if (_remainingSeconds != inactivityTimeoutSeconds) {
      _remainingSeconds = inactivityTimeoutSeconds;
      notifyListeners();
    }
  }

  Future<bool> _saveSession(Map<String, dynamic>? data) async {
    final access = data?['access_token'] as String?;
    if (access == null || access.isEmpty) return false;
    final refresh = data?['refresh_token'] as String?;
    await _session.saveSession(accessToken: access, refreshToken: refresh);
    return true;
  }

  /// Persiste el ultimo `user_ref` (F-T20) tras exito facial o PIN.
  ///
  /// Best-effort: un fallo del store no tumba el login ya concedido. El
  /// `user_ref` nunca se loguea (docs/16 reglas 7 y 10).
  Future<void> _rememberUser(String userRef) async {
    if (userRef.isEmpty) return;
    final store = _identity ?? sessionIdentityStoreFactory?.call();
    if (store == null) return;
    try {
      await store.saveUserRef(userRef);
    } catch (_) {
      // Silencio deliberado: no se registra el `user_ref` ni el error.
    }
  }

  /// `android`/`ios` segun el SO; `null` en plataformas no moviles.
  String? _effectivePlatform() {
    final explicit = _platform;
    if (explicit != null && explicit.isNotEmpty) return explicit;
    switch (defaultTargetPlatform) {
      case TargetPlatform.android:
        return 'android';
      case TargetPlatform.iOS:
        return 'ios';
      default:
        return null;
    }
  }

  /// Extrae el `data` del envelope docs/05 `{data, meta}`.
  static Map<String, dynamic> _dataOf(Object? body) {
    if (body is Map && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    throw const FormatException('Envelope de login sin campo data');
  }

  @override
  void dispose() {
    stopInactivityTimer();
    super.dispose();
  }
}
