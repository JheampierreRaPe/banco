// Pruebas del servicio HTTP de recuperación (F-T29).
//
// - `request` envía `{email}` a `/auth/recovery/request` y parsea
//   `accepted`/`ttl_seconds`/`resend_wait_seconds`.
// - `verify` envía `{email, code, device_id, device_public_key, platform}` a
//   `/auth/recovery/verify` y parsea la sesión + `user_ref` + `device_bound`.
// - Errores mapeados por la capa HTTP (F-T01): `401 INVALID_RECOVERY_CODE`
//   genérico y `429 RATE_LIMITED`.
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/recovery/recovery_service.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:uuid/uuid.dart';

Map<String, dynamic>? _capturedBody;
int _statusToReturn = 200;
Map<String, dynamic> _dataToReturn = const {};

ApiClient _api(InMemorySessionRepository session) {
  final dio = Dio();
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
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
    _statusToReturn = 200;
    _dataToReturn = const {};
  });

  group('request', () {
    test('envía {email} y parsea accepted/ttl/resendWait', () async {
      _dataToReturn = {
        'accepted': true,
        'ttl_seconds': 600,
        'resend_wait_seconds': 30,
      };
      final service = HttpRecoveryService(api: _api(session));

      final result = await service.request(email: 'a@b.com');

      expect(_capturedBody, {'email': 'a@b.com'});
      expect(result.accepted, isTrue);
      expect(result.ttlSeconds, 600);
      expect(result.resendWaitSeconds, 30);
    });

    test('misma respuesta neutra para email existente y no existente',
        () async {
      _dataToReturn = {
        'accepted': true,
        'ttl_seconds': 600,
        'resend_wait_seconds': 30,
      };
      final service = HttpRecoveryService(api: _api(session));

      final existing =
          await service.request(email: 'existe@banco.com');
      final missing =
          await service.request(email: 'nadie@banco.com');

      // El cliente no distingue: mismo shape, mismo `accepted`.
      expect(existing.accepted, missing.accepted);
      expect(existing.ttlSeconds, missing.ttlSeconds);
      expect(existing.resendWaitSeconds, missing.resendWaitSeconds);
    });

    test('429 propaga RATE_LIMITED por la capa HTTP', () async {
      _statusToReturn = 429;
      _dataToReturn = {'code': 'RATE_LIMITED', 'message': 'interno'};
      final service = HttpRecoveryService(api: _api(session));

      expect(
        () => service.request(email: 'a@b.com'),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'RATE_LIMITED'),
        ),
      );
    });
  });

  group('verify', () {
    test('envía binding del dispositivo y parsea la sesión', () async {
      _dataToReturn = {
        'access_token': 'acc-1',
        'refresh_token': 'ref-1',
        'token_type': 'Bearer',
        'session_id': 'ses-1',
        'expires_in': 900,
        'user_ref': 'user-123',
        'device_bound': true,
      };
      final service = HttpRecoveryService(api: _api(session));

      final result = await service.verify(
        email: 'a@b.com',
        code: '123456',
        deviceId: 'device-1',
        devicePublicKey: 'hmac:abcd',
        platform: 'android',
      );

      expect(_capturedBody, {
        'email': 'a@b.com',
        'code': '123456',
        'device_id': 'device-1',
        'device_public_key': 'hmac:abcd',
        'platform': 'android',
      });
      expect(result.accessToken, 'acc-1');
      expect(result.refreshToken, 'ref-1');
      expect(result.sessionId, 'ses-1');
      expect(result.userRef, 'user-123');
      expect(result.deviceBound, isTrue);
    });

    test('omite device_public_key/platform vacíos sin romper el contrato',
        () async {
      _dataToReturn = {
        'access_token': 'acc-1',
        'refresh_token': 'ref-1',
        'token_type': 'Bearer',
        'session_id': 'ses-1',
        'expires_in': 900,
        'user_ref': 'user-123',
        'device_bound': false,
      };
      final service = HttpRecoveryService(api: _api(session));

      final result = await service.verify(
        email: 'a@b.com',
        code: '123456',
      );

      expect(_capturedBody, {'email': 'a@b.com', 'code': '123456'});
      // `device_bound=false` no bloquea: la sesión viene igual.
      expect(result.accessToken, 'acc-1');
      expect(result.userRef, 'user-123');
      expect(result.deviceBound, isFalse);
    });

    test('401 propaga INVALID_RECOVERY_CODE (único error genérico)',
        () async {
      _statusToReturn = 401;
      _dataToReturn = {
        'code': 'INVALID_RECOVERY_CODE',
        'message': 'interno',
      };
      final service = HttpRecoveryService(api: _api(session));

      expect(
        () => service.verify(email: 'a@b.com', code: '000000'),
        throwsA(
          isA<ApiException>().having(
            (e) => e.code,
            'code',
            'INVALID_RECOVERY_CODE',
          ),
        ),
      );
    });
  });
}
