// Lector biometrico real con `local_auth` (F-T46, CA-01/CA-05).
//
// - `isAvailable()` refleja el dispositivo (sin hardware/no enrolado ->
//   `false`); cualquier excepcion del plugin -> `false` sin lanzar.
// - `authenticate` OK -> autenticado; cancelado/denegado/error/excepcion ->
//   `requiresPinFallback` sin lanzar ni bloquear (sin lockout: el PIN es el
//   fallback).
// - Los seams inyectados evitan tocar canales de plataforma en tests;
//   [FakeBiometricReader] se mantiene intacto para el resto de la suite.
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('SystemBiometricReader.isAvailable', () {
    test('disponible cuando hay hardware y biometria enrolada', () async {
      final reader = SystemBiometricReader(
        canCheckBiometrics: () async => true,
        isDeviceSupported: () async => true,
      );

      expect(await reader.isAvailable(), isTrue);
    });

    test('sin hardware (canCheck=false) -> no disponible', () async {
      final reader = SystemBiometricReader(
        canCheckBiometrics: () async => false,
        isDeviceSupported: () async => true,
      );

      expect(await reader.isAvailable(), isFalse);
    });

    test('dispositivo no soportado -> no disponible', () async {
      final reader = SystemBiometricReader(
        canCheckBiometrics: () async => true,
        isDeviceSupported: () async => false,
      );

      expect(await reader.isAvailable(), isFalse);
    });

    test('excepcion del plugin -> no disponible sin lanzar', () async {
      final reader = SystemBiometricReader(
        canCheckBiometrics: () async => throw Exception('sin canal'),
      );

      expect(await reader.isAvailable(), isFalse);
    });
  });

  group('SystemBiometricReader.authenticate (fallback a PIN sin lockout)',
      () {
    test('desafio superado -> autenticado', () async {
      var seenReason = '';
      final reader = SystemBiometricReader(
        authenticateWithReason: ({required reason}) async {
          seenReason = reason;
          return true;
        },
      );

      final result = await reader.authenticate(reason: 'Entra a tu cuenta');

      expect(result.authenticated, isTrue);
      expect(result.requiresPinFallback, isFalse);
      expect(seenReason, 'Entra a tu cuenta');
    });

    test('cancelado por el usuario (false) -> fallback a PIN', () async {
      final reader = SystemBiometricReader(
        authenticateWithReason: ({required reason}) async => false,
      );

      final result = await reader.authenticate(reason: 'Entra a tu cuenta');

      expect(result.authenticated, isFalse);
      expect(result.requiresPinFallback, isTrue);
    });

    test('denegado (false) -> fallback a PIN', () async {
      final reader = SystemBiometricReader(
        authenticateWithReason: ({required reason}) async => false,
      );

      final result = await reader.authenticate(reason: 'x');

      expect(result.requiresPinFallback, isTrue);
    });

    test('excepcion del plugin -> fallback sin lanzar', () async {
      final reader = SystemBiometricReader(
        authenticateWithReason: ({required reason}) async =>
            throw Exception('auth fallida'),
      );

      final result = await reader.authenticate(reason: 'x');

      expect(result.requiresPinFallback, isTrue);
    });

    test('sin seams (plugin real ausente en tests) -> fallback sin lanzar',
        () async {
      final reader = SystemBiometricReader();

      expect(await reader.isAvailable(), isFalse);
      final result = await reader.authenticate(reason: 'x');
      expect(result.requiresPinFallback, isTrue);
    });
  });

  group('FakeBiometricReader (compatibilidad)', () {
    test('sigue disponible para tests y desarrollo', () async {
      final reader = FakeBiometricReader(available: true, succeeds: true);

      expect(await reader.isAvailable(), isTrue);
      final result = await reader.authenticate(reason: 'x');
      expect(result.authenticated, isTrue);
      expect(reader.authenticateCalls, 1);
    });

    test('no disponible -> fallback', () async {
      final reader = FakeBiometricReader(available: false);

      expect(await reader.isAvailable(), isFalse);
      expect(
        (await reader.authenticate(reason: 'x')).requiresPinFallback,
        isTrue,
      );
    });
  });
}
