import 'package:banca_online/core/session/secure_key_value_storage.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:flutter_test/flutter_test.dart';

/// Storage que siempre falla (simula Keystore/Keychain no disponible).
class _ThrowingStorage implements SecureKeyValueStorage {
  @override
  Future<String?> read(String key) async => throw StateError('keystore down');

  @override
  Future<void> write(String key, String value) async =>
      throw StateError('keystore down');

  @override
  Future<void> delete(String key) async => throw StateError('keystore down');
}

void main() {
  test('sin datos: readUserRef/readDeviceId devuelven null', () async {
    final store = SecureSessionIdentityStore(storage: InMemorySecureStorage());
    await store.load();

    expect(store.userRef, isNull);
    expect(store.deviceId, isNull);
    expect(await store.readUserRef(), isNull);
    expect(await store.readDeviceId(), isNull);
  });

  test('saveUserRef persiste y load() hidrata el cache', () async {
    final storage = InMemorySecureStorage();
    final store = SecureSessionIdentityStore(storage: storage);

    await store.saveUserRef('user-1');

    expect(store.userRef, 'user-1');
    expect(await store.readUserRef(), 'user-1');
    expect(storage.debugValues[SecureSessionIdentityStore.userRefKey],
        'user-1');

    // Reinicio de app: instancia nueva no ve nada hasta load().
    final reopened = SecureSessionIdentityStore(storage: storage);
    expect(reopened.userRef, isNull);
    await reopened.load();
    expect(reopened.userRef, 'user-1');
  });

  test('saveUserRef sobreescribe: una sola cuenta activa por dispositivo',
      () async {
    final storage = InMemorySecureStorage();
    final store = SecureSessionIdentityStore(storage: storage);

    await store.saveUserRef('user-1');
    await store.saveUserRef('user-2');

    expect(store.userRef, 'user-2');
    expect(await store.readUserRef(), 'user-2');
    expect(storage.debugValues[SecureSessionIdentityStore.userRefKey],
        'user-2');
  });

  test('getOrCreateDeviceId es idempotente y persiste', () async {
    final storage = InMemorySecureStorage();
    final store = SecureSessionIdentityStore(storage: storage);

    final first = await store.getOrCreateDeviceId();
    expect(first, isNotEmpty);
    expect(await store.getOrCreateDeviceId(), first);
    expect(await store.readDeviceId(), first);
    expect(store.deviceId, first);
    expect(storage.debugValues[SecureSessionIdentityStore.deviceIdKey], first);

    // Reinicio de app: el id se reutiliza, no se regenera.
    final reopened = SecureSessionIdentityStore(storage: storage);
    await reopened.load();
    expect(await reopened.getOrCreateDeviceId(), first);
  });

  test('user_ref y device_id conviven en el mismo storage seguro', () async {
    final storage = InMemorySecureStorage();
    final store = SecureSessionIdentityStore(storage: storage);

    await store.saveUserRef('user-1');
    final deviceId = await store.getOrCreateDeviceId();

    expect(store.userRef, 'user-1');
    expect(store.deviceId, deviceId);
  });

  test('InMemorySessionIdentityStore es estable en getOrCreateDeviceId',
      () async {
    final store = InMemorySessionIdentityStore();

    final first = await store.getOrCreateDeviceId();
    expect(await store.getOrCreateDeviceId(), first);
    expect(await store.readDeviceId(), first);
  });

  test('getOrCreateDeviceId tolera fallo del secure storage (fallback memoria)',
      () async {
    final store = SecureSessionIdentityStore(storage: _ThrowingStorage());

    final id = await store.getOrCreateDeviceId();
    expect(id, isNotEmpty);
    expect(store.deviceId, id);
    // Idempotente dentro del proceso aunque no se pudo persistir.
    expect(await store.getOrCreateDeviceId(), id);
  });

  test('load tolera fallo del secure storage y deja el cache vacio', () async {
    final store = SecureSessionIdentityStore(storage: _ThrowingStorage());

    await store.load();

    expect(store.userRef, isNull);
    expect(store.deviceId, isNull);
  });

  test('SecureSessionIdentityStore notifica en saveUserRef (Listenable)',
      () async {
    final store =
        SecureSessionIdentityStore(storage: InMemorySecureStorage());
    addTearDown(store.dispose);
    var notified = 0;
    store.addListener(() => notified++);

    await store.saveUserRef('user-1');

    expect(store.userRef, 'user-1');
    expect(notified, greaterThan(0));
  });

  test('SecureSessionIdentityStore notifica en load (hidratacion)', () async {
    final storage = InMemorySecureStorage();
    final writer = SecureSessionIdentityStore(storage: storage);
    await writer.saveUserRef('user-1');

    final store = SecureSessionIdentityStore(storage: storage);
    addTearDown(store.dispose);
    var notified = 0;
    store.addListener(() => notified++);

    await store.load();

    expect(store.userRef, 'user-1');
    expect(notified, greaterThan(0));
  });

  test('InMemorySessionIdentityStore notifica en saveUserRef (Listenable)',
      () async {
    final store = InMemorySessionIdentityStore();
    addTearDown(store.dispose);
    var notified = 0;
    store.addListener(() => notified++);

    await store.saveUserRef('u-1');

    expect(store.userRef, 'u-1');
    expect(notified, greaterThan(0));
  });

  test('F-T49: sin sync el flag biometrico es null en ambos stores', () async {
    final secure = SecureSessionIdentityStore(storage: InMemorySecureStorage());
    await secure.load();
    expect(secure.biometricEnabled, isNull);
    expect(await secure.readBiometricEnabled(), isNull);

    final memory = InMemorySessionIdentityStore();
    expect(memory.biometricEnabled, isNull);
    expect(await memory.readBiometricEnabled(), isNull);
  });

  test('F-T49: saveBiometricEnabled persiste con la clave biometric.enabled',
      () async {
    final storage = InMemorySecureStorage();
    final store = SecureSessionIdentityStore(storage: storage);

    await store.saveBiometricEnabled(true);

    expect(store.biometricEnabled, isTrue);
    expect(await store.readBiometricEnabled(), isTrue);
    expect(
      storage.debugValues[SecureSessionIdentityStore.biometricEnabledKey],
      'true',
    );

    await store.saveBiometricEnabled(false);

    expect(store.biometricEnabled, isFalse);
    expect(await store.readBiometricEnabled(), isFalse);
    expect(
      storage.debugValues[SecureSessionIdentityStore.biometricEnabledKey],
      'false',
    );
  });

  test('F-T49: load() hidrata el flag biometrico', () async {
    final storage = InMemorySecureStorage();
    final writer = SecureSessionIdentityStore(storage: storage);
    await writer.saveUserRef('user-1');
    await writer.saveBiometricEnabled(true);

    final reopened = SecureSessionIdentityStore(storage: storage);
    expect(reopened.biometricEnabled, isNull);
    await reopened.load();

    expect(reopened.userRef, 'user-1');
    expect(reopened.biometricEnabled, isTrue);
    expect(await reopened.readBiometricEnabled(), isTrue);
  });

  test('F-T49: InMemorySessionIdentityStore soporta el flag (ctor + save)',
      () async {
    final store = InMemorySessionIdentityStore(biometricEnabled: true);
    addTearDown(store.dispose);
    expect(store.biometricEnabled, isTrue);
    expect(await store.readBiometricEnabled(), isTrue);

    var notified = 0;
    store.addListener(() => notified++);
    await store.saveBiometricEnabled(false);

    expect(store.biometricEnabled, isFalse);
    expect(await store.readBiometricEnabled(), isFalse);
    expect(notified, greaterThan(0));
  });

  test('F-T49: load tolera el flag ausente y deja el cache en null', () async {
    final store = SecureSessionIdentityStore(storage: _ThrowingStorage());

    await store.load();

    expect(store.biometricEnabled, isNull);
  });
}
