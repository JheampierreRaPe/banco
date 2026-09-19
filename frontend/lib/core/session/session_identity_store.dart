// Store de identidad local (F-T20): ultimo `user_ref` y `device_id` estable.
//
// Reutiliza el seam de almacenamiento seguro de F-T02
// ([SecureKeyValueStorage]: `FlutterSecureStorageAdapter` en produccion,
// `InMemorySecureStorage` en tests). NO guarda tokens (eso es F-T02) ni
// decide nada: solo conserva referencias para entregarlas al backend
// (docs/19, cliente delgado).
//
// Decision cerrada: una sola cuenta activa por dispositivo (multi-cuenta
// fuera de alcance); [saveUserRef] sobreescribe el `user_ref` anterior.
//
// Nunca loguear el `user_ref`/`device_id` ni los valores del secure storage
// (docs/16 reglas 7 y 10).

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import 'secure_key_value_storage.dart';

/// Store de identidad local (`user_ref` + `device_id`).
///
/// Cache sincronico hidratado con [load] (la guarda/el router no pueden
/// esperar) + lecturas async puntuales.
abstract class SessionIdentityStore {
  /// `user_ref` en cache; `null` si no hay cuenta conocida.
  String? get userRef;

  /// `device_id` en cache; `null` si aun no se ha generado.
  String? get deviceId;

  /// Hidrata el cache desde almacenamiento seguro (antes del primer frame).
  Future<void> load();

  /// Persiste el `user_ref` del alta (E1-T24). Sobreescribe el anterior
  /// (una sola cuenta activa por dispositivo).
  Future<void> saveUserRef(String userRef);

  /// Lee el `user_ref` persistido; `null` si no hay.
  Future<String?> readUserRef();

  /// Devuelve el `device_id` estable, generandolo (UUID v4) la primera vez.
  /// Idempotente: en llamadas sucesivas devuelve el mismo valor.
  Future<String> getOrCreateDeviceId();

  /// Lee el `device_id` persistido; `null` si aun no existe.
  Future<String?> readDeviceId();
}

/// Fabrica del store (la fija `main`/el orquestador; en tests se inyecta una
/// instancia en memoria). Mismo patron que `welcomeSeenStoreFactory`.
typedef SessionIdentityStoreFactory = SessionIdentityStore Function();

/// Fabrica global usada por `buildRouter` cuando no recibe un store explicito.
SessionIdentityStoreFactory? sessionIdentityStoreFactory;

/// Implementacion productiva sobre almacenamiento seguro (cifrado).
class SecureSessionIdentityStore implements SessionIdentityStore {
  SecureSessionIdentityStore({
    required SecureKeyValueStorage storage,
    Uuid? uuid,
  }) {
    _storage = storage;
    _uuid = uuid ?? const Uuid();
  }

  late final SecureKeyValueStorage _storage;
  late final Uuid _uuid;

  /// Claves en secure storage (ver docs/tasks/F-T20.md, modelo de datos).
  @visibleForTesting
  static const userRefKey = 'user.ref';
  @visibleForTesting
  static const deviceIdKey = 'device.id';

  String? _userRef;
  String? _deviceId;

  @override
  String? get userRef => _userRef;

  @override
  String? get deviceId => _deviceId;

  @override
  Future<void> load() async {
    // Una lectura fallida (p. ej. sin plataforma) no debe impedir el arranque:
    // el cache queda vacio y se rehidrata bajo demanda.
    try {
      _userRef = await readUserRef();
      _deviceId = await readDeviceId();
    } catch (_) {
      _userRef = null;
      _deviceId = null;
    }
  }

  @override
  Future<void> saveUserRef(String userRef) async {
    await _storage.write(userRefKey, userRef);
    _userRef = userRef;
  }

  @override
  Future<String?> readUserRef() async {
    final value = await _storage.read(userRefKey);
    if (value == null || value.isEmpty) return null;
    return value;
  }

  @override
  Future<String> getOrCreateDeviceId() async {
    try {
      final existing = await readDeviceId();
      if (existing != null) {
        _deviceId = existing;
        return existing;
      }
    } catch (_) {
      // Secure storage inaccesible: se continua con un id en memoria para no
      // impedir el arranque (best-effort, se rehidrata bajo demanda).
    }
    final created = _deviceId ?? _uuid.v4();
    try {
      await _storage.write(deviceIdKey, created);
    } catch (_) {
      // Escritura fallida: el id queda solo en memoria; la app arranca igual.
    }
    _deviceId = created;
    return created;
  }

  @override
  Future<String?> readDeviceId() async {
    final value = await _storage.read(deviceIdKey);
    if (value == null || value.isEmpty) return null;
    return value;
  }
}

/// Implementacion en memoria (tests de store/router, sin canales de
/// plataforma). No persiste entre instancias.
class InMemorySessionIdentityStore implements SessionIdentityStore {
  InMemorySessionIdentityStore({
    String? userRef,
    String? deviceId,
    Uuid? uuid,
  }) {
    _userRef = userRef;
    _deviceId = deviceId;
    _uuid = uuid ?? const Uuid();
  }

  late final Uuid _uuid;
  String? _userRef;
  String? _deviceId;

  @override
  String? get userRef => _userRef;

  @override
  String? get deviceId => _deviceId;

  @override
  Future<void> load() async {}

  @override
  Future<void> saveUserRef(String userRef) async {
    _userRef = userRef;
  }

  @override
  Future<String?> readUserRef() async => _userRef;

  @override
  Future<String> getOrCreateDeviceId() async {
    _deviceId ??= _uuid.v4();
    return _deviceId!;
  }

  @override
  Future<String?> readDeviceId() async => _deviceId;
}
