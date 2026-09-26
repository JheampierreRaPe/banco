import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Abstraccion testeable sobre almacenamiento seguro (F-T02).
///
/// La implementacion real delega en `flutter_secure_storage` (Keychain /
/// Keystore cifrado); los tests usan [InMemorySecureStorage] para no tocar
/// canales de plataforma. Tokens y clave de dispositivo NUNCA en texto
/// plano ni en logs (docs/16 reglas 7 y 10).
abstract class SecureKeyValueStorage {
  Future<String?> read(String key);
  Future<void> write(String key, String value);
  Future<void> delete(String key);
}

/// Adaptador de produccion sobre `flutter_secure_storage`.
class FlutterSecureStorageAdapter implements SecureKeyValueStorage {
  FlutterSecureStorageAdapter({FlutterSecureStorage? storage})
      : _storage = storage ?? const FlutterSecureStorage();

  final FlutterSecureStorage _storage;

  // Defaults v11: AES/GCM + RSA-OAEP en Keystore (cifrado fuerte).
  static const _androidOptions = AndroidOptions();

  @override
  Future<String?> read(String key) =>
      _storage.read(key: key, aOptions: _androidOptions);

  @override
  Future<void> write(String key, String value) =>
      _storage.write(key: key, value: value, aOptions: _androidOptions);

  @override
  Future<void> delete(String key) =>
      _storage.delete(key: key, aOptions: _androidOptions);
}

/// Almacenamiento en memoria (solo tests de F-T02).
class InMemorySecureStorage implements SecureKeyValueStorage {
  final Map<String, String> _values = {};

  @override
  Future<String?> read(String key) async => _values[key];

  @override
  Future<void> write(String key, String value) async {
    _values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    _values.remove(key);
  }

  /// Vista de solo lectura para aserciones (nunca loguear en produccion).
  @visibleForTesting
  Map<String, String> get debugValues => Map.unmodifiable(_values);
}
