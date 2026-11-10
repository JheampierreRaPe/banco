import 'dart:convert';

import 'package:banca_online/core/session/secure_key_value_storage.dart';
import 'package:banca_online/core/session/secure_session_repository.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('saveSession persiste access+refresh y load() hidrata el caché',
      () async {
    final storage = InMemorySecureStorage();
    final repo = SecureSessionRepository(storage: storage);

    await repo.saveSession(accessToken: 'acc-1', refreshToken: 'ref-1');

    expect(repo.currentAccessToken, 'acc-1');
    expect(repo.isAuthenticated, isTrue);
    expect(await repo.readRefreshToken(), 'ref-1');
    expect(storage.debugValues[SecureSessionRepository.accessTokenKey],
        'acc-1');
    expect(storage.debugValues[SecureSessionRepository.refreshTokenKey],
        'ref-1');

    // Una instancia nueva (reinicio de app) no ve nada hasta load().
    final reopened = SecureSessionRepository(storage: storage);
    expect(reopened.isAuthenticated, isFalse);
    await reopened.load();
    expect(reopened.currentAccessToken, 'acc-1');
    expect(reopened.isAuthenticated, isTrue);
    expect(await reopened.readRefreshToken(), 'ref-1');
  });

  test('sin sesión: no autenticado y sin refresh', () async {
    final repo = SecureSessionRepository(storage: InMemorySecureStorage());
    await repo.load();

    expect(repo.isAuthenticated, isFalse);
    expect(repo.currentAccessToken, isNull);
    expect(await repo.readRefreshToken(), isNull);
  });

  test('device secret: estable, 32 bytes, sobrevive a logout', () async {
    final repo = SecureSessionRepository(storage: InMemorySecureStorage());

    final first = await repo.getOrCreateDeviceSecret();
    expect(await repo.getOrCreateDeviceSecret(), first);
    expect(base64Url.decode(first), hasLength(32));

    await repo.saveSession(accessToken: 'a', refreshToken: 'r');
    await repo.clearOnLogout();

    expect(repo.isAuthenticated, isFalse);
    expect(await repo.readRefreshToken(), isNull);
    expect(await repo.getOrCreateDeviceSecret(), first);
  });

  test('device binding key: estable, formato hmac:<hex> y no expone secreto',
      () async {
    final repo = SecureSessionRepository(storage: InMemorySecureStorage());

    final first = await repo.getOrCreateDeviceBindingKey();

    // Idempotente y formato exacto que espera el backend (64 hex minusculas).
    expect(await repo.getOrCreateDeviceBindingKey(), first);
    expect(first, matches(RegExp(r'^hmac:[0-9a-f]{64}$')));

    // Derivada de los MISMOS bytes que firma F-T03 (base64url de F-T02).
    final secret = await repo.getOrCreateDeviceSecret();
    final expectedHex = base64Url
        .decode(secret)
        .map((b) => b.toRadixString(16).padLeft(2, '0'))
        .join();
    expect(first, 'hmac:$expectedHex');
    expect(first, isNot(contains(secret)));
  });

  test('binding key se conserva por defecto en clearOnInvalidRefresh',
      () async {
    final repo = SecureSessionRepository(storage: InMemorySecureStorage());
    final before = await repo.getOrCreateDeviceBindingKey();

    // F-T53: el default es benigno (rotateDeviceKey: false) y conserva.
    await repo.clearOnInvalidRefresh();

    final after = await repo.getOrCreateDeviceBindingKey();
    expect(after, before);
    expect(after, matches(RegExp(r'^hmac:[0-9a-f]{64}$')));
  });

  test('binding key rota solo con rotateDeviceKey: true', () async {
    final repo = SecureSessionRepository(storage: InMemorySecureStorage());
    final before = await repo.getOrCreateDeviceBindingKey();

    await repo.clearOnInvalidRefresh(rotateDeviceKey: true);

    final after = await repo.getOrCreateDeviceBindingKey();
    expect(after, isNot(before));
    expect(after, matches(RegExp(r'^hmac:[0-9a-f]{64}$')));
  });

  test('clearOnInvalidRefresh por defecto conserva el device secret', () async {
    final storage = InMemorySecureStorage();
    final repo = SecureSessionRepository(storage: storage);

    final before = await repo.getOrCreateDeviceSecret();
    await repo.saveSession(accessToken: 'a', refreshToken: 'r');
    var notified = 0;
    repo.addListener(() => notified++);
    await repo.clearOnInvalidRefresh();

    expect(repo.isAuthenticated, isFalse);
    expect(await repo.readRefreshToken(), isNull);
    expect(notified, 1);
    // Fallo benigno: la clave se conserva (mismo secreto).
    expect(
        storage.debugValues[SecureSessionRepository.deviceSecretKey], before);
    expect(await repo.getOrCreateDeviceSecret(), before);
  });

  test('clearOnInvalidRefresh con rotateDeviceKey: true rota el secret',
      () async {
    final storage = InMemorySecureStorage();
    final repo = SecureSessionRepository(storage: storage);

    final before = await repo.getOrCreateDeviceSecret();
    await repo.saveSession(accessToken: 'a', refreshToken: 'r');
    var notified = 0;
    repo.addListener(() => notified++);
    await repo.clearOnInvalidRefresh(rotateDeviceKey: true);

    expect(repo.isAuthenticated, isFalse);
    expect(await repo.readRefreshToken(), isNull);
    expect(notified, 1);
    expect(
        storage.debugValues.containsKey(
            SecureSessionRepository.deviceSecretKey),
        isFalse);

    final rotated = await repo.getOrCreateDeviceSecret();
    expect(rotated, isNot(before));
    expect(base64Url.decode(rotated), hasLength(32));
  });

  test('notifica a listeners en save/load/clear (go_router reacciona)',
      () async {
    final repo = SecureSessionRepository(storage: InMemorySecureStorage());
    var calls = 0;
    repo.addListener(() => calls++);

    await repo.saveSession(accessToken: 'a', refreshToken: 'r');
    await repo.load();
    await repo.clear();

    expect(calls, 3);
  });
}
