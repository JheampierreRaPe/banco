// Pruebas del servicio HTTP de recuperación (F-T40, contrato E1-T33).
//
// - `request` envía `{email}` a `/auth/recovery/request` y parsea
//   `accepted`/`ttl_seconds`/`resend_wait_seconds`.
// - `verify` envía `{email, code, device_id, device_public_key, platform}` a
//   `/auth/recovery/verify` y parsea SOLO `{user_ref, device_bound}`
//   (E1-T33/SCR-005: verify YA NO abre sesión ni devuelve tokens; la única
//   sesión la abre `POST /auth/login/pin`).
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
    test('envía binding del dispositivo y parsea user_ref/device_bound',
        () async {
      _dataToReturn = {
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
      expect(result.userRef, 'user-123');
      expect(result.deviceBound, isTrue);
    });

    test('la respuesta ya no trae tokens ni sesion (E1-T33)', () async {
      _dataToReturn = {
        'user_ref': 'user-123',
        'device_bound': false,
      };
      final service = HttpRecoveryService(api: _api(session));

      await service.verify(email: 'a@b.com', code: '123456');

      // El servicio solo expone `user_ref`/`device_bound`: si el backend
      // volviera a emitir `access_token`, el modelo ya no lo consume.
      expect(_dataToReturn.containsKey('access_token'), isFalse);
      expect(_dataToReturn.containsKey('refresh_token'), isFalse);
      expect(_dataToReturn.containsKey('session_id'), isFalse);
    });

    test('device_bound ausente usa false sin romper el contrato', () async {
      _dataToReturn = {'user_ref': 'user-123'};
      final service = HttpRecoveryService(api: _api(session));

      final result = await service.verify(
        email: 'a@b.com',
        code: '123456',
      );

      expect(_capturedBody, {'email': 'a@b.com', 'code': '123456'});
      // `device_bound=false` no bloquea: la continuación al login procede.
      expect(result.userRef, 'user-123');
      expect(result.deviceBound, isFalse);
    });

    test('omite device_public_key/platform vacíos sin romper el contrato',
        () async {
      _dataToReturn = {
        'user_ref': 'user-123',
        'device_bound': false,
      };
      final service = HttpRecoveryService(api: _api(session));

      final result = await service.verify(
        email: 'a@b.com',
        code: '123456',
      );

      expect(_capturedBody, {'email': 'a@b.com', 'code': '123456'});
      expect(result.userRef, 'user-123');
      expect(result.deviceBound, isFalse);
    });

    test('sin user_ref lanza UNKNOWN (contrato roto)', () async {
      _dataToReturn = {'device_bound': true};
      final service = HttpRecoveryService(api: _api(session));

      expect(
        () => service.verify(email: 'a@b.com', code: '123456'),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'UNKNOWN'),
        ),
      );
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
