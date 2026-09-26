// Controladores delgados del restablecimiento de PIN (F-T43, HU02/HU04).
//
// - [PinResetIdentityController]: captura email + DNI y pide el OTP
//   `RECOVERY` (`POST /auth/recovery/request`); expone el mensaje NEUTRO
//   único (idéntico exista o no el email).
// - [PinResetOtpController]: código de 6 dígitos, cuenta atrás de vigencia
//   (`ttl_seconds`) y reenvío con cooldown (`resend_wait_seconds`). El OTP
//   se consume recién en el request final (sin `verify` intermedio).
// - [PinResetConfirmController]: fija el PIN (`POST /auth/pin-reset`) y al
//   éxito expone [resetUserRef] para navegar a `/login?userRef=` (sin abrir
//   sesión; la única sesión la abre `POST /auth/login/pin`).
//
// Anti-enumeración: `401 INVALID_PIN_RESET` es el ÚNICO error de
// email/DNI/OTP con un único mensaje genérico (nunca revela el campo).
// Nunca se loguea email, DNI, OTP, PIN ni `user_ref` (docs/16 reglas 7 y 9).
// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública y no pueden ser
// formales inicializadores de campos privados.

import 'dart:async';

import 'package:flutter/foundation.dart';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/session/session_identity_store.dart';

import 'pin_reset_service.dart';
import 'pin_reset_validators.dart';

/// Formatea segundos a `MM:SS` para los contadores visibles.
String formatPinResetCountdown(int totalSeconds) {
  final clamped = totalSeconds < 0 ? 0 : totalSeconds;
  final minutes = clamped ~/ 60;
  final seconds = clamped % 60;
  return '${minutes.toString().padLeft(2, '0')}:'
      '${seconds.toString().padLeft(2, '0')}';
}

/// Borrador del flujo en memoria (viaja por `extra`, nunca en la ruta ni
/// en logs): el DNI/OTP/PIN jamás aparecen en la URL.
class PinResetDraft {
  const PinResetDraft({
    required this.email,
    required this.docNumber,
    this.docType = 'DNI',
    this.code = '',
    this.pin = '',
  });

  /// Email capturado en el paso inicial.
  final String email;

  /// DNI capturado en el paso inicial (solo memoria, enmascarado en UI).
  final String docNumber;

  /// Tipo de documento (F-T50, `DNI|RUC`; default `DNI` preserva F-T43).
  /// Viaja solo en memoria (`extra`), nunca en la ruta ni en logs.
  final String docType;

  /// OTP `RECOVERY` capturado en el paso OTP.
  final String code;

  /// PIN nuevo capturado en el paso de creación.
  final String pin;

  PinResetDraft copyWith({String? code, String? pin, String? docType}) =>
      PinResetDraft(
        email: email,
        docNumber: docNumber,
        docType: docType ?? this.docType,
        code: code ?? this.code,
        pin: pin ?? this.pin,
      );
}

/// Estados visibles del paso inicial (email + DNI).
enum PinResetIdentityStatus { idle, submitting, success, error }

/// Paso `/pin-reset`: captura email + DNI y emite el OTP `RECOVERY`.
class PinResetIdentityController extends ChangeNotifier {
  PinResetIdentityController({required PinResetService service})
      : _service = service;

  final PinResetService _service;

  /// Mensaje NEUTRO único tras `requestOtp` (idéntico exista o no el email).
  static const String neutralMessage =
      'Si el correo está registrado, recibirás un código.';

  PinResetIdentityStatus _status = PinResetIdentityStatus.idle;
  String? _errorMessage;
  PinResetRequestResult? _lastResult;
  bool _disposed = false;

  PinResetIdentityStatus get status => _status;
  String? get errorMessage => _errorMessage;
  PinResetRequestResult? get lastResult => _lastResult;
  bool get isBusy => _status == PinResetIdentityStatus.submitting;
  bool get succeeded => _status == PinResetIdentityStatus.success;

  /// Envía el email a `POST /auth/recovery/request`. El DNI NO viaja aquí:
  /// se captura y se envía solo en el request final de `pin-reset`.
  /// Con 200 retorna `true` (la página navega a `/pin-reset/otp`).
  Future<bool> submit({
    required String email,
    required String docNumber,
    String docType = 'DNI',
  }) async {
    if (isBusy) return false;
    final cleanEmail = email.trim();
    if (!isPinResetEmailValid(cleanEmail)) {
      _status = PinResetIdentityStatus.error;
      _errorMessage = pinResetInvalidEmailMessage;
      notifyListeners();
      return false;
    }
    final cleanType =
        docType.trim().toUpperCase() == 'RUC' ? 'RUC' : 'DNI';
    final cleanDoc = docNumber.trim();
    if (!isDocNumberValid(cleanDoc, docType: cleanType)) {
      _status = PinResetIdentityStatus.error;
      _errorMessage = pinResetInvalidDocMessageFor(cleanType);
      notifyListeners();
      return false;
    }
    _status = PinResetIdentityStatus.submitting;
    _errorMessage = null;
    notifyListeners();
    try {
      _lastResult = await _service.requestOtp(email: cleanEmail);
      _status = PinResetIdentityStatus.success;
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      // Red / 429 / 422: mensaje accionable de la capa HTTP (F-T01).
      _status = PinResetIdentityStatus.error;
      _errorMessage = _requestError(e);
      notifyListeners();
      return false;
    }
  }

  /// Reintento tras un error (limpia el error visible y vuelve al
  /// formulario).
  void retry() {
    if (_status == PinResetIdentityStatus.error) {
      _status = PinResetIdentityStatus.idle;
      _errorMessage = null;
      notifyListeners();
    }
  }

  String _requestError(ApiException e) {
    switch (e.code) {
      case 'RATE_LIMITED':
        return PinResetOtpController.rateMessage;
      default:
        return e.message;
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

/// Estados visibles del paso OTP.
enum PinResetOtpStatus { idle, submitting, success, error }

/// Paso `/pin-reset/otp`: ingresa el código `RECOVERY` y continúa a crear
/// el PIN nuevo.
///
/// Los contadores avanzan con [tick] (1 s). La página usa [startAutoTick]
/// (Timer real); los tests llaman a [tick] manualmente (determinista).
/// El reenvío reutiliza el `PENDING` vigente con cooldown
/// (`POST /auth/recovery/request`, nunca `/auth/otp/resend`).
class PinResetOtpController extends ChangeNotifier {
  PinResetOtpController({
    required PinResetService service,
    required String email,
    this.otpValiditySeconds = 600,
    this.resendWaitSeconds = 30,
  })  : _service = service,
        _email = email {
    _remainingSeconds = otpValiditySeconds;
    _resendCooldownSeconds = 0;
  }

  final PinResetService _service;

  /// Email al que se envió el código (va como `email` al backend).
  final String _email;

  /// Vigencia del OTP en segundos (viene de `ttl_seconds` del `request`).
  final int otpValiditySeconds;

  /// Espera mínima entre reenvíos (`resend_wait_seconds` del `request`).
  final int resendWaitSeconds;

  /// Mensaje NEUTRO único (idéntico exista o no el email).
  static const String neutralMessage =
      'Si el correo está registrado, recibirás un código.';

  /// Mensaje de espera para `429 RATE_LIMITED`.
  static const String rateMessage =
      'Demasiados intentos. Espera un momento e inténtalo de nuevo.';

  PinResetOtpStatus _status = PinResetOtpStatus.idle;
  String? _errorMessage;
  String _code = '';
  bool _isResending = false;
  int _remainingSeconds = 0;
  int _resendCooldownSeconds = 0;
  Timer? _timer;
  bool _disposed = false;

  PinResetOtpStatus get status => _status;
  String? get errorMessage => _errorMessage;

  /// Aviso informativo neutro (siempre el mismo, sin filtrar existencia).
  String get infoMessage => neutralMessage;
  String get code => _code;
  bool get isResending => _isResending;
  int get remainingSeconds => _remainingSeconds;
  int get resendCooldownSeconds => _resendCooldownSeconds;
  bool get isBusy =>
      _status == PinResetOtpStatus.submitting || _isResending;
  bool get succeeded => _status == PinResetOtpStatus.success;

  /// Avance habilitado solo con el código completo y sin operación en curso.
  /// (El OTP se valida en el servidor recién en el request final.)
  bool get canSubmit => _code.length == 6 && !isBusy && !succeeded;

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
  /// de vencido lo decide el backend con `INVALID_PIN_RESET`).
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
      if (_status == PinResetOtpStatus.error) {
        _status = PinResetOtpStatus.idle;
        _errorMessage = null;
      }
      notifyListeners();
    }
  }

  /// Valida el formato local y marca el éxito para avanzar a crear el PIN.
  /// (Sin red: el OTP se consume en el request final de `pin-reset`.)
  Future<bool> submit(String code) async {
    if (isBusy || succeeded) return false;
    final clean = code.trim();
    if (clean.length != 6 || int.tryParse(clean) == null) {
      _status = PinResetOtpStatus.error;
      _errorMessage = 'Ingresa los 6 dígitos del código.';
      notifyListeners();
      return false;
    }
    _code = clean;
    _status = PinResetOtpStatus.success;
    stopTick();
    notifyListeners();
    return true;
  }

  /// Reenvía el código (`requestOtp` de nuevo) y reinicia la vigencia.
  Future<bool> resend() async {
    if (!canResend) return false;
    _isResending = true;
    _errorMessage = null;
    notifyListeners();
    try {
      final result = await _service.requestOtp(email: _email.trim());
      _isResending = false;
      _remainingSeconds = result.ttlSeconds.clamp(1, otpValiditySeconds);
      // El cooldown usa el valor RECIÉN devuelto por el backend; fallback al
      // del constructor/query solo si el backend trae 0/no positivo.
      _resendCooldownSeconds = result.resendWaitSeconds > 0
          ? result.resendWaitSeconds
          : resendWaitSeconds;
      _status = PinResetOtpStatus.idle;
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _isResending = false;
      _status = PinResetOtpStatus.error;
      _errorMessage = e.code == 'RATE_LIMITED' ? rateMessage : e.message;
      notifyListeners();
      return false;
    } catch (_) {
      // Error inesperado (no ApiException): limpiar el estado de reenvío
      // (sin spinner fijo) y mensaje genérico, sin filtrar detalles ni PII.
      _isResending = false;
      _status = PinResetOtpStatus.error;
      _errorMessage =
          'No pudimos completar el restablecimiento. Inténtalo de nuevo.';
      notifyListeners();
      return false;
    }
  }

  /// Reintento tras un error (limpia el error visible y vuelve al
  /// formulario).
  void retry() {
    if (_status == PinResetOtpStatus.error) {
      _status = PinResetOtpStatus.idle;
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
    stopTick();
    super.dispose();
  }
}

/// Estados visibles del paso de confirmación (request final).
enum PinResetConfirmStatus { idle, submitting, success, error }

/// Paso `/pin-reset/confirm`: fija el PIN contra `POST /auth/pin-reset`.
///
/// Al éxito persiste best-effort el `user_ref` devuelto y expone
/// [resetUserRef] para que la página navegue a `/pin-reset/success` (y de
/// ahí a `/login?userRef=`). Aquí NO se abre sesión ni se guardan tokens.
class PinResetConfirmController extends ChangeNotifier {
  PinResetConfirmController({
    required PinResetService service,
    required PinResetDraft draft,
    SessionIdentityStore? identity,
  })  : _service = service,
        _draft = draft,
        _identity = identity;

  final PinResetService _service;
  final PinResetDraft _draft;

  /// Store F-T20 para persistir el `user_ref` tras el éxito (best-effort).
  final SessionIdentityStore? _identity;

  /// Mensaje ÚNICO y genérico para `401 INVALID_PIN_RESET` (cubre email no
  /// registrado, DNI que no coincide, sin OTP, código incorrecto, OTP
  /// vencido y OTP bloqueado, sin distinguir el campo).
  static const String invalidResetMessage =
      'No pudimos restablecer tu PIN con esos datos. '
      'Verifica e inténtalo de nuevo.';

  /// Mensaje de espera para `429 RATE_LIMITED`.
  static const String rateMessage =
      'Demasiados intentos. Espera un momento e inténtalo de nuevo.';

  PinResetConfirmStatus _status = PinResetConfirmStatus.idle;
  String? _errorMessage;
  String? _resetUserRef;
  bool _disposed = false;

  PinResetConfirmStatus get status => _status;
  String? get errorMessage => _errorMessage;
  /// `user_ref` devuelto por el último `pin-reset` exitoso (`null` antes
  /// del éxito). La página lo usa para navegar al success.
  String? get resetUserRef => _resetUserRef;
  bool get isBusy => _status == PinResetConfirmStatus.submitting;
  bool get succeeded => _status == PinResetConfirmStatus.success;

  /// Reintento tras un error (limpia el error visible y vuelve al teclado).
  void retry() {
    if (_status == PinResetConfirmStatus.error) {
      _status = PinResetConfirmStatus.idle;
      _errorMessage = null;
      notifyListeners();
    }
  }

  /// Fija el PIN. Retorna `true` en éxito (`pin_set: true`).
  Future<bool> submit() async {
    if (isBusy || succeeded) return false;
    _status = PinResetConfirmStatus.submitting;
    _errorMessage = null;
    notifyListeners();
    try {
      final result = await _service.resetPin(
        email: _draft.email.trim(),
        docNumber: _draft.docNumber.trim(),
        docType: _draft.docType,
        code: _draft.code.trim(),
        pin: _draft.pin,
      );
      // Best-effort: si falla, no bloquea la continuación al login ya
      // concedida (silencio deliberado: nunca se loguea el `user_ref`).
      await _rememberUserRef(result.userRef);
      _resetUserRef = result.userRef;
      _status = PinResetConfirmStatus.success;
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _applyError(e);
      notifyListeners();
      return false;
    } catch (_) {
      // Fallo del secure storage: mensaje accionable con reintento, sin
      // filtrar detalles.
      _status = PinResetConfirmStatus.error;
      _errorMessage =
          'No pudimos completar el restablecimiento. Inténtalo de nuevo.';
      notifyListeners();
      return false;
    }
  }

  void _applyError(ApiException e) {
    switch (e.code) {
      case 'INVALID_PIN_RESET':
        // Único error genérico: mismo mensaje sin distinguir email / DNI /
        // OTP ni vencido / bloqueado.
        _status = PinResetConfirmStatus.error;
        _errorMessage = invalidResetMessage;
      case 'RATE_LIMITED':
        _status = PinResetConfirmStatus.error;
        _errorMessage = rateMessage;
      default:
        // Red / servidor / 422 (email/PIN malformado o PIN débil): mensaje
        // en español de la capa HTTP (F-T01), accionable con reintento.
        _status = PinResetConfirmStatus.error;
        _errorMessage = e.message;
    }
  }

  Future<void> _rememberUserRef(String userRef) async {
    final identity =
        _identity ?? sessionIdentityStoreFactory?.call();
    if (identity == null || userRef.isEmpty) return;
    try {
      await identity.saveUserRef(userRef);
    } catch (_) {
      // Silencio deliberado: no se registra el `user_ref` ni el error.
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
