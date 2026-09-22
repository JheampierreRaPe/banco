// Controladores delgados de recuperación de acceso (F-T29, HU04).
//
// - [RecoveryEmailController]: valida el formato del email en la UI (no es
//   lógica de negocio), llama a `request` y expone el mensaje NEUTRO único.
// - [RecoveryOtpController]: código de 6 dígitos, cuenta atrás de vigencia,
//   reenvío con cooldown y `verify` con binding del dispositivo nuevo. Al
//   éxito guarda el `user_ref` devuelto (`SessionIdentityStore`, F-T20,
//   best-effort) y expone [RecoveryOtpController.verifiedUserRef] para que la
//   página navegue a `/login?userRef=` (E1-T33/SCR-005: verify YA NO abre
//   sesión; la única sesión la abre `POST /auth/login/pin`).
//
// Anti-oráculo: el MISMO mensaje y la MISMA navegación exista o no el email;
// `401 INVALID_RECOVERY_CODE` es el ÚNICO error de código (cubre incorrecto,
// vencido y agotados) con un único mensaje genérico. Nunca se loguea email,
// OTP, tokens, `user_ref` ni `device_id` (docs/16 reglas 7 y 9).
// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública y no pueden ser
// formales inicializadores de campos privados.

import 'dart:async';

import 'package:flutter/foundation.dart';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/core/session/session_repository.dart';

import 'recovery_service.dart';

/// Valida el formato del email en la UI (no decide existencia: eso lo hace
/// el servidor).
bool isRecoveryEmailValid(String email) {
  final clean = email.trim();
  // Límite alineado al contrato E1-T31 (`max_length=320` en
  // `backend/.../schemas/recovery.py`): no ser más estricto que el backend.
  if (clean.isEmpty || clean.length > 320) return false;
  return RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$').hasMatch(clean);
}

/// Formatea segundos a `MM:SS` para los contadores visibles.
String formatRecoveryCountdown(int totalSeconds) {
  final clamped = totalSeconds < 0 ? 0 : totalSeconds;
  final minutes = clamped ~/ 60;
  final seconds = clamped % 60;
  return '${minutes.toString().padLeft(2, '0')}:'
      '${seconds.toString().padLeft(2, '0')}';
}

/// Estados visibles de la pantalla de email.
enum RecoveryEmailStatus { idle, submitting, success, error }

/// Pantalla `/recovery`: captura el email y pide el OTP.
class RecoveryEmailController extends ChangeNotifier {
  RecoveryEmailController({required RecoveryService service})
      : _service = service;

  final RecoveryService _service;

  /// Mensaje NEUTRO único tras `request` (idéntico exista o no el email).
  static const String neutralMessage =
      'Si el correo está registrado, recibirás un código.';

  /// Error de formato (validación UI, no de negocio).
  static const String invalidEmailMessage =
      'Ingresa un correo válido para continuar.';

  RecoveryEmailStatus _status = RecoveryEmailStatus.idle;
  String? _errorMessage;
  RecoveryRequestResult? _lastResult;
  bool _disposed = false;

  RecoveryEmailStatus get status => _status;
  String? get errorMessage => _errorMessage;
  RecoveryRequestResult? get lastResult => _lastResult;
  bool get isBusy => _status == RecoveryEmailStatus.submitting;
  bool get succeeded => _status == RecoveryEmailStatus.success;

  /// Envía el email a `POST /auth/recovery/request`. Con 200 retorna `true`
  /// (la página navega a `/recovery/otp` con el mensaje neutro).
  Future<bool> submit(String email) async {
    if (isBusy) return false;
    final clean = email.trim();
    if (!isRecoveryEmailValid(clean)) {
      _status = RecoveryEmailStatus.error;
      _errorMessage = invalidEmailMessage;
      notifyListeners();
      return false;
    }
    _status = RecoveryEmailStatus.submitting;
    _errorMessage = null;
    notifyListeners();
    try {
      _lastResult = await _service.request(email: clean);
      _status = RecoveryEmailStatus.success;
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      // Red / 429 / 422: mensaje accionable de la capa HTTP (F-T01).
      _status = RecoveryEmailStatus.error;
      _errorMessage = e.message;
      notifyListeners();
      return false;
    }
  }

  /// Reintento tras un error de red (limpia el error visible).
  void retry() {
    if (_status == RecoveryEmailStatus.error) {
      _status = RecoveryEmailStatus.idle;
      _errorMessage = null;
      notifyListeners();
    }
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

/// Estados visibles de la pantalla de OTP.
enum RecoveryOtpStatus { idle, submitting, success, error }

/// Pantalla `/recovery/otp`: ingresa el código y continúa al login.
///
/// Los contadores avanzan con [tick] (1 s). La página usa [startAutoTick]
/// (Timer real); los tests llaman a [tick] manualmente (determinista).
///
/// E1-T33/SCR-005: el éxito de `verify` NO abre sesión; persiste el
/// `user_ref` devuelto y la página navega a `/login?userRef=<user_ref>`.
class RecoveryOtpController extends ChangeNotifier {
  RecoveryOtpController({
    required RecoveryService service,
    required SessionRepository session,
    required String email,
    this.otpValiditySeconds = 600,
    this.resendWaitSeconds = 30,
    SessionIdentityStore? identity,
    String? platform,
  })  : _service = service,
        _session = session,
        _email = email,
        _identity = identity,
        _platform = platform {
    _remainingSeconds = otpValiditySeconds;
    _resendCooldownSeconds = 0;
  }

  final RecoveryService _service;
  final SessionRepository _session;

  /// Email al que se envió el código (va como `email` al backend).
  final String _email;

  /// Store F-T20 para el `device_id` estable y el `user_ref` tras el éxito.
  final SessionIdentityStore? _identity;

  /// Plataforma explícita (`android`/`ios`); `null` = resolver del sistema.
  final String? _platform;

  /// Vigencia del OTP en segundos (viene de `ttl_seconds` del `request`).
  final int otpValiditySeconds;

  /// Espera mínima entre reenvíos (`resend_wait_seconds` del `request`).
  final int resendWaitSeconds;

  /// Mensaje NEUTRO único (idéntico exista o no el email).
  static const String neutralMessage =
      'Si el correo está registrado, recibirás un código.';

  /// Mensaje ÚNICO y genérico para `401 INVALID_RECOVERY_CODE` (cubre código
  /// incorrecto, OTP vencido e intentos agotados, sin distinguir).
  static const String invalidCodeMessage =
      'El código no es correcto o venció. Revisa el mensaje que te enviamos '
      'o pide uno nuevo.';

  /// Mensaje de espera para `429 RATE_LIMITED`.
  static const String rateMessage =
      'Demasiados intentos. Espera un momento e inténtalo de nuevo.';

  static const String resentMessage =
      'Te enviamos un nuevo código. Revisa tu correo.';

  RecoveryOtpStatus _status = RecoveryOtpStatus.idle;
  String? _errorMessage;
  String _code = '';
  bool _isResending = false;
  int _remainingSeconds = 0;
  int _resendCooldownSeconds = 0;
  Timer? _timer;
  bool _disposed = false;

  /// `user_ref` devuelto por el último `verify` exitoso (`null` si aún no
  /// hay éxito). La página lo usa para navegar a `/login?userRef=`.
  String? _verifiedUserRef;

  RecoveryOtpStatus get status => _status;
  String? get errorMessage => _errorMessage;

  /// `user_ref` del último `verify` exitoso; `null` antes del éxito.
  String? get verifiedUserRef => _verifiedUserRef;

  /// Aviso informativo neutro (siempre el mismo, sin filtrar existencia).
  String get infoMessage => neutralMessage;
  String get code => _code;
  bool get isResending => _isResending;
  int get remainingSeconds => _remainingSeconds;
  int get resendCooldownSeconds => _resendCooldownSeconds;
  bool get isBusy =>
      _status == RecoveryOtpStatus.submitting || _isResending;
  bool get succeeded => _status == RecoveryOtpStatus.success;

  /// Submit habilitado solo con el código completo y sin operación en curso.
  bool get canSubmit =>
      _code.length == 6 && !isBusy && !succeeded;

  /// Reenvío habilitado solo tras el cooldown y sin operación en curso.
  bool get canResend =>
      _resendCooldownSeconds <= 0 && !_isResending && !succeeded;

  /// Inicia el descuento automático (1 s). La página lo llama en `initState`.
  void startAutoTick() {
    _timer?.cancel();
    _timer = Timer.periodic(const Duration(seconds: 1), (_) => tick());
  }

  void stopTick() {
    _timer?.cancel();
    _timer = null;
  }

  /// Avanza un segundo ambos contadores (la vigencia solo informa; el error
  /// de vencido lo decide el backend con `INVALID_RECOVERY_CODE`).
  void tick() {
    if (succeeded) return;
    var changed = false;
    if (_remainingSeconds > 0) {
      _remainingSeconds--;
      changed = true;
    }
    if (_resendCooldownSeconds > 0) {
      _resendCooldownSeconds--;
      changed = true;
    }
    if (changed) notifyListeners();
  }

  void setCode(String code) {
    if (_code != code) {
      _code = code;
      if (_status == RecoveryOtpStatus.error) {
        _status = RecoveryOtpStatus.idle;
        _errorMessage = null;
      }
      notifyListeners();
    }
  }

  /// Verifica el código y persiste el `user_ref` devuelto (E1-T33/SCR-005).
  ///
  /// Al éxito retorna `true` para que la página navegue a
  /// `/login?userRef=<user_ref>`; aquí NO se abre sesión ni se guardan
  /// tokens (la única sesión la abre `POST /auth/login/pin`).
  Future<bool> submit(String code) async {
    if (isBusy || succeeded) return false;
    final clean = code.trim();
    if (clean.length != 6 || int.tryParse(clean) == null) {
      _status = RecoveryOtpStatus.error;
      _errorMessage = 'Ingresa los 6 dígitos del código.';
      notifyListeners();
      return false;
    }
    _status = RecoveryOtpStatus.submitting;
    _errorMessage = null;
    notifyListeners();
    try {
      // Binding del dispositivo nuevo (F-T20/F-T22, best-effort): el secreto
      // nunca sale del almacenamiento seguro ni se loguea. La sesión NO se
      // toca aquí (E1-T33): solo se necesita la clave pública de binding.
      final identity =
          _identity ?? sessionIdentityStoreFactory?.call();
      final deviceId = await identity?.getOrCreateDeviceId();
      final devicePublicKey =
          await _session.getOrCreateDeviceBindingKey();
      final result = await _service.verify(
        email: _email.trim(),
        code: clean,
        deviceId: deviceId,
        devicePublicKey: devicePublicKey,
        platform: _effectivePlatform(),
      );
      // Best-effort como en F-T21/F-T22: si falla, no bloquea la
      // continuación al login ya concedida (silencio deliberado: nunca se
      // loguea el `user_ref`).
      await _rememberUserRef(identity, result.userRef);
      _verifiedUserRef = result.userRef;
      _status = RecoveryOtpStatus.success;
      stopTick();
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _applyError(e);
      notifyListeners();
      return false;
    } catch (_) {
      // Fallo del secure storage (binding o `user_ref`): mensaje accionable
      // con reintento, sin filtrar detalles.
      _status = RecoveryOtpStatus.error;
      _errorMessage =
          'No pudimos completar la recuperación. Inténtalo de nuevo.';
      notifyListeners();
      return false;
    }
  }

  /// Reenvía el código (`request` de nuevo) y reinicia la vigencia.
  Future<bool> resend() async {
    if (!canResend) return false;
    _isResending = true;
    _errorMessage = null;
    notifyListeners();
    try {
      final result = await _service.request(email: _email.trim());
      _isResending = false;
      _remainingSeconds =
          result.ttlSeconds.clamp(1, otpValiditySeconds);
      // El cooldown usa el valor RECIÉN devuelto por el backend; fallback al
      // del constructor/query solo si el backend trae 0/no positivo.
      _resendCooldownSeconds = result.resendWaitSeconds > 0
          ? result.resendWaitSeconds
          : resendWaitSeconds;
      _status = RecoveryOtpStatus.idle;
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _isResending = false;
      _applyError(e);
      notifyListeners();
      return false;
    } catch (_) {
      // Error inesperado (no ApiException): limpiar el estado de reenvío
      // (sin spinner fijo) y mensaje genérico, sin filtrar detalles ni PII.
      _isResending = false;
      _errorMessage =
          'No pudimos completar la recuperación. Inténtalo de nuevo.';
      notifyListeners();
      return false;
    }
  }

  void _applyError(ApiException e) {
    switch (e.code) {
      case 'INVALID_RECOVERY_CODE':
        // Único error genérico de código: mismo mensaje sin distinguir
        // incorrecto / vencido / agotados / email sin OTP.
        if (_status == RecoveryOtpStatus.submitting) {
          _status = RecoveryOtpStatus.error;
        }
        _errorMessage = invalidCodeMessage;
      case 'RATE_LIMITED':
        if (_status == RecoveryOtpStatus.submitting) {
          _status = RecoveryOtpStatus.error;
        }
        _errorMessage = rateMessage;
      default:
        // Red / servidor / 422: mensaje en español de la capa HTTP (F-T01).
        if (_status == RecoveryOtpStatus.submitting) {
          _status = RecoveryOtpStatus.error;
        }
        _errorMessage = e.message;
    }
  }

  Future<void> _rememberUserRef(
    SessionIdentityStore? identity,
    String userRef,
  ) async {
    if (identity == null || userRef.isEmpty) return;
    try {
      await identity.saveUserRef(userRef);
    } catch (_) {
      // Silencio deliberado: no se registra el `user_ref` ni el error.
    }
  }

  /// `android`/`ios` según el SO; `null` en plataformas no móviles.
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

  @override
  void notifyListeners() {
    if (_disposed) return;
    super.notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    stopTick();
    super.dispose();
  }
}
