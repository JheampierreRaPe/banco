import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';

import 'session_repository.dart';

/// Implementacion en memoria de [SessionRepository] (F-T01, seam de tests).
///
/// Los tokens viven SOLO en memoria. En produccion se usa
/// [SecureSessionRepository] (F-T02). Es un [ChangeNotifier] para que
/// `go_router` (`refreshListenable`) reaccione a login/logout.
class InMemorySessionRepository extends ChangeNotifier
    implements SessionRepository {
  String? _accessToken;
  String? _refreshToken;
  String? _deviceSecret;

  final Random _random;

  /// Crea el repositorio opcionalmente pre-autenticado (util para tests).
  InMemorySessionRepository({String? initialAccessToken, Random? random})
      : _accessToken = initialAccessToken,
        _random = random ?? Random.secure();

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
    _accessToken = accessToken;
    _refreshToken = refreshToken;
    notifyListeners();
  }

  @override
  Future<void> clear() => clearOnLogout();

  @override
  Future<String?> readRefreshToken() async => _refreshToken;

  @override
  Future<String> getOrCreateDeviceSecret() async {
    if (_deviceSecret != null && _deviceSecret!.isNotEmpty) {
      return _deviceSecret!;
    }
    final bytes = List<int>.generate(32, (_) => _random.nextInt(256));
    _deviceSecret = base64Url.encode(bytes);
    return _deviceSecret!;
  }

  @override
  Future<void> clearOnLogout() async {
    _accessToken = null;
    _refreshToken = null;
    // El secreto identifica al dispositivo, no a la sesion: se conserva.
    notifyListeners();
  }

  @override
  Future<void> clearOnInvalidRefresh() async {
    _accessToken = null;
    _refreshToken = null;
    _deviceSecret = null; // posible robo: se regenera bajo demanda.
    notifyListeners();
  }

  /// Solo para depuracion/tests. Nunca loguear el token real en produccion.
  @visibleForTesting
  String? get debugRefreshToken => _refreshToken;
}
