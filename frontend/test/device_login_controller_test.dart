// Pruebas del flujo "iniciar sesion en este dispositivo" (F-T56, CA-01..CA-03).
//
// - Paso 1 ok -> paso 2 (sin revelar existencia); paso 2 captura OTP sin red;
//   paso 3 ok -> `session.saveSession` + `identity.saveUserRef` +
//   `biometricEnabled` (CA-01/CA-02).
// - `platform`/`device_public_key`/`biometric_type` derivados correctamente.
// - Fallo de red y `401`/`429` en `request` o `complete` -> UNICO mensaje
//   generico (CA-03; se comparan los mensajes).
// - Fallo del store local -> sesion igual concedida (best-effort).
// - Servicio HTTP: paths relativos, cuerpos y sobre `{data, meta}`.
// - Seguridad: no-logueo de email/DNI/OTP/PIN/tokens/`user_ref`.
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/errors/error_messages.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/login/device_login_controller.dart';
import 'package:banca_online/features/login/device_login_service.dart';
import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:uuid/uuid.dart';

/// Fake programable del contrato E1-T45/E1-T46 (sin red).
class FakeDeviceLoginService implements DeviceLoginService {
  FakeDeviceLoginService({
    this.requestResult = const DeviceLoginRequestResult(
      accepted: true,
      ttlSeconds: 600,
      resendWaitSeconds: 30,
    ),
    this.completeResult = const DeviceLoginCompleteResult(
      accessToken: 'acc-device',
      refreshToken: 'ref-device',
      userRef: 'user-123',
      sessionId: 'ses-1',
      expiresIn: 900,
      biometricEnabled: true,
    ),
  });

  DeviceLoginRequestResult requestResult;
  DeviceLoginCompleteResult completeResult;
  ApiException? requestError;
  ApiException? completeError;

  int requestCalls = 0;
  int completeCalls = 0;
  Map<String, dynamic>? lastRequest;
  Map<String, dynamic>? lastComplete;

  @override
  Future<DeviceLoginRequestResult> requestOtp({
    required String email,
    required String docType,
    required String documentNumber,
  }) async {
    requestCalls++;
    lastRequest = {
      'email': email,
      'doc_type': docType,
      'document_number': documentNumber,
    };
    final error = requestError;
    if (error != null) throw error;
    return requestResult;
  }

  @override
  Future<DeviceLoginCompleteResult> complete({
    required String email,
    required String docType,
    required String documentNumber,
    required String code,
    required String pin,
    required String deviceId,
    required String devicePublicKey,
    String? platform,
    String? biometricType,
    Map<String, dynamic>? deviceInfo,
  }) async {
    completeCalls++;
    final body = <String, dynamic>{
      'email': email,
      'doc_type': docType,
      'document_number': documentNumber,
      'code': code,
      'pin': pin,
      'device_id': deviceId,
      'device_public_key': devicePublicKey,
    };
    if (platform != null) {
      body['platform'] = platform;
    }
    if (biometricType != null) {
      body['biometric_type'] = biometricType;
    }
    lastComplete = body;
    final error = completeError;
    if (error != null) throw error;
    return completeResult;
  }
}

DeviceLoginController _controller({
  required FakeDeviceLoginService service,
  required InMemorySessionRepository session,
  SessionIdentityStore? identity,
  String? platform = 'android',
  String? biometricType,
}) =>
    DeviceLoginController(
      service: service,
      session: session,
      identity: identity,
      platform: platform,
      biometricType: biometricType,
    );

/// Avanza el controlador hasta el paso 3 (request + OTP ok).
Future<void> _goToStep3(DeviceLoginController controller) async {
  final okIdentity = await controller.submitIdentity(
    email: 'usuario@banco.com',
    docType: 'DNI',
    documentNumber: '12345678',
  );
  expect(okIdentity, isTrue);
  final okCode = await controller.submitCode('654321');
  expect(okCode, isTrue);
  expect(controller.step, 3);
}

/// Store que falla al persistir (secure storage caido).
class _ThrowingUserRefStore extends InMemorySessionIdentityStore {
  @override
  Future<void> saveUserRef(String userRef) async =>
      throw StateError('keystore down');
}

/// Sesion que falla al guardar (secure storage caido).
class _ThrowingSaveSession extends InMemorySessionRepository {
  @override
  Future<void> saveSession({
    required String accessToken,
    String? refreshToken,
  }) async =>
      throw StateError('secure storage caido');
}

void main() {
  test('CA-01: paso 1 envia email+doc y con 200 avanza al paso 2', () async {
    final session = InMemorySessionRepository();
    final service = FakeDeviceLoginService();
    final controller = _controller(service: service, session: session);
    addTearDown(controller.dispose);

    final ok = await controller.submitIdentity(
      email: 'usuario@banco.com',
      docType: 'DNI',
      documentNumber: '12345678',
    );

    expect(ok, isTrue);
    expect(service.requestCalls, 1);
    expect(service.lastRequest, {
      'email': 'usuario@banco.com',
      'doc_type': 'DNI',
      'document_number': '12345678',
    });
    expect(controller.step, 2);
    expect(controller.errorMessage, isNull);
    // Neutro: no revela si la cuenta existe.
    expect(
      controller.infoMessage,
      DeviceLoginController.neutralRequestMessage,
    );
  });

  test('paso 2 captura el OTP sin llamar al backend', () async {
    final session = InMemorySessionRepository();
    final service = FakeDeviceLoginService();
    final controller = _controller(service: service, session: session);
    addTearDown(controller.dispose);

    await controller.submitIdentity(
      email: 'usuario@banco.com',
      docType: 'DNI',
      documentNumber: '12345678',
    );
    final requestCalls = service.requestCalls;

    final ok = await controller.submitCode('654321');

    expect(ok, isTrue);
    expect(controller.step, 3);
    expect(service.requestCalls, requestCalls);
    expect(service.completeCalls, 0);
  });

  test('CA-02: paso 3 envia code+pin+binding y guarda sesion + userRef',
      () async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    final service = FakeDeviceLoginService();
    final controller = _controller(
      service: service,
      session: session,
      identity: identity,
      platform: 'android',
      biometricType: 'FACE',
    );
    addTearDown(controller.dispose);
    await _goToStep3(controller);

    final bindingKey = await session.getOrCreateDeviceBindingKey();
    final deviceId = await identity.getOrCreateDeviceId();
    final ok = await controller.submitPin('482916');

    expect(ok, isTrue);
    expect(controller.succeeded, isTrue);
    expect(service.completeCalls, 1);
    final body = service.lastComplete!;
    expect(body['email'], 'usuario@banco.com');
    expect(body['code'], '654321');
    expect(body['pin'], '482916');
    expect(body['device_id'], deviceId);
    expect(body['device_public_key'], bindingKey);
    expect(
      body['device_public_key'],
      matches(RegExp(r'^hmac:[0-9a-f]{64}$')),
    );
    expect(body['platform'], 'android');
    expect(body['biometric_type'], 'FACE');
    // Persistencia al exito: tokens + userRef + flag biometrico.
    expect(session.isAuthenticated, isTrue);
    expect(session.currentAccessToken, 'acc-device');
    expect(await session.readRefreshToken(), 'ref-device');
    expect(identity.userRef, 'user-123');
    expect(await identity.readUserRef(), 'user-123');
    expect(controller.biometricEnabled, isTrue);
    expect(await identity.readBiometricEnabled(), isTrue);
  });

  test('complete omite biometric_type cuando no se conoce', () async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    final service = FakeDeviceLoginService();
    final controller = _controller(
      service: service,
      session: session,
      identity: identity,
      platform: 'ios',
    );
    addTearDown(controller.dispose);
    await _goToStep3(controller);

    await controller.submitPin('4829');

    expect(service.lastComplete!.containsKey('biometric_type'), isFalse);
    expect(service.lastComplete!['platform'], 'ios');
  });

  test('validaciones de formato no llaman al backend', () async {
    final session = InMemorySessionRepository();
    final service = FakeDeviceLoginService();
    final controller = _controller(service: service, session: session);
    addTearDown(controller.dispose);

    // Email malformado.
    expect(
      await controller.submitIdentity(
        email: 'no-es-correo',
        docType: 'DNI',
        documentNumber: '12345678',
      ),
      isFalse,
    );
    expect(
      controller.errorMessage,
      DeviceLoginController.invalidEmailMessage,
    );

    // DNI con longitud inexacta.
    expect(
      await controller.submitIdentity(
        email: 'usuario@banco.com',
        docType: 'DNI',
        documentNumber: '1234',
      ),
      isFalse,
    );
    expect(controller.errorMessage, DeviceLoginController.invalidDniMessage);

    // RUC con longitud inexacta.
    expect(
      await controller.submitIdentity(
        email: 'usuario@banco.com',
        docType: 'RUC',
        documentNumber: '12345678',
      ),
      isFalse,
    );
    expect(controller.errorMessage, DeviceLoginController.invalidRucMessage);
    expect(service.requestCalls, 0);
    expect(controller.step, 1);

    // Paso 2: OTP incompleto.
    await controller.submitIdentity(
      email: 'usuario@banco.com',
      docType: 'DNI',
      documentNumber: '12345678',
    );
    expect(await controller.submitCode('123'), isFalse);
    expect(controller.errorMessage, DeviceLoginController.invalidCodeMessage);
    expect(controller.step, 2);

    // Paso 3: PIN fuera de 4-6 digitos.
    await controller.submitCode('654321');
    expect(await controller.submitPin('12'), isFalse);
    expect(controller.errorMessage, DeviceLoginController.invalidPinMessage);
    expect(service.completeCalls, 0);
  });

  test('CA-03: fallo de request y de complete usan el MISMO mensaje generico',
      () async {
    ApiException code(String c) =>
        ApiException(code: c, message: messageForCode(c));
    final session = InMemorySessionRepository();

    // Fallo del paso 1 (429).
    final service1 = FakeDeviceLoginService()
      ..requestError = code('RATE_LIMITED');
    final c1 = _controller(service: service1, session: session);
    addTearDown(c1.dispose);
    expect(
      await c1.submitIdentity(
        email: 'usuario@banco.com',
        docType: 'DNI',
        documentNumber: '12345678',
      ),
      isFalse,
    );
    final requestMessage = c1.errorMessage;
    expect(requestMessage, DeviceLoginController.genericDeviceLoginErrorMessage);

    // Fallo del paso 3 (401 INVALID_LOGIN).
    final service2 = FakeDeviceLoginService()
      ..completeError = code('INVALID_LOGIN');
    final c2 = _controller(
      service: service2,
      session: InMemorySessionRepository(),
      identity: InMemorySessionIdentityStore(),
    );
    addTearDown(c2.dispose);
    await _goToStep3(c2);
    expect(await c2.submitPin('482916'), isFalse);
    expect(c2.errorMessage, requestMessage);
    expect(c2.succeeded, isFalse);

    // Fallo de red en el paso 3: mismo generico.
    final service3 = FakeDeviceLoginService()
      ..completeError = ApiException.network();
    final c3 = _controller(
      service: service3,
      session: InMemorySessionRepository(),
      identity: InMemorySessionIdentityStore(),
    );
    addTearDown(c3.dispose);
    await _goToStep3(c3);
    expect(await c3.submitPin('482916'), isFalse);
    expect(c3.errorMessage, requestMessage);
    expect(c3.errorMessage, isNot(contains('INVALID_LOGIN')));
    expect(c3.errorMessage, isNot(contains('NETWORK')));
  });

  test('fallo del store local no tumba la sesion concedida', () async {
    final session = InMemorySessionRepository();
    final identity = _ThrowingUserRefStore();
    final service = FakeDeviceLoginService();
    final controller = _controller(
      service: service,
      session: session,
      identity: identity,
    );
    addTearDown(controller.dispose);
    await _goToStep3(controller);

    final ok = await controller.submitPin('482916');

    expect(ok, isTrue);
    expect(controller.succeeded, isTrue);
    expect(session.isAuthenticated, isTrue);
    expect(session.currentAccessToken, 'acc-device');
  });

  test('fallo al guardar la sesion no marca exito (generico)', () async {
    final session = _ThrowingSaveSession();
    final service = FakeDeviceLoginService();
    final controller = _controller(
      service: service,
      session: session,
      identity: InMemorySessionIdentityStore(),
    );
    addTearDown(controller.dispose);
    await _goToStep3(controller);

    final ok = await controller.submitPin('482916');

    expect(ok, isFalse);
    expect(controller.succeeded, isFalse);
    expect(
      controller.errorMessage,
      DeviceLoginController.genericDeviceLoginErrorMessage,
    );
  });

  test('H2: sin device_id falla temprano sin llamar al complete', () async {
    final previousFactory = sessionIdentityStoreFactory;
    sessionIdentityStoreFactory = null;
    addTearDown(() => sessionIdentityStoreFactory = previousFactory);
    final session = InMemorySessionRepository();
    final service = FakeDeviceLoginService();
    final controller = DeviceLoginController(
      service: service,
      session: session,
      identity: null,
      platform: 'android',
    );
    addTearDown(controller.dispose);
    await _goToStep3(controller);

    final ok = await controller.submitPin('482916');

    expect(ok, isFalse);
    expect(service.completeCalls, 0);
    expect(
      controller.errorMessage,
      DeviceLoginController.invalidDeviceMessage,
    );
    expect(controller.succeeded, isFalse);
  });

  test('retry limpia el error y back vuelve al paso anterior', () async {
    final session = InMemorySessionRepository();
    final service = FakeDeviceLoginService()
      ..requestError = ApiException.network();
    final controller = _controller(service: service, session: session);
    addTearDown(controller.dispose);

    await controller.submitIdentity(
      email: 'usuario@banco.com',
      docType: 'DNI',
      documentNumber: '12345678',
    );
    expect(controller.errorMessage, isNotNull);
    controller.retry();
    expect(controller.errorMessage, isNull);

    service.requestError = null;
    await controller.submitIdentity(
      email: 'usuario@banco.com',
      docType: 'DNI',
      documentNumber: '12345678',
    );
    await controller.submitCode('654321');
    expect(controller.step, 3);
    controller.back();
    expect(controller.step, 2);
    expect(controller.errorMessage, isNull);
  });

  test('no loguea email/DNI/OTP/PIN/tokens/user_ref', () async {
    final logs = <String>[];
    final original = debugPrint;
    debugPrint = (String? message, {int? wrapWidth}) {
      logs.add(message ?? '');
    };
    addTearDown(() => debugPrint = original);

    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    final service = FakeDeviceLoginService();
    final controller = _controller(
      service: service,
      session: session,
      identity: identity,
    );
    addTearDown(controller.dispose);

    await controller.submitIdentity(
      email: 'secreto@banco.com',
      docType: 'DNI',
      documentNumber: '87654321',
    );
    await controller.submitCode('654321');
    await controller.submitPin('482916');

    final joined = logs.join('\n');
    expect(joined, isNot(contains('secreto@banco.com')));
    expect(joined, isNot(contains('87654321')));
    expect(joined, isNot(contains('654321')));
    expect(joined, isNot(contains('482916')));
    expect(joined, isNot(contains('acc-device')));
    expect(joined, isNot(contains('user-123')));
    expect(joined, isNot(contains('hmac:')));
  });

  group('HttpDeviceLoginService (contrato E1-T45/E1-T46)', () {
    HttpDeviceLoginService httpService(
      InMemorySessionRepository session,
      Dio dio,
    ) {
      final api = ApiClient.create(
        session: session,
        dioOverride: dio,
        refreshDioOverride: Dio(),
        uuid: const Uuid(),
      );
      return HttpDeviceLoginService(api: api);
    }

    test('request usa el path relativo y parsea el sobre', () async {
      final session = InMemorySessionRepository();
      final dio = Dio();
      var observedPath = '';
      Map<String, dynamic>? observedBody;
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            observedPath = options.path;
            observedBody = Map<String, dynamic>.from(options.data as Map);
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'data': {
                    'accepted': true,
                    'ttl_seconds': 600,
                    'resend_wait_seconds': 30,
                  },
                  'meta': {'request_id': 'r-1'},
                },
              ),
            );
          },
        ),
      );

      final result = await httpService(session, dio).requestOtp(
        email: 'usuario@banco.com',
        docType: 'DNI',
        documentNumber: '12345678',
      );

      expect(
        observedPath,
        endsWith(HttpDeviceLoginService.requestPath),
      );
      expect(observedBody, {
        'email': 'usuario@banco.com',
        'doc_type': 'DNI',
        'document_number': '12345678',
      });
      expect(result.accepted, isTrue);
      expect(result.ttlSeconds, 600);
      expect(result.resendWaitSeconds, 30);
    });

    test('complete envia binding y parsea tokens + user_ref', () async {
      final session = InMemorySessionRepository();
      final dio = Dio();
      Map<String, dynamic>? observedBody;
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            observedBody = Map<String, dynamic>.from(options.data as Map);
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'data': {
                    'access_token': 'acc-1',
                    'refresh_token': 'ref-1',
                    'token_type': 'Bearer',
                    'session_id': 'ses-9',
                    'expires_in': 900,
                    'user_ref': 'user-9',
                    'biometric_enabled': false,
                  },
                  'meta': {'request_id': 'r-2'},
                },
              ),
            );
          },
        ),
      );

      final result = await httpService(session, dio).complete(
        email: 'usuario@banco.com',
        docType: 'RUC',
        documentNumber: '20123456789',
        code: '654321',
        pin: '4829',
        deviceId: 'd-1',
        devicePublicKey: 'hmac:abc',
        platform: 'android',
        biometricType: 'FACE',
      );

      expect(observedBody, {
        'email': 'usuario@banco.com',
        'doc_type': 'RUC',
        'document_number': '20123456789',
        'code': '654321',
        'pin': '4829',
        'device_id': 'd-1',
        'device_public_key': 'hmac:abc',
        'platform': 'android',
        'biometric_type': 'FACE',
      });
      expect(result.accessToken, 'acc-1');
      expect(result.refreshToken, 'ref-1');
      expect(result.userRef, 'user-9');
      expect(result.sessionId, 'ses-9');
      expect(result.expiresIn, 900);
      expect(result.biometricEnabled, isFalse);
    });

    test('H3: expires_in es TTL del refresh; acepta alias refresh_expires_in',
        () async {
      Future<int> readExpires(Map<String, dynamic> data) async {
        final session = InMemorySessionRepository();
        final dio = Dio();
        dio.interceptors.add(
          InterceptorsWrapper(
            onRequest: (options, handler) {
              handler.resolve(
                Response(
                  requestOptions: options,
                  statusCode: 200,
                  data: {'data': data, 'meta': {}},
                ),
              );
            },
          ),
        );
        final result = await httpService(session, dio).complete(
          email: 'usuario@banco.com',
          docType: 'DNI',
          documentNumber: '12345678',
          code: '654321',
          pin: '4829',
          deviceId: 'd-1',
          devicePublicKey: 'hmac:abc',
        );
        return result.expiresIn;
      }

      expect(
        await readExpires({
          'access_token': 'acc-1',
          'user_ref': 'user-9',
          'expires_in': 900,
        }),
        900,
      );
      expect(
        await readExpires({
          'access_token': 'acc-1',
          'user_ref': 'user-9',
          'refresh_expires_in': 901,
        }),
        901,
      );
    });

    test('sobre sin data lanza ApiException', () async {
      final session = InMemorySessionRepository();
      final dio = Dio();
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {'meta': {}},
              ),
            );
          },
        ),
      );

      expect(
        () => httpService(session, dio).requestOtp(
          email: 'usuario@banco.com',
          docType: 'DNI',
          documentNumber: '12345678',
        ),
        throwsA(isA<ApiException>()),
      );
    });
  });
}
