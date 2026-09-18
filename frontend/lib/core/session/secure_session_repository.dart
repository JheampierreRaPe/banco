import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';

import 'secure_key_value_storage.dart';
import 'session_repository.dart';

/// Sesion persistente en almacenamiento seguro (F-T02).
///
/// - `access_token` / `refresh_token` / `device.secret` viven SOLO en
///   [SecureKeyValueStorage]; en memoria solo hay un caché del access
///   (el `onRequest` de dio es síncrono y no puede leer async).
/// - Llamar a [load] en `main()` antes de `runApp` para hidratar el caché.
/// - Es [ChangeNotifier]: `go_router` (`refreshListenable`) redirige a
///   `/login` en cuanto la sesion se limpia (logout o refresh invalido).
/// - Nunca loguea tokens ni el secreto (docs/16 reglas 7 y 10).
class SecureSessionRepository extends ChangeNotifier
    implements SessionRepository {
  SecureSessionRepository({
    required SecureKeyValueStorage storage,
    Random? random,
  }) {
    _storage = storage;
    _random = random ?? Random.secure();
  }

  late final SecureKeyValueStorage _storage;
  late final Random _random;

  @visibleForTesting
  static const accessTokenKey = 'session.access_token';
  @visibleForTesting
  static const refreshTokenKey = 'session.refresh_token';
  @visibleForTesting
  static const deviceSecretKey = 'device.secret';

  String? _accessToken;

  /// Hidrata el caché en memoria desde secure storage.
  Future<void> load() async {
    _accessToken = await _storage.read(accessTokenKey);
    notifyListeners();
  }

  @override
  String? get currentAccessToken => _accessToken;

  @override
  bool get isAuthenticated =>
      _accessToken != null && _accessToken!.isNotEmpty;

  @override
  Future<void> saveSession({
    required String accessToken,
    String? refreshToken,
  }) async {
    await _storage.write(accessTokenKey, accessToken);
    if (refreshToken != null) {
      await _storage.write(refreshTokenKey, refreshToken);
    } else {
      await _storage.delete(refreshTokenKey);
    }
    _accessToken = accessToken;
    notifyListeners();
  }

  @override
  Future<void> clear() => clearOnLogout();

  @override
  Future<String?> readRefreshToken() => _storage.read(refreshTokenKey);

  /// Secreto de dispositivo para la firma de nonce (seam F-T03).
  ///
  /// Por ahora es un secreto aleatorio de 32 bytes (base64) que F-T03 usará
  /// para HMAC. La criptografía asimétrica real (par de claves en Keystore /
  /// Keychain) se introduce cuando el backend la exija.
  @override
  Future<String> getOrCreateDeviceSecret() async {
    final existing = await _storage.read(deviceSecretKey);
    if (existing != null && existing.isNotEmpty) return existing;
    final bytes = List<int>.generate(32, (_) => _random.nextInt(256));
    final secret = base64Url.encode(bytes);
    await _storage.write(deviceSecretKey, secret);
    return secret;
  }

  @override
  Future<void> clearOnLogout() async {
    await _storage.delete(accessTokenKey);
    await _storage.delete(refreshTokenKey);
    // device.secret se conserva: identifica al dispositivo, no a la sesion.
    _accessToken = null;
    notifyListeners();
  }

  @override
  Future<void> clearOnInvalidRefresh() async {
    await _storage.delete(accessTokenKey);
    await _storage.delete(refreshTokenKey);
    await _storage.delete(deviceSecretKey);
    _accessToken = null;
    notifyListeners();
  }
}
