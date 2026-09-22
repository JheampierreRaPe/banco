// Estado del paso OTP del alta (F-T39): verifica el codigo de activacion y
// crea el PIN con `POST /auth/pin/setup`.
//
// Cliente delgado (docs/19): la app captura el codigo y navega; el backend
// valida el OTP/PIN y activa. El PIN/OTP nunca se registra en logs ni se
// devuelve en mensajes (docs/16 reglas 7 y 10).
// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública (`setupService:`,
// `resendService:`, `userRef:`) y no pueden ser formales inicializadores de
// campos privados.
library;

import 'package:flutter/foundation.dart';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/activation/activation_service.dart';

import 'pin_setup_service.dart';

/// Estados visibles del paso OTP del alta.
enum PinSetupOtpStatus { idle, submitting, success, pinAlreadySet }

/// Controlador del OTP de activacion del registro.
///
/// - [submit] envia `userRef + code + pin` a [PinSetupService] (el PIN viaja
///   solo en memoria, nunca se loguea).
/// - [resend] pide un codigo nuevo por email con el MISMO contrato de
///   `activation` (`POST /auth/otp/resend`, sin duplicar el endpoint).
class PinSetupOtpController extends ChangeNotifier {
  PinSetupOtpController({
    required PinSetupService setupService,
    required ActivationService resendService,
    required String userRef,
  })  : _setupService = setupService,
        _resendService = resendService,
        _userRef = userRef;

  final PinSetupService _setupService;
  final ActivationService _resendService;
  final String _userRef;

  PinSetupOtpStatus _status = PinSetupOtpStatus.idle;
  String? _errorMessage;
  String? _infoMessage;
  String _code = '';
  bool _isResending = false;

  PinSetupOtpStatus get status => _status;
  String? get errorMessage => _errorMessage;
  String? get infoMessage => _infoMessage;
  String get code => _code;
  bool get isResending => _isResending;
  bool get isBusy =>
      _status == PinSetupOtpStatus.submitting || _isResending;

  /// `true` si el PIN ya existe (409): la pagina ofrece ir al login.
  bool get isPinAlreadySet => _status == PinSetupOtpStatus.pinAlreadySet;

  static const String codeHint =
      'Ingresa el código de 6 dígitos que enviamos a tu correo.';
  static const String codeMessage = 'Ingresa los 6 dígitos del código.';
  static const String invalidCodeMessage =
      'El código es inválido o venció. Pide un código nuevo.';
  static const String alreadySetMessage =
      'Ya tienes un PIN creado. Inicia sesión para continuar.';
  static const String rateLimitedMessage =
      'Demasiados intentos. Espera unos minutos e inténtalo de nuevo.';
  static const String resentMessage =
      'Te enviamos un nuevo código. Revisa tu correo.';

  void setCode(String value) {
    if (_code != value) {
      _code = value;
      notifyListeners();
    }
  }

  /// Verifica el codigo y crea el PIN. Retorna `true` en exito.
  Future<bool> submit(String pin) async {
    final cleanCode = _code.trim();
    if (cleanCode.length != 6 || int.tryParse(cleanCode) == null) {
      _errorMessage = codeMessage;
      _infoMessage = null;
      notifyListeners();
      return false;
    }
    _status = PinSetupOtpStatus.submitting;
    _errorMessage = null;
    _infoMessage = null;
    notifyListeners();
    try {
      await _setupService.setup(
        userRef: _userRef,
        code: cleanCode,
        pin: pin,
      );
      _status = PinSetupOtpStatus.success;
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _applyError(e);
      notifyListeners();
      return false;
    }
  }

  /// Pide un codigo nuevo por email (`POST /auth/otp/resend`, invalida el
  /// anterior). El OTP del alta viaja solo por correo (E1-T32).
  Future<bool> resend() async {
    if (_isResending || _status == PinSetupOtpStatus.success) return false;
    _isResending = true;
    _errorMessage = null;
    notifyListeners();
    try {
      await _resendService.resend(userRef: _userRef, channel: 'email');
      _isResending = false;
      _infoMessage = resentMessage;
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _isResending = false;
      _errorMessage = _resendError(e);
      notifyListeners();
      return false;
    }
  }

  void _applyError(ApiException e) {
    switch (e.code) {
      case 'INVALID_SETUP_CODE':
      case 'INVALID_OTP':
      case 'EXPIRED_OTP':
        _status = PinSetupOtpStatus.idle;
        _errorMessage = invalidCodeMessage;
      case 'PIN_ALREADY_SET':
        _status = PinSetupOtpStatus.pinAlreadySet;
        _errorMessage = alreadySetMessage;
      case 'RATE_LIMITED':
      case 'RESEND_LIMIT':
        if (_status == PinSetupOtpStatus.submitting) {
          _status = PinSetupOtpStatus.idle;
        }
        _errorMessage = rateLimitedMessage;
      default:
        if (_status == PinSetupOtpStatus.submitting) {
          _status = PinSetupOtpStatus.idle;
        }
        _errorMessage = e.message;
    }
  }

  String _resendError(ApiException e) {
    switch (e.code) {
      case 'RATE_LIMITED':
      case 'RESEND_LIMIT':
        return rateLimitedMessage;
      default:
        return e.message;
    }
  }
}
