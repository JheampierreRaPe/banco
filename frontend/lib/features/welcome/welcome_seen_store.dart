import 'package:flutter/foundation.dart';

import '../../core/session/secure_key_value_storage.dart';

/// Seam del flag de primera vez `hasSeenWelcome` (pantalla de bienvenida).
///
/// Verificacion de API (exigida por el brief): [SessionRepository] /
/// [SecureSessionRepository] NO exponen ningun flag de bienvenida (solo
/// tokens + secreto de dispositivo), asi que NO se asumen metodos
/// inexistentes ni se rompe su interfaz: el flag vive aqui, en el feature
/// `welcome`, persistido con la misma [SecureKeyValueStorage]
/// (`flutter_secure_storage`) que usa la sesion.
///
/// - Sincrono para la guarda de `go_router` (`redirect` no puede esperar):
///   [hasSeenWelcome] es un cache en memoria hidratado con [load()].
/// - Es [Listenable]: el orquestador lo agrega al `refreshListenable` del
///   router para que marcar el flag redirija sin navegacion manual.
abstract class WelcomeSeenStore implements Listenable {
  /// `true` si la bienvenida ya se mostro alguna vez (cache sincronico).
  bool get hasSeenWelcome;

  /// Hidrata el cache desde almacenamiento seguro.
  Future<void> load();

  /// Marca la bienvenida como vista (persiste `'1'`).
  Future<void> markSeenWelcome();
}

/// Implementacion en memoria (tests y fallback sin plataforma).
class InMemoryWelcomeSeenStore extends ChangeNotifier
    implements WelcomeSeenStore {
  InMemoryWelcomeSeenStore({bool initialSeen = false}) : _seen = initialSeen;

  bool _seen;

  @override
  bool get hasSeenWelcome => _seen;

  @override
  Future<void> load() async {}

  @override
  Future<void> markSeenWelcome() async {
    if (_seen) return;
    _seen = true;
    notifyListeners();
  }

  /// Fija el valor en tests (simula primera/segunda apertura).
  @visibleForTesting
  void debugSetSeen(bool value) {
    _seen = value;
    notifyListeners();
  }
}

/// Implementacion persistente sobre almacenamiento seguro (produccion).
class SecureWelcomeSeenStore extends ChangeNotifier
    implements WelcomeSeenStore {
  SecureWelcomeSeenStore({required this._storage});

  final SecureKeyValueStorage _storage;

  @visibleForTesting
  static const seenKey = 'welcome.hasSeenWelcome';

  bool _seen = false;

  @override
  bool get hasSeenWelcome => _seen;

  @override
  Future<void> load() async {
    try {
      _seen = await _storage.read(seenKey) == '1';
    } catch (_) {
      // Sin plataforma o lectura fallida: se asume primera vez.
      _seen = false;
    }
    notifyListeners();
  }

  @override
  Future<void> markSeenWelcome() async {
    _seen = true;
    notifyListeners();
    try {
      await _storage.write(seenKey, '1');
    } catch (_) {
      // El cache en memoria ya quedo en `true`; la navegacion continua.
    }
  }
}
