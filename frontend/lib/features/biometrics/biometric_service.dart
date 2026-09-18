// Servicio de biometria local + firma de nonce (F-T03, HU03).
//
// Flujo (docs/05 §6.1, docs/06 HU03):
//  1. El servidor emite un `nonce` (`POST /auth/login/challenge`).
//  2. La app exige el biometrico del dispositivo ([BiometricReader]).
//  3. Con el biometrico superado, firma el `nonce` con el `device.secret`
//     de F-T02 ([SessionRepository.getOrCreateDeviceSecret]) y lo envia a
//     `POST /auth/login/facial` (lo hace [LoginController], no este servicio).
//
// Reglas (brief F-T03 + docs/16):
//  - Si la biometria falla o no esta disponible -> fallback a PIN (lo senala
//    este servicio; la pantalla de PIN la monta E1-T16).
//  - La clave privada nunca sale del almacenamiento seguro: solo se usa en
//    memoria para el HMAC y jamas se loguea (docs/16 reglas 7 y 10).
//  - El dispositivo no ejecuta modelos de IA (docs/13 §1.2).
library;

import 'dart:convert';

import 'package:flutter/foundation.dart';

import '../../core/session/session_repository.dart';
import 'biometric_reader.dart';
import 'hmac_sha256.dart';

/// Biometria local + firma del `nonce` de login.
class BiometricService {
  BiometricService({required this._session, required this._reader});

  final SessionRepository _session;
  final BiometricReader _reader;

  /// `true` si el dispositivo puede pedir biometria ahora mismo.
  Future<bool> isAvailable() => _reader.isAvailable();

  /// Pide el biometrico del SO con el [reason] visible al usuario.
  ///
  /// Nunca lanza: cualquier fallo se traduce a
  /// [BiometricAuthResult.requiresPinFallback] (CA-02 HU03).
  Future<BiometricAuthResult> authenticate({required String reason}) =>
      _reader.authenticate(reason: reason);

  /// Firma [nonce] con HMAC-SHA256 usando el `device.secret` y devuelve el
  /// hex en minusculas (64 chars), el formato exacto que espera el backend
  /// (`backend/tests/test_device_login.py::_sign_hmac`):
  /// `hmac.new(secret_bytes, nonce.encode("utf-8"), sha256).hexdigest()`.
  ///
  /// Lanza [ArgumentError] si [nonce] es vacio (nunca se firma ni se envia
  /// un nonce vacio).
  Future<String> signNonce(String nonce) async {
    if (nonce.isEmpty) {
      throw ArgumentError.value(nonce, 'nonce', 'No se firma un nonce vacio');
    }
    final secret = await _session.getOrCreateDeviceSecret();
    final key = decodeDeviceSecret(secret);
    return hmacSha256Hex(key, utf8.encode(nonce));
  }

  /// Valida el formato de una firma ANTES de enviarla al backend.
  ///
  /// Formato exacto del backend: 64 hex en minusculas
  /// (`hashlib.sha256(...).hexdigest()`). Mayusculas, prefijos `0x`,
  /// longitudes distintas o caracteres fuera de `[0-9a-f]` son invalidos.
  static bool isValidSignatureFormat(String signature) {
    if (signature.length != 64) return false;
    for (var i = 0; i < signature.length; i++) {
      final c = signature.codeUnitAt(i);
      final isDigit = c >= 0x30 && c <= 0x39;
      final isLowerHex = c >= 0x61 && c <= 0x66;
      if (!isDigit && !isLowerHex) return false;
    }
    return true;
  }

  /// Decodifica el `device.secret` a los bytes de la clave HMAC.
  ///
  /// Orden determinista (el primero que aplique gana):
  ///  1. Prefijo `hmac:` -> el resto es hex (formato de binding del backend,
  ///     `public_key = "hmac:<hex>"` en `test_device_login.py`).
  ///  2. Base64URL de 32 bytes -> formato de F-T02
  ///     (`SecureSessionRepository.getOrCreateDeviceSecret`: 32 bytes
  ///     aleatorios en base64).
  ///  3. Hex par (`^[0-9a-fA-F]+$`) -> secreto registrado como hex suelto.
  ///  4. Resto -> bytes UTF-8 del texto.
  @visibleForTesting
  static List<int> decodeDeviceSecret(String secret) {
    final s = secret.trim();
    if (s.startsWith('hmac:')) {
      return _hexDecode(s.substring(5));
    }
    try {
      final bytes = base64Url.decode(s);
      if (bytes.length == 32) return bytes;
    } on FormatException {
      // No es base64 valido: sigue a las ramas de abajo.
    }
    if (s.length.isEven && s.isNotEmpty && _isHex(s)) {
      return _hexDecode(s);
    }
    return utf8.encode(s);
  }

  static bool _isHex(String s) {
    for (var i = 0; i < s.length; i++) {
      final c = s.codeUnitAt(i);
      final isDigit = c >= 0x30 && c <= 0x39;
      final isLower = c >= 0x61 && c <= 0x66;
      final isUpper = c >= 0x41 && c <= 0x46;
      if (!isDigit && !isLower && !isUpper) return false;
    }
    return true;
  }

  static List<int> _hexDecode(String hex) {
    final out = <int>[];
    for (var i = 0; i < hex.length; i += 2) {
      out.add(int.parse(hex.substring(i, i + 2), radix: 16));
    }
    return out;
  }
}
