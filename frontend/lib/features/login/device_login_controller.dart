// Controlador delgado del flujo "iniciar sesion en este dispositivo" (F-T56).
//
// Tres pasos sobre `/login/device` (solo cuando NO hay `userRef`):
//  1. email + DNI/RUC -> `POST /auth/login/device/request` (E1-T45); con
//     cualquier 200 avanza al paso 2 sin revelar si la cuenta existe.
//  2. OTP de 6 digitos recibido por email; se conserva en memoria hasta el
//     paso final (sin llamada al backend).
//  3. PIN de 4-6 digitos -> `POST /auth/login/device/complete` (E1-T46) con
//     OTP + PIN de forma atomica + `device_id` (`identity`
//     `.getOrCreateDeviceId()`), clave PUBLICA de binding
//     (`session.getOrCreateDeviceBindingKey()`), `platform` (patron
//     `login_controller._effectivePlatform`) y `biometric_type` si se conoce.
//     Al exito: `session.saveSession(...)`, `identity.saveUserRef(userRef)`,
//     sincroniza `biometricEnabled` (patron F-T49) y marca `succeeded` (la
//     pagina deja que la guarda navegue a `/home`; no navega a mano).
//
// Cliente delgado (docs/19#4): el backend es la autoridad (existencia de
// cuenta, OTP, PIN, binding). Validaciones locales SOLO de formato con
// mensajes de formato por campo; cualquier fallo de RED/BACKEND (paso 1 o 3:
// `INVALID_LOGIN`, `ACCOUNT_LOCKED`, `RATE_LIMITED`, `VALIDATION_ERROR`,
// `NETWORK_ERROR`/`TIMEOUT_ERROR`) usa un UNICO mensaje generico que no
// revela la causa ni si la cuenta existe (docs/16 reglas 7 y 9).
//
// Nunca se loguea email, documento, OTP, PIN, tokens, `user_ref` ni
// `device_public_key`; nada de PII en rutas.
// ignore_for_file: prefer_initializing_formals
// Razon: los parametros del constructor son API publica y no pueden ser
// formales inicializadores de campos privados.
library;

import 'package:flutter/foundation.dart';

import '../../core/errors/api_exception.dart';
import '../../core/session/session_identity_store.dart';
import '../../core/session/session_repository.dart';
import 'device_login_service.dart';

/// Controlador de 3 pasos del login en este dispositivo (F-T56).
class DeviceLoginController extends ChangeNotifier {
  DeviceLoginController({
    required DeviceLoginService service,
    required SessionRepository session,
    SessionIdentityStore? identity,
    String? platform,
    String? biometricType,
  })  : _service = service,
        _session = session,
        _identity = identity,
        _platform = platform,
        _biometricType = biometricType;

  final DeviceLoginService _service;
  final SessionRepository _session;

  /// Store F-T20 (`userRef`/`device_id`; `null` = se usa
  /// [sessionIdentityStoreFactory] o se omite el guardado best-effort).
  /// Tambien es la fuente del flag `biometric_enabled` (F-T49, E1-T39).
  final SessionIdentityStore? _identity;

  /// Plataforma explicita (`android`/`ios`); `null` = resolver del sistema.
  final String? _platform;

  /// Tipo biometrico (`FACE`/`FINGERPRINT`) si se conoce; `null` = omitir.
  final String? _biometricType;

  /// Tipos aceptados por el backend para `biometric_type`.
  static const String biometricTypeFace = 'FACE';
  static const String biometricTypeFingerprint = 'FINGERPRINT';

  /// Mensaje UNICO para cualquier fallo de red/backend del paso 1 o del
  /// paso 3: no filtra la causa ni si la cuenta existe (docs/16 reglas 7/9).
  static const String genericDeviceLoginErrorMessage =
      'No pudimos verificar tu identidad. Revisa tus datos e inténtalo de nuevo.';

  /// Aviso neutro tras el paso 1 (identico exista o no la cuenta).
  static const String neutralRequestMessage =
      'Si los datos pertenecen a una cuenta, recibirás un código por correo.';

  /// Errores de FORMATO local (no revelan nada del backend).
  static const String invalidEmailMessage =
      'Ingresa un correo válido para continuar.';
  static const String invalidDniMessage = 'Ingresa los 8 dígitos de tu DNI.';
  static const String invalidRucMessage = 'Ingresa los 11 dígitos de tu RUC.';
  static const String invalidCodeMessage =
      'Ingresa los 6 dígitos del código.';
  static const String invalidPinMessage =
      'Ingresa un PIN de 4 a 6 dígitos.';

  /// Dispositivo no preparado (sin `SessionIdentityStore`/`device_id`):
  /// neutro y accionable; se muestra sin llamar al backend.
  static const String invalidDeviceMessage =
      'No pudimos preparar este dispositivo. Reinténtalo.';

  int _step = 1;
  bool _busy = false;
  bool _succeeded = false;
  bool? _biometricEnabled;
  String? _errorMessage;
  String? _infoMessage;
  String _email = '';
  String _docType = 'DNI';
  String _documentNumber = '';
  String _code = '';
  DeviceLoginRequestResult? _lastRequest;
  bool _disposed = false;

  /// Paso visible actual (1..3).
  int get step => _step;

  /// `true` durante `request`/`complete`.
  bool get busy => _busy;

  /// `true` tras un `complete` exitoso con sesion guardada (la pagina deja
  /// que la guarda navegue a `/home`).
  bool get succeeded => _succeeded;

  /// Consentimiento biometrico sincronizado tras el exito (F-T49, E1-T39).
  bool? get biometricEnabled => _biometricEnabled;

  /// Error visible actual (`null` = sin error). Los fallos de backend/red
  /// usan siempre [genericDeviceLoginErrorMessage].
  String? get errorMessage => _errorMessage;

  /// Aviso informativo neutro (p. ej. tras el paso 1).
  String? get infoMessage => _infoMessage;

  /// Ultimo resultado de `request` (ttl/cooldown informativos).
  DeviceLoginRequestResult? get lastRequest => _lastRequest;

  /// Paso 1: pide el OTP `LOGIN` y avanza al paso 2 con cualquier 200.
  ///
  /// Retorna `true` y avanza a `step == 2` en exito. Con error de formato
  /// muestra el mensaje de formato; con error de backend/red muestra el
  /// mensaje generico unico (sin revelar si la cuenta existe).
  Future<bool> submitIdentity({
    required String email,
    required String docType,
    required String documentNumber,
  }) async {
    if (_busy || _succeeded) return false;
    final cleanEmail = email.trim();
    if (!isDeviceLoginEmailValid(cleanEmail)) {
      _errorMessage = invalidEmailMessage;
      _infoMessage = null;
      notifyListeners();
      return false;
    }
    final cleanType = docType.trim().toUpperCase() == 'RUC' ? 'RUC' : 'DNI';
    final cleanDoc = documentNumber.trim();
    if (!isDeviceLoginDocValid(cleanDoc, docType: cleanType)) {
      _errorMessage =
          cleanType == 'RUC' ? invalidRucMessage : invalidDniMessage;
      _infoMessage = null;
      notifyListeners();
      return false;
    }
    _busy = true;
    _errorMessage = null;
    _infoMessage = null;
    notifyListeners();
    try {
      _lastRequest = await _service.requestOtp(
        email: cleanEmail,
        docType: cleanType,
        documentNumber: cleanDoc,
      );
      _email = cleanEmail;
      _docType = cleanType;
      _documentNumber = cleanDoc;
      _step = 2;
      _infoMessage = neutralRequestMessage;
      return true;
    } on ApiException {
      _errorMessage = genericDeviceLoginErrorMessage;
      return false;
    } catch (_) {
      _errorMessage = genericDeviceLoginErrorMessage;
      return false;
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  /// Paso 2: captura el OTP (6 digitos) SIN llamar al backend y avanza al
  /// paso 3. El `complete` del paso 3 valida OTP + PIN de forma atomica
  /// (E1-T46).
  Future<bool> submitCode(String code) async {
    if (_busy || _succeeded) return false;
    if (_step != 2) return false;
    final clean = code.trim();
    if (!isDeviceLoginCodeValid(clean)) {
      _errorMessage = invalidCodeMessage;
      notifyListeners();
      return false;
    }
    _code = clean;
    _errorMessage = null;
    _step = 3;
    notifyListeners();
    return true;
  }

  /// Paso 3: valida OTP + PIN de forma atomica (`complete`), asocia el
  /// dispositivo y abre sesion.
  ///
  /// Al exito guarda la sesion (tokens), persiste `userRef` best-effort,
  /// sincroniza `biometricEnabled` best-effort y marca [succeeded]. Con
  /// cualquier fallo de backend/red muestra el mensaje generico unico (el
  /// MISMO que el paso 1: no revela que fallo).
  Future<bool> submitPin(String pin) async {
    if (_busy || _succeeded) return false;
    if (_step != 3) return false;
    final cleanPin = pin.trim();
    if (!isDeviceLoginPinValid(cleanPin)) {
      _errorMessage = invalidPinMessage;
      notifyListeners();
      return false;
    }
    _busy = true;
    _errorMessage = null;
    notifyListeners();
    try {
      final identity = _identity ?? sessionIdentityStoreFactory?.call();
      final deviceId = identity == null
          ? ''
          : await identity.getOrCreateDeviceId();
      if (deviceId.trim().isEmpty) {
        _errorMessage = invalidDeviceMessage;
        return false;
      }
      // Binding best-effort: solo la clave PUBLICA viaja al backend; el
      // secreto nunca sale del almacenamiento seguro ni se loguea (docs/16
      // reglas 7 y 10). Patron `login_controller.loginWithPin`.
      final devicePublicKey = await _session.getOrCreateDeviceBindingKey();
      final result = await _service.complete(
        email: _email,
        docType: _docType,
        documentNumber: _documentNumber,
        code: _code,
        pin: cleanPin,
        deviceId: deviceId.trim(),
        devicePublicKey: devicePublicKey,
        platform: _effectivePlatform(),
        biometricType: _normalizedBiometricType(),
      );
      final access = result.accessToken;
      if (access.isEmpty) {
        _errorMessage = genericDeviceLoginErrorMessage;
        return false;
      }
      await _session.saveSession(
        accessToken: access,
        refreshToken: result.refreshToken,
      );
      await _rememberUserRef(result.userRef);
      await _syncBiometricEnabled(result.biometricEnabled);
      _succeeded = true;
      _infoMessage = null;
      return true;
    } on ApiException {
      _errorMessage = genericDeviceLoginErrorMessage;
      return false;
    } catch (_) {
      // Fallo al derivar la clave del dispositivo, del secure storage o del
      // `complete`: mensaje generico sin filtrar detalles; se permite
      // reintentar. `_busy` se libera SIEMPRE en finally.
      _errorMessage = genericDeviceLoginErrorMessage;
      return false;
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  /// Reintento tras un error (limpia el error visible y vuelve al paso
  /// actual con lo capturado intacto).
  void retry() {
    if (_errorMessage != null) {
      _errorMessage = null;
      notifyListeners();
    }
  }

  /// Vuelve al paso anterior (conserva lo capturado; limpia el error).
  void back() {
    if (_busy || _succeeded) return;
    if (_step > 1) {
      _step--;
      _errorMessage = null;
      notifyListeners();
    }
  }

  /// Reinicia el flujo al paso 1 (conserva la sesion concedida si ya hubo
  /// exito: no borra nada persistido).
  void reset() {
    if (_busy) return;
    _step = 1;
    _errorMessage = null;
    _infoMessage = null;
    _code = '';
    notifyListeners();
  }

  /// Persiste el `user_ref` devuelto (F-T20) tras el exito.
  ///
  /// Best-effort: un fallo del store no tumba la sesion ya concedida. El
  /// `user_ref` nunca se loguea (docs/16 reglas 7 y 10).
  Future<void> _rememberUserRef(String userRef) async {
    if (userRef.isEmpty) return;
    final store = _identity ?? sessionIdentityStoreFactory?.call();
    if (store == null) return;
    try {
      await store.saveUserRef(userRef);
    } catch (_) {
      // Silencio deliberado: no se registra el `user_ref` ni el error.
    }
  }

  /// Sincroniza la cache local de `biometric_enabled` (F-T49, E1-T39).
  ///
  /// Solo acepta `bool` (`null`/ausente/otro tipo -> no escribe).
  /// Best-effort como `_rememberUserRef`: un fallo del store no tumba la
  /// sesion ya concedida.
  Future<void> _syncBiometricEnabled(Object? value) async {
    if (value is! bool) return;
    _biometricEnabled = value;
    final store = _identity ?? sessionIdentityStoreFactory?.call();
    if (store == null) return;
    try {
      await store.saveBiometricEnabled(value);
    } catch (_) {
      // Silencio deliberado: la sesion ya fue concedida.
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

  /// `FACE`/`FINGERPRINT` si se conoce; `null` = omitir en el `complete`.
  String? _normalizedBiometricType() {
    final value = _biometricType;
    if (value == null || value.isEmpty) return null;
    return value;
  }

  @override
  void notifyListeners() {
    if (_disposed) return;
    super.notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}

/// El email debe tener forma de correo (no decide existencia).
bool isDeviceLoginEmailValid(String email) {
  final clean = email.trim();
  if (clean.isEmpty || clean.length > 320) return false;
  return RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$').hasMatch(clean);
}

/// El documento son solo digitos con longitud exacta: DNI 8 / RUC 11
/// (paridad visual con KYC/pin-reset; el backend decide).
bool isDeviceLoginDocValid(String docNumber, {String docType = 'DNI'}) {
  final clean = docNumber.trim();
  final expected = docType.toUpperCase() == 'RUC' ? 11 : 8;
  if (clean.length != expected) return false;
  return int.tryParse(clean) != null;
}

/// El OTP son 6 digitos (el backend decide si es valido/vigente).
bool isDeviceLoginCodeValid(String code) {
  final clean = code.trim();
  if (clean.length != 6) return false;
  return int.tryParse(clean) != null;
}

/// El PIN son 4-6 digitos (el backend decide si es valido).
bool isDeviceLoginPinValid(String pin) {
  final clean = pin.trim();
  if (clean.length < 4 || clean.length > 6) return false;
  return int.tryParse(clean) != null;
}
