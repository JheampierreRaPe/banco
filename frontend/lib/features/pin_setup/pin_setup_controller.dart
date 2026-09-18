// ignore_for_file: prefer_initializing_formals
// Razón: los parámetros del constructor son API pública (`setupService:`,
// `resendService:`, `userRef:`) y no pueden ser formales inicializadores de
// campos privados.
import 'package:flutter/foundation.dart';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/activation/activation_service.dart';

import 'pin_setup_service.dart';

/// Estados visibles de la pantalla de creación de PIN.
enum PinSetupStatus { idle, submitting, success, pinAlreadySet }

/// Estado de la pantalla de creación de PIN: validación local, envío a
/// `POST /auth/pin/setup` y reenvío vía [ActivationService] (mismo
/// endpoint de `activation`, sin duplicarlo).
///
/// Seguridad: el PIN nunca se registra en logs ni se expone en mensajes.
class PinSetupController extends ChangeNotifier {
  PinSetupController({
    required PinSetupService setupService,
    required ActivationService resendService,
    required String userRef,
  })  : _setupService = setupService,
        _resendService = resendService,
        _userRef = userRef;

  final PinSetupService _setupService;
  final ActivationService _resendService;
  final String _userRef;

  PinSetupStatus _status = PinSetupStatus.idle;
  String? _errorMessage;
  String? _infoMessage;
  String _code = '';
  String _pin = '';
  String _confirmPin = '';
  bool _isResending = false;

  PinSetupStatus get status => _status;
  String? get errorMessage => _errorMessage;
  String? get infoMessage => _infoMessage;
  String get code => _code;
  bool get isResending => _isResending;
  bool get isBusy =>
      _status == PinSetupStatus.submitting || _isResending;

  /// `true` si el PIN ya existe (409): la página muestra el botón ir-a-login.
  bool get isPinAlreadySet => _status == PinSetupStatus.pinAlreadySet;

  static const String codeHint =
      'Tu código de activación anterior ya se usó. Si necesitas uno nuevo, '
      'pídelo con “Enviarme un código nuevo” e ingrésalo aquí junto con tu PIN.';
  static const String invalidCodeMessage =
      'El código es inválido o venció. Pide un código nuevo.';
  static const String alreadySetMessage =
      'Ya tienes un PIN creado. Inicia sesión para continuar.';
  static const String mismatchMessage = 'Los PIN no coinciden.';
  static const String pinLengthMessage = 'El PIN debe tener de 4 a 6 dígitos.';
  static const String pinDigitsMessage =
      'El PIN solo puede contener dígitos.';
  static const String codeMessage = 'Ingresa los 6 dígitos del código.';
  static const String resentMessage =
      'Te enviamos un nuevo código. Revisa tus mensajes.';

  void setCode(String value) {
    if (_code != value) {
      _code = value;
      notifyListeners();
    }
  }

  void setPin(String value) {
    if (_pin != value) {
      _pin = value;
      notifyListeners();
    }
  }

  void setConfirmPin(String value) {
    if (_confirmPin != value) {
      _confirmPin = value;
      notifyListeners();
    }
  }

  /// Validación local antes de enviar. Retorna el mensaje de error o `null`
  /// si todo es válido.
  String? validate() {
    final cleanCode = _code.trim();
    if (cleanCode.length != 6 || int.tryParse(cleanCode) == null) {
      return codeMessage;
    }
    if (_pin.length < 4 || _pin.length > 6) {
      return pinLengthMessage;
    }
    if (int.tryParse(_pin) == null) {
      return pinDigitsMessage;
    }
    if (_pin != _confirmPin) {
      return mismatchMessage;
    }
    return null;
  }

  /// Envía código + PIN a `POST /auth/pin/setup`. Retorna `true` en éxito.
  Future<bool> submit() async {
    final localError = validate();
    if (localError != null) {
      _errorMessage = localError;
      _infoMessage = null;
      notifyListeners();
      return false;
    }
    _status = PinSetupStatus.submitting;
    _errorMessage = null;
    _infoMessage = null;
    notifyListeners();
    try {
      await _setupService.setup(
        userRef: _userRef,
        code: _code.trim(),
        pin: _pin,
      );
      _status = PinSetupStatus.success;
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _applyError(e);
      notifyListeners();
      return false;
    }
  }

  /// Pide un código nuevo con el MISMO servicio de `activation`
  /// (`POST /auth/otp/resend`, invalida el anterior).
  Future<bool> resend() async {
    if (_isResending || _status == PinSetupStatus.success) return false;
    _isResending = true;
    _errorMessage = null;
    notifyListeners();
    try {
      await _resendService.resend(userRef: _userRef);
      _isResending = false;
      _infoMessage = resentMessage;
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _isResending = false;
      _errorMessage = e.message;
      notifyListeners();
      return false;
    }
  }

  void _applyError(ApiException e) {
    switch (e.code) {
      case 'INVALID_SETUP_CODE':
        _status = PinSetupStatus.idle;
        _errorMessage = invalidCodeMessage;
      case 'PIN_ALREADY_SET':
        _status = PinSetupStatus.pinAlreadySet;
        _errorMessage = alreadySetMessage;
      default:
        if (_status == PinSetupStatus.submitting) {
          _status = PinSetupStatus.idle;
        }
        _errorMessage = e.message;
    }
  }
}
