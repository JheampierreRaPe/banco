import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/kyc/kyc_error_handler.dart';
import 'package:flutter_test/flutter_test.dart';

ApiException _apiError({
  required String code,
  String serverMessage = 'detalle del servidor',
  int? statusCode,
  String? requestId,
}) =>
    ApiException(
      code: code,
      message: 'mensaje ui base',
      serverMessage: serverMessage,
      statusCode: statusCode,
      requestId: requestId,
    );

void main() {
  group('KycErrorHandler.isTokenExpired', () {
    test('401 siempre es token expirado aunque el code sea generico', () {
      expect(
        KycErrorHandler.isTokenExpired(
          _apiError(code: 'NOT_AUTHORIZED', statusCode: 401),
        ),
        isTrue,
      );
    });

    test('codes con EXPIRED son token expirado sin importar el status', () {
      for (final code in [
        'TOKEN_EXPIRED',
        'CHALLENGE_EXPIRED',
        'RATE_EXPIRED_X',
      ]) {
        expect(
          KycErrorHandler.isTokenExpired(_apiError(code: code)),
          isTrue,
          reason: code,
        );
      }
    });

    test('error de red NO es token expirado', () {
      expect(
        KycErrorHandler.isTokenExpired(ApiException.network()),
        isFalse,
      );
    });
  });

  group('KycErrorHandler.isChallengeExpiredByTtl', () {
    test('TTL local agotado -> expirado', () {
      final issued = DateTime.utc(2026, 1, 1, 0, 0, 0);
      expect(
        KycErrorHandler.isChallengeExpiredByTtl(
          issuedAt: issued,
          expiresInSeconds: 300,
          now: issued.add(const Duration(seconds: 301)),
        ),
        isTrue,
      );
      expect(
        KycErrorHandler.isChallengeExpiredByTtl(
          issuedAt: issued,
          expiresInSeconds: 300,
          now: issued.add(const Duration(seconds: 10)),
        ),
        isFalse,
      );
    });

    test('sin issuedAt o expiresIn -> no expirado', () {
      expect(
        KycErrorHandler.isChallengeExpiredByTtl(
          issuedAt: null,
          expiresInSeconds: 300,
        ),
        isFalse,
      );
    });
  });

  group('KycErrorHandler.fromApiException', () {
    test('token expirado pide challenge nuevo conservando el mensaje base', () {
      final info = KycErrorHandler.fromApiException(
        _apiError(code: 'TOKEN_EXPIRED', statusCode: 401),
      );
      expect(info.action, KycErrorAction.refreshChallenge);
      expect(info.message, contains('nuevo'));
      // El motivo del servidor se conserva (no se pierde en un generico).
      expect(info.serverReason, 'detalle del servidor');
    });

    test('RISK_BLOCKED deriva directo a revision manual con folio', () {
      final info = KycErrorHandler.fromApiException(
        _apiError(code: 'RISK_BLOCKED', statusCode: 403),
      );
      expect(info.action, KycErrorAction.manualReview);
      expect(info.folio, startsWith('KYC-'));
    });

    test('con intentos restantes sugiere reintentar', () {
      final info = KycErrorHandler.fromApiException(
        ApiException.network(),
        task: 'blink',
        attempts: 1,
      );
      expect(info.action, KycErrorAction.retryTask);
      expect(info.message, contains('Sin conexion'));
    });

    test('intentos agotados derivan a revision manual con folio', () {
      final info = KycErrorHandler.fromApiException(
        ApiException.network(),
        task: 'blink',
        attempts: 3,
      );
      expect(info.action, KycErrorAction.manualReview);
      expect(info.folio, startsWith('KYC-'));
    });

    test('limite configurable respeta maxAttempts distinto del default', () {
      final retry = KycErrorHandler.fromApiException(
        ApiException.network(),
        task: 'blink',
        attempts: 4,
        maxAttempts: 5,
      );
      expect(retry.action, KycErrorAction.retryTask);
      final manual = KycErrorHandler.fromApiException(
        ApiException.network(),
        task: 'blink',
        attempts: 5,
        maxAttempts: 5,
      );
      expect(manual.action, KycErrorAction.manualReview);
    });
  });

  group('KycErrorHandler.forTaskNotPassed', () {
    test('passed:false NO es error HTTP: permite reintento de la misma tarea',
        () {
      final info = KycErrorHandler.forTaskNotPassed(
        task: 'blink',
        attempts: 1,
      );
      expect(info.code, 'TASK_NOT_PASSED');
      expect(info.action, KycErrorAction.retryTask);
      expect(info.message, contains('misma tarea'));
      expect(info.attemptsExhausted, isFalse);
    });

    test('default 3 intentos: al tercero deriva a revision manual', () {
      expect(KycErrorHandler.defaultMaxAttemptsPerTask, 3);
      final info = KycErrorHandler.forTaskNotPassed(
        task: 'blink',
        attempts: 3,
      );
      expect(info.action, KycErrorAction.manualReview);
      expect(info.folio, startsWith('KYC-'));
      expect(info.attemptsExhausted, isTrue);
    });

    test('conserva el motivo del servidor cuando viene informado', () {
      final info = KycErrorHandler.forTaskNotPassed(
        task: 'blink',
        attempts: 1,
        serverReason: 'LOW_QUALITY',
      );
      expect(info.serverReason, 'LOW_QUALITY');
    });
  });

  group('KycErrorHandler.newManualReviewFolio', () {
    test('folio con formato KYC-...', () {
      final folio = KycErrorHandler.newManualReviewFolio(
        now: DateTime.utc(2026, 9, 18, 12, 0, 0),
      );
      expect(folio, startsWith('KYC-'));
      expect(folio, contains('20260918'));
    });
  });
}
