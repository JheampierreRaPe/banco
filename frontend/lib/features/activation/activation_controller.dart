// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública (`service:`,
// `userRef:`) y no pueden ser formales inicializadores de campos privados.
import 'dart:async';

import 'package:flutter/foundation.dart';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/notifications/notification_service.dart';

import 'activation_service.dart';

/// Estados visibles de la pantalla de activación (E1-T11).
///
/// - [idle]: ingreso del código (un error recuperable viaja en
///   [ActivationController.errorMessage] sin cambiar de estado).
/// - [submitting]: "cargando" (llamada a `/auth/activate` en curso).
/// - [success]: cuenta `ACTIVE`; la página navega a `/login`.
/// - [expired]: código vencido (contador en 0 o `EXPIRED_OTP` del backend);
///   la página ofrece el reenvío de inmediato.
/// - [limitReached]: `RESEND_LIMIT`; ya no se puede reenviar.
enum ActivationStatus { idle, submitting, success, expired, limitReached }

/// Formatea segundos a `MM:SS` para el contador visible (10:00 inicial).
String formatCountdown(int totalSeconds) {
  final clamped = totalSeconds < 0 ? 0 : totalSeconds;
  final minutes = clamped ~/ 60;
  final seconds = clamped % 60;
  return '${minutes.toString().padLeft(2, '0')}:'
      '${seconds.toString().padLeft(2, '0')}';
}

/// Estado de la pantalla de activación: contadores, reenvío y mapeo de
/// `error.code` a mensajes accionables en español.
///
/// Los contadores avanzan con [tick] (1 s). La página usa [startAutoTick]
/// (Timer real); los tests llaman a [tick] manualmente para no depender de
/// tiempo real (determinista, sin `fakeAsync`).
class ActivationController extends ChangeNotifier {
  ActivationController({
    required ActivationService service,
    required String userRef,
    this.otpValiditySeconds = 600,
    this.resendWaitSeconds = 30,
    NotificationService? notifications,
  })  : _service = service,
        _userRef = userRef,
        _notifications = notifications {
    // El código se acaba de emitir (el usuario viene del registro), así que
    // el contador parte en 10:00 (`OTP_TTL_SECONDS`) y el reenvío exige la
    // espera de 30 s (`OTP_RESEND_WAIT_SECONDS`).
    _remainingSeconds = otpValiditySeconds;
    _resendCooldownSeconds = resendWaitSeconds;
  }

  final ActivationService _service;
  final String _userRef;

  /// Servicio de notificaciones locales (opcional para no romper llamadas
  /// existentes). Tras un reenvío exitoso muestra el aviso informativo SIN
  /// el código (el backend mock nunca lo devuelve; HU22 para el push real).
  final NotificationService? _notifications;

  /// Servicio de notificaciones asociado (visible para tests).
  NotificationService? get notifications => _notifications;

  /// Vigencia del OTP en segundos (600 = 10 min, `OTP_TTL_SECONDS`).
  final int otpValiditySeconds;

  /// Espera mínima entre reenvíos en segundos (`OTP_RESEND_WAIT_SECONDS`).
  final int resendWaitSeconds;

  ActivationStatus _status = ActivationStatus.idle;
  String? _errorMessage;
  String? _infoMessage;
  String _code = '';
  bool _isResending = false;
  int _remainingSeconds = 0;
  int _resendCooldownSeconds = 0;
  int _resendCount = 0;
  Timer? _timer;

  ActivationStatus get status => _status;
  String? get errorMessage => _errorMessage;
  String? get infoMessage => _infoMessage;
  String get code => _code;
  bool get isResending => _isResending;
  int get remainingSeconds => _remainingSeconds;
  int get resendCooldownSeconds => _resendCooldownSeconds;
  int get resendCount => _resendCount;
  bool get isBusy =>
      _status == ActivationStatus.submitting || _isResending;

  /// `true` si se puede pedir otro código ahora mismo.
  bool get canResend =>
      _resendCooldownSeconds <= 0 &&
      !_isResending &&
      _status != ActivationStatus.success &&
      _status != ActivationStatus.limitReached;

  /// Inicia el descuento automático (1 s). La página lo llama en `initState`.
  void startAutoTick() {
    _timer?.cancel();
    _timer = Timer.periodic(const Duration(seconds: 1), (_) => tick());
  }

  void stopTick() {
    _timer?.cancel();
    _timer = null;
  }

  /// Avanza un segundo ambos contadores. Al agotar la vigencia marca
  /// [ActivationStatus.expired] con mensaje accionable.
  void tick() {
    if (_status == ActivationStatus.success) return;
    var changed = false;
    if (_remainingSeconds > 0) {
      _remainingSeconds--;
      changed = true;
      if (_remainingSeconds == 0 &&
          _status != ActivationStatus.limitReached) {
        _status = ActivationStatus.expired;
        _errorMessage = expiredMessage;
      }
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
      notifyListeners();
    }
  }

  static const String invalidMessage = 'El código no es correcto. '
      'Revisa el mensaje que te enviamos e inténtalo de nuevo.';
  static const String expiredMessage =
      'El código venció. Pide uno nuevo con “Reenviar código”.';
  static const String limitMessage =
      'Alcanzaste el límite de reenvíos. '
      'Inténtalo más tarde o contacta a tu banco.';
  static const String rateMessage =
      'Demasiados intentos. Espera un momento e inténtalo de nuevo.';
  static const String resentMessage =
      'Te enviamos un nuevo código. Revisa tus mensajes.';
  static const String resendFailedMessage =
      'No pudimos reenviar el código. Vuelve a intentarlo.';

  /// Envía el código a `/auth/activate`. Retorna `true` si la cuenta quedó
  /// `ACTIVE`.
  Future<bool> submit(String code) async {
    final clean = code.trim();
    if (clean.length != 6 || int.tryParse(clean) == null) {
      _errorMessage = 'Ingresa los 6 dígitos del código.';
      notifyListeners();
      return false;
    }
    _status = ActivationStatus.submitting;
    _errorMessage = null;
    _infoMessage = null;
    notifyListeners();
    try {
      await _service.activate(userRef: _userRef, code: clean);
      _status = ActivationStatus.success;
      stopTick();
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _applyError(e, fromResend: false);
      notifyListeners();
      return false;
    }
  }

  /// Pide un nuevo código a `/auth/otp/resend`. Al emitirse, reinicia el
  /// contador de vigencia con `expires_in` y aplica la espera de 30 s.
  /// Retorna `true` si se emitió.
  Future<bool> resend() async {
    if (!canResend) return false;
    _isResending = true;
    _errorMessage = null;
    notifyListeners();
    try {
      final result = await _service.resend(userRef: _userRef);
      _isResending = false;
      _resendCount = result.resendCount;
      _remainingSeconds =
          result.expiresInSeconds.clamp(1, otpValiditySeconds);
      _resendCooldownSeconds = resendWaitSeconds;
      _status = ActivationStatus.idle;
      _infoMessage = resentMessage;
      // Aviso local en el dispositivo (informativo, sin OTP): el código
      // real viaja por el canal SMS mock y no vuelve en la respuesta.
      _notifications?.showOtpSent(userRef: _userRef);
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _isResending = false;
      _applyError(e, fromResend: true);
      notifyListeners();
      return false;
    }
  }

  void _applyError(ApiException e, {required bool fromResend}) {
    switch (e.code) {
      case 'EXPIRED_OTP':
        _status = ActivationStatus.expired;
        _errorMessage = expiredMessage;
        // Accionable: habilitar el reenvío de inmediato.
        _resendCooldownSeconds = 0;
      case 'RESEND_LIMIT':
        _status = ActivationStatus.limitReached;
        _errorMessage = limitMessage;
      case 'INVALID_OTP':
        if (_status == ActivationStatus.submitting) {
          _status = ActivationStatus.idle;
          _errorMessage = invalidMessage;
        } else {
          // Viene de `resend` (sin OTP pendiente): mensaje propio.
          _errorMessage = fromResend ? resendFailedMessage : invalidMessage;
        }
      case 'RATE_LIMITED':
        if (_status == ActivationStatus.submitting) {
          _status = ActivationStatus.idle;
        }
        _errorMessage = rateMessage;
      default:
        // Red / servidor: `ApiException.message` ya viene en español
        // (mapeo de `core/errors`). Se conserva el estado de vigencia.
        if (_status == ActivationStatus.submitting) {
          _status = ActivationStatus.idle;
        }
        _errorMessage = e.message;
    }
  }

  @override
  void dispose() {
    stopTick();
    super.dispose();
  }
}
