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

  test('clearOnInvalidRefresh borra todo y rota el device secret', () async {
    final storage = InMemorySecureStorage();
    final repo = SecureSessionRepository(storage: storage);

    final before = await repo.getOrCreateDeviceSecret();
    await repo.saveSession(accessToken: 'a', refreshToken: 'r');
    await repo.clearOnInvalidRefresh();

    expect(repo.isAuthenticated, isFalse);
    expect(await repo.readRefreshToken(), isNull);
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
