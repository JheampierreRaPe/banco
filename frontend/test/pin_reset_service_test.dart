// Pruebas del servicio HTTP de restablecimiento de PIN (F-T43, contrato
// E1-T34/SCR-005 + OTP `RECOVERY` de E1-T31).
//
// - `requestOtp` envía SOLO `{email}` a `/auth/recovery/request` (canónico
//   de recovery; nunca `/auth/otp/resend`) y parsea
//   `accepted`/`ttl_seconds`/`resend_wait_seconds`.
// - `resetPin` envía `{email, doc_number, code, pin}` a `/auth/pin-reset` y
//   parsea SOLO `{user_ref, pin_set}` (sin sesión ni tokens; la única sesión
//   la abre `POST /auth/login/pin`).
// - Errores mapeados por la capa HTTP (F-T01): `401 INVALID_PIN_RESET`
//   genérico y `429 RATE_LIMITED`, `422` de esquema.
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/pin_reset/pin_reset_service.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:uuid/uuid.dart';

Map<String, dynamic>? _capturedBody;
String? _capturedPath;
int _statusToReturn = 200;
Map<String, dynamic> _dataToReturn = const {};

ApiClient _api(InMemorySessionRepository session) {
  final dio = Dio();
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        _capturedPath = options.path;
        _capturedBody = options.data is Map
            ? Map<String, dynamic>.from(options.data as Map)
            : null;
        if (_statusToReturn >= 400) {
          handler.reject(
            DioException(
              requestOptions: options,
              type: DioExceptionType.badResponse,
              response: Response(
                requestOptions: options,
                statusCode: _statusToReturn,
                data: {'error': _dataToReturn},
              ),
            ),
          );
          return;
        }
        handler.resolve(
          Response(
            requestOptions: options,
            statusCode: 200,
            data: {
              'data': _dataToReturn,
              'meta': {'request_id': 'r-1'},
            },
          ),
        );
      },
    ),
  );
  return ApiClient.create(
    session: session,
    dioOverride: dio,
    refreshDioOverride: Dio(),
    uuid: const Uuid(),
  );
}

void main() {
  late InMemorySessionRepository session;

  setUp(() {
    session = InMemorySessionRepository();
    _capturedBody = null;
    _capturedPath = null;
    _statusToReturn = 200;
    _dataToReturn = const {};
  });

  group('requestOtp (OTP RECOVERY canónico)', () {
    test('envía SOLO {email} a /auth/recovery/request y parsea', () async {
      _dataToReturn = {
        'accepted': true,
        'ttl_seconds': 600,
        'resend_wait_seconds': 30,
      };
      final service = HttpPinResetService(api: _api(session));

      final result = await service.requestOtp(email: 'a@b.com');

      expect(_capturedPath, '/auth/recovery/request');
      // El DNI nunca viaja en el request del OTP (solo en el final).
      expect(_capturedBody, {'email': 'a@b.com'});
      expect(result.accepted, isTrue);
      expect(result.ttlSeconds, 600);
      expect(result.resendWaitSeconds, 30);
    });

    test('nunca usa /auth/otp/resend (ese es de activación)', () async {
      _dataToReturn = {
        'accepted': true,
        'ttl_seconds': 600,
        'resend_wait_seconds': 30,
      };
      final service = HttpPinResetService(api: _api(session));

      await service.requestOtp(email: 'a@b.com');

      expect(_capturedPath, isNot(contains('otp/resend')));
    });

    test('misma respuesta neutra para email existente y no existente',
        () async {
      _dataToReturn = {
        'accepted': true,
        'ttl_seconds': 600,
        'resend_wait_seconds': 30,
      };
      final service = HttpPinResetService(api: _api(session));

      final existing = await service.requestOtp(email: 'existe@banco.com');
      final missing = await service.requestOtp(email: 'nadie@banco.com');

      expect(existing.accepted, missing.accepted);
      expect(existing.ttlSeconds, missing.ttlSeconds);
      expect(existing.resendWaitSeconds, missing.resendWaitSeconds);
    });

    test('429 propaga RATE_LIMITED por la capa HTTP', () async {
      _statusToReturn = 429;
      _dataToReturn = {'code': 'RATE_LIMITED', 'message': 'interno'};
      final service = HttpPinResetService(api: _api(session));

      expect(
        () => service.requestOtp(email: 'a@b.com'),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'RATE_LIMITED'),
        ),
      );
    });
  });

  group('resetPin (POST /auth/pin-reset)', () {
    test('envía {email, doc_number, code, pin} y parsea user_ref/pin_set',
        () async {
      _dataToReturn = {'user_ref': 'user-123', 'pin_set': true};
      final service = HttpPinResetService(api: _api(session));

      final result = await service.resetPin(
        email: 'a@b.com',
        docNumber: '12345678',
        code: '654321',
        pin: '482916',
      );

      expect(_capturedPath, '/auth/pin-reset');
      expect(_capturedBody, {
        'email': 'a@b.com',
        'doc_number': '12345678',
        'code': '654321',
        'pin': '482916',
      });
      expect(result.userRef, 'user-123');
      expect(result.pinSet, isTrue);
    });

    test('la respuesta no abre sesión ni trae tokens (E1-T34)', () async {
      _dataToReturn = {'user_ref': 'user-123', 'pin_set': true};
      final service = HttpPinResetService(api: _api(session));

      await service.resetPin(
        email: 'a@b.com',
        docNumber: '12345678',
        code: '654321',
        pin: '482916',
      );

      // El servicio solo expone `user_ref`/`pin_set`: si el backend volviera
      // a emitir tokens, el modelo ya no los consume.
      expect(_dataToReturn.containsKey('access_token'), isFalse);
      expect(_dataToReturn.containsKey('refresh_token'), isFalse);
      expect(_dataToReturn.containsKey('session_id'), isFalse);
    });

    test('sin user_ref lanza UNKNOWN (contrato roto)', () async {
      _dataToReturn = {'pin_set': true};
      final service = HttpPinResetService(api: _api(session));

      expect(
        () => service.resetPin(
          email: 'a@b.com',
          docNumber: '12345678',
          code: '654321',
          pin: '482916',
        ),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'UNKNOWN'),
        ),
      );
    });

    test('401 propaga INVALID_PIN_RESET (único error genérico)', () async {
      _statusToReturn = 401;
      _dataToReturn = {'code': 'INVALID_PIN_RESET', 'message': 'interno'};
      final service = HttpPinResetService(api: _api(session));

      expect(
        () => service.resetPin(
          email: 'a@b.com',
          docNumber: '12345678',
          code: '000000',
          pin: '482916',
        ),
        throwsA(
          isA<ApiException>().having(
            (e) => e.code,
            'code',
            'INVALID_PIN_RESET',
          ),
        ),
      );
    });

    test('429 propaga RATE_LIMITED', () async {
      _statusToReturn = 429;
      _dataToReturn = {'code': 'RATE_LIMITED', 'message': 'interno'};
      final service = HttpPinResetService(api: _api(session));

      expect(
        () => service.resetPin(
          email: 'a@b.com',
          docNumber: '12345678',
          code: '654321',
          pin: '482916',
        ),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'RATE_LIMITED'),
        ),
      );
    });

    test('422 propaga VALIDATION_ERROR (PIN débil o email malformado)',
        () async {
      _statusToReturn = 422;
      _dataToReturn = {'code': 'VALIDATION_ERROR', 'message': 'interno'};
      final service = HttpPinResetService(api: _api(session));

      expect(
        () => service.resetPin(
          email: 'a@b.com',
          docNumber: '12345678',
          code: '654321',
          pin: '123456',
        ),
        throwsA(
          isA<ApiException>().having(
            (e) => e.code,
            'code',
            'VALIDATION_ERROR',
          ),
        ),
      );
    });
  });
}
