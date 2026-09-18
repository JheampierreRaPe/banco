// Pruebas del servicio de biometria + firma (F-T03).
//
// - HMAC-SHA256 en Dart puro verificado contra el vector conocido.
// - `signNonce` usa el `device.secret` de F-T02 y emite el formato exacto
//   del backend (64 hex minusculas, `test_device_login.py::_sign_hmac`).
// - Fallback a PIN senalado cuando la biometria no esta disponible.
import 'dart:convert';

import 'package:banca_online/core/session/secure_key_value_storage.dart';
import 'package:banca_online/core/session/secure_session_repository.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/biometrics/biometric_service.dart';
import 'package:banca_online/features/biometrics/hmac_sha256.dart';
import 'package:flutter_test/flutter_test.dart';

BiometricService _serviceWithSecret(
  String secret, {
  FakeBiometricReader? reader,
}) {
  final storage = InMemorySecureStorage();
  final session = SecureSessionRepository(storage: storage);
  // Siembra directa del secreto (mismo canal que F-T02).
  return _SeededBiometricService(session, reader ?? FakeBiometricReader(),
      seed: secret, storage: storage);
}

class _SeededBiometricService extends BiometricService {
  _SeededBiometricService(
    SecureSessionRepository session,
    BiometricReader reader, {
    required this._seed,
    required this._storage,
  }) : super(session: session, reader: reader);

  final String _seed;
  final InMemorySecureStorage _storage;
  bool _seeded = false;

  @override
  Future<String> signNonce(String nonce) async {
    if (!_seeded) {
      await _storage.write(SecureSessionRepository.deviceSecretKey, _seed);
      _seeded = true;
    }
    return super.signNonce(nonce);
  }
}

void main() {
  group('hmac_sha256 (Dart puro)', () {
    test('vector conocido RFC 4231 / Wikipedia', () {
      final got = hmacSha256Hex(
        utf8.encode('key'),
        utf8.encode('The quick brown fox jumps over the lazy dog'),
      );
      expect(
        got,
        'f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8',
      );
    });

    test('sha256("abc") coincide con el digest oficial', () {
      final digest = sha256(utf8.encode('abc'));
      final hex =
          digest.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
      expect(hex, 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    });
  });

  group('BiometricService.signNonce', () {
    test('con secreto F-T02 (base64 32B): 64 hex, determinista, con HMAC', () async {
      final storage = InMemorySecureStorage();
      final session = SecureSessionRepository(storage: storage);
      final secret = await session.getOrCreateDeviceSecret();
      expect(base64Url.decode(secret), hasLength(32));

      final service = BiometricService(
        session: session,
        reader: FakeBiometricReader(),
      );
      final sig1 = await service.signNonce('nonce-del-challenge-123');
      final sig2 = await service.signNonce('nonce-del-challenge-123');

      expect(sig1, hasLength(64));
      expect(BiometricService.isValidSignatureFormat(sig1), isTrue);
      expect(sig1, sig2, reason: 'misma clave + mismo nonce = misma firma');

      // Prueba criptografica: la firma es HMAC-SHA256(nonce) con el secreto.
      final expected = hmacSha256Hex(base64Url.decode(secret), utf8.encode('nonce-del-challenge-123'));
      expect(sig1, expected);

      // Otro nonce -> otra firma (el nonce es de un solo uso).
      expect(await service.signNonce('otro-nonce'), isNot(equals(sig1)));
    });

    test('secreto "hmac:<hex>" del backend usa esos bytes como clave', () async {
      const hex = 'aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899';
      final service = _serviceWithSecret('hmac:$hex');
      final got = await service.signNonce('n-1');
      final key = List<int>.generate(
        32,
        (i) => int.parse(hex.substring(i * 2, i * 2 + 2), radix: 16),
      );
      expect(got, hmacSha256Hex(key, utf8.encode('n-1')));
    });

    test('nonce vacio lanza ArgumentError y nunca se firma', () async {
      final storage = InMemorySecureStorage();
      final service = BiometricService(
        session: SecureSessionRepository(storage: storage),
        reader: FakeBiometricReader(),
      );
      expect(() => service.signNonce(''), throwsArgumentError);
    });
  });

  group('BiometricService.isValidSignatureFormat', () {
    test('acepta 64 hex en minusculas', () {
      expect(BiometricService.isValidSignatureFormat('ab12' * 16), isTrue);
      expect(BiometricService.isValidSignatureFormat('00' * 32), isTrue);
    });

    test('rechaza formatos invalidos antes de enviar', () {
      expect(BiometricService.isValidSignatureFormat(''), isFalse);
      expect(BiometricService.isValidSignatureFormat('corta'), isFalse);
      expect(BiometricService.isValidSignatureFormat('ab12' * 15), isFalse,
          reason: '63 chars');
      expect(BiometricService.isValidSignatureFormat('${'ab12' * 16}00'), isFalse,
          reason: '66 chars');
      expect(BiometricService.isValidSignatureFormat('AB12' * 16), isFalse,
          reason: 'mayusculas: hexdigest() del backend es minusculas');
      expect(BiometricService.isValidSignatureFormat('zz12' * 16), isFalse,
          reason: 'fuera de [0-9a-f]');
      expect(BiometricService.isValidSignatureFormat('0x${'ab' * 31}'), isFalse,
          reason: 'prefijo 0x');
    });
  });

  group('BiometricService.authenticate (fallback PIN)', () {
    test('biometria disponible y aprobada', () async {
      final service = BiometricService(
        session: SecureSessionRepository(storage: InMemorySecureStorage()),
        reader: FakeBiometricReader(available: true, succeeds: true),
      );
      final result = await service.authenticate(reason: 'Confirma tu identidad');
      expect(result.authenticated, isTrue);
      expect(result.requiresPinFallback, isFalse);
    });

    test('biometria no disponible -> senala fallback a PIN', () async {
      final reader = FakeBiometricReader(available: false);
      final service = BiometricService(
        session: SecureSessionRepository(storage: InMemorySecureStorage()),
        reader: reader,
      );
      final result = await service.authenticate(reason: 'Confirma tu identidad');
      expect(result.authenticated, isFalse);
      expect(result.requiresPinFallback, isTrue);
      expect(reader.lastReason, 'Confirma tu identidad');
    });

    test('biometria fallida/cancelada -> senala fallback a PIN', () async {
      final service = BiometricService(
        session: SecureSessionRepository(storage: InMemorySecureStorage()),
        reader: FakeBiometricReader(available: true, succeeds: false),
      );
      final result = await service.authenticate(reason: 'Confirma tu identidad');
      expect(result.requiresPinFallback, isTrue);
    });

    test('SystemBiometricReader (sin local_auth) reporta no disponible', () async {
      final service = BiometricService(
        session: SecureSessionRepository(storage: InMemorySecureStorage()),
        reader: SystemBiometricReader(),
      );
      expect(await service.isAvailable(), isFalse);
      final result = await service.authenticate(reason: 'x');
      expect(result.requiresPinFallback, isTrue);
    });
  });
}
