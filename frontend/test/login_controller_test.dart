// Pruebas unitarias del LoginController delgado (E1-T16).
//
// - Biometria exitosa (fake) -> guarda sesion.
// - Biometria no disponible -> senala PIN, facial NUNCA llamado.
// - PIN correcto entra y guarda sesion.
// - PIN incorrecto -> mensaje GENERICO (igual que facial invalido: no filtra).
// - Inactividad expira la sesion (reloj manual via `tick()`).
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/biometrics/biometric_service.dart';
import 'package:banca_online/features/biometrics/login_controller.dart' as bio;
import 'package:banca_online/features/login/login_controller.dart';
import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:uuid/uuid.dart';

Map<String, dynamic> _sessionEnvelope(String access, String refresh) => {
      'data': {
        'access_token': access,
        'refresh_token': refresh,
        'token_type': 'Bearer',
        'session_id': 'ses-1',
        'expires_in': 900,
      },
      'meta': {'request_id': 'r-1'},
    };

/// Mock HTTP de challenge/facial/pin (sin red).
class _HttpMock {
  _HttpMock({this.facialFails = false, this.pinBiometricEnabled}) {
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          if (options.path.endsWith(bio.LoginController.challengePath)) {
            challengeCalls++;
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'data': {'nonce': 'n-abc-123', 'expires_in': 120},
                  'meta': {'request_id': 'r-challenge'},
                },
              ),
            );
            return;
          }
          if (options.path.endsWith(bio.LoginController.facialPath)) {
            facialCalls++;
            if (facialFails) {
              handler.reject(
                DioException(
                  requestOptions: options,
                  type: DioExceptionType.badResponse,
                  response: Response(
                    requestOptions: options,
                    statusCode: 401,
                    data: {
                      'error': {
                        'code': 'INVALID_SIGNATURE',
                        'message': 'firma invalida (detalle interno)',
                        'request_id': 'r-facial-err',
                      },
                    },
                  ),
                ),
              );
              return;
            }
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: _sessionEnvelope('acc-bio', 'ref-bio'),
              ),
            );
            return;
          }
          if (options.path.endsWith(LoginController.pinPath)) {
            pinCalls++;
            lastPinPayload = Map<String, dynamic>.from(options.data as Map);
            final pin = (options.data as Map)['pin'] as String?;
            if (pin == '1234') {
              final data = <String, dynamic>{
                'access_token': 'acc-pin',
                'refresh_token': 'ref-pin',
                'token_type': 'Bearer',
                'session_id': 'ses-1',
                'expires_in': 900,
              };
              // F-T49 (E1-T39): el servidor informa el consentimiento vigente.
              if (pinBiometricEnabled != null) {
                data['biometric_enabled'] = pinBiometricEnabled;
              }
              handler.resolve(
                Response(
                  requestOptions: options,
                  statusCode: 200,
                  data: {
                    'data': data,
                    'meta': {'request_id': 'r-1'},
                  },
                ),
              );
            } else {
              handler.reject(
                DioException(
                  requestOptions: options,
                  type: DioExceptionType.badResponse,
                  response: Response(
                    requestOptions: options,
                    statusCode: 401,
                    data: {
                      'error': {
                        'code': 'INVALID_PIN',
                        'message': 'pin incorrecto (detalle interno)',
                        'request_id': 'r-pin-err',
                      },
                    },
                  ),
                ),
              );
            }
            return;
          }
          handler.next(options);
        },
      ),
    );
  }

  final Dio dio = Dio();
  final bool facialFails;

  /// `biometric_enabled` que el PIN exitoso devuelve (`null` = ausente).
  final bool? pinBiometricEnabled;
  int challengeCalls = 0;
  int facialCalls = 0;
  int pinCalls = 0;
  Map<String, dynamic>? lastPinPayload;
}

LoginController _controller({
  required InMemorySessionRepository session,
  required _HttpMock http,
  required BiometricReader reader,
  SessionIdentityStore? identity,
  String? platform,
  String? biometricType,
  int inactivityTimeoutSeconds = kLoginInactivityTimeoutSeconds,
}) {
  final api = ApiClient.create(
    session: session,
    dioOverride: http.dio,
    refreshDioOverride: Dio(),
    uuid: const Uuid(),
  );
  final biometrics = BiometricService(session: session, reader: reader);
  return LoginController(
    api: api,
    session: session,
    biometricLogin: bio.LoginController(api: api, biometrics: biometrics),
    identity: identity,
    platform: platform,
    biometricType: biometricType,
    inactivityTimeoutSeconds: inactivityTimeoutSeconds,
  );
}

/// Sesion que falla al derivar la clave de binding (p. ej. secreto corrupto).
class _ThrowingBindingSession extends InMemorySessionRepository {
  bool throwOnBinding = true;

  @override
  Future<String> getOrCreateDeviceBindingKey() async {
    if (throwOnBinding) throw StateError('secreto corrupto');
    return super.getOrCreateDeviceBindingKey();
  }
}

/// Sesion que falla al guardar (p. ej. secure storage caido).
class _ThrowingSaveSession extends InMemorySessionRepository {
  @override
  Future<void> saveSession({
    required String accessToken,
    String? refreshToken,
  }) async =>
      throw StateError('secure storage caido');
}

/// Store de identidad que falla al persistir el flag biometrico (F-T49).
class _ThrowingBiometricStore extends InMemorySessionIdentityStore {
  @override
  Future<void> saveBiometricEnabled(bool enabled) async =>
      throw StateError('keystore down');
}

void main() {
  test('biometria exitosa guarda sesion', () async {
    final session = InMemorySessionRepository();
    final controller = _controller(
      session: session,
      http: _HttpMock(),
      reader: FakeBiometricReader(available: true, succeeds: true),
    );
    addTearDown(controller.dispose);

    final ok = await controller.loginWithBiometrics(
      userRef: 'u-1',
      deviceId: 'd-1',
      reason: 'Confirma tu identidad para ingresar',
    );

    expect(ok, isTrue);
    expect(controller.succeeded, isTrue);
    expect(controller.errorMessage, isNull);
    expect(session.isAuthenticated, isTrue);
    expect(session.currentAccessToken, 'acc-bio');
    expect(await session.readRefreshToken(), 'ref-bio');
  });

  test('biometria no disponible: senala PIN y facial nunca llamado', () async {
    final session = InMemorySessionRepository();
    final http = _HttpMock();
    final controller = _controller(
      session: session,
      http: http,
      reader: FakeBiometricReader(available: false),
    );
    addTearDown(controller.dispose);

    final ok = await controller.loginWithBiometrics(
      userRef: 'u-1',
      deviceId: 'd-1',
      reason: 'Confirma tu identidad para ingresar',
    );

    expect(ok, isFalse);
    expect(http.facialCalls, 0);
    expect(controller.succeeded, isFalse);
    expect(controller.showPinFallback, isTrue);
    expect(controller.infoMessage, LoginController.pinFallbackMessage);
    expect(session.isAuthenticated, isFalse);
  });

  test('PIN correcto entra y guarda sesion', () async {
    final session = InMemorySessionRepository();
    final http = _HttpMock();
    final controller = _controller(
      session: session,
      http: http,
      reader: FakeBiometricReader(available: false),
    );
    addTearDown(controller.dispose);

    final ok = await controller.loginWithPin(
      userRef: 'u-1',
      deviceId: 'd-1',
      pin: '1234',
    );

    expect(ok, isTrue);
    expect(http.pinCalls, 1);
    expect(controller.succeeded, isTrue);
    expect(session.isAuthenticated, isTrue);
    expect(session.currentAccessToken, 'acc-pin');
  });

  test('loginWithPin envia device_public_key, platform y biometric_type',
      () async {
    final session = InMemorySessionRepository();
    final http = _HttpMock();
    final controller = _controller(
      session: session,
      http: http,
      reader: FakeBiometricReader(available: false),
      platform: 'android',
      biometricType: 'FINGERPRINT',
    );
    addTearDown(controller.dispose);

    final bindingKey = await session.getOrCreateDeviceBindingKey();
    final ok = await controller.loginWithPin(
      userRef: 'u-1',
      deviceId: 'd-1',
      pin: '1234',
    );

    expect(ok, isTrue);
    final payload = http.lastPinPayload!;
    expect(payload['user_ref'], 'u-1');
    expect(payload['device_id'], 'd-1');
    expect(payload['device_public_key'], bindingKey);
    expect(
      payload['device_public_key'],
      matches(RegExp(r'^hmac:[0-9a-f]{64}$')),
    );
    expect(payload['platform'], 'android');
    expect(payload['biometric_type'], 'FINGERPRINT');
  });

  test('loginWithPin omite biometric_type cuando no se conoce', () async {
    final session = InMemorySessionRepository();
    final http = _HttpMock();
    final controller = _controller(
      session: session,
      http: http,
      reader: FakeBiometricReader(available: false),
      platform: 'ios',
    );
    addTearDown(controller.dispose);

    await controller.loginWithPin(
      userRef: 'u-1',
      deviceId: 'd-1',
      pin: '1234',
    );

    final payload = http.lastPinPayload!;
    expect(payload.containsKey('biometric_type'), isFalse);
    expect(payload['platform'], 'ios');
  });

  test('exito con PIN guarda el ultimo user_ref (F-T20)', () async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    final controller = _controller(
      session: session,
      http: _HttpMock(),
      reader: FakeBiometricReader(available: false),
      identity: identity,
    );
    addTearDown(controller.dispose);

    await controller.loginWithPin(
      userRef: 'u-pin',
      deviceId: 'd-1',
      pin: '1234',
    );

    expect(identity.userRef, 'u-pin');
    expect(await identity.readUserRef(), 'u-pin');
  });

  test('exito biometrico guarda el ultimo user_ref (F-T20)', () async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    final controller = _controller(
      session: session,
      http: _HttpMock(),
      reader: FakeBiometricReader(available: true, succeeds: true),
      identity: identity,
    );
    addTearDown(controller.dispose);

    final ok = await controller.loginWithBiometrics(
      userRef: 'u-face',
      deviceId: 'd-1',
      reason: 'Confirma tu identidad para ingresar',
    );

    expect(ok, isTrue);
    expect(identity.userRef, 'u-face');
  });

  test('no loguea PIN, user_ref ni device_public_key', () async {
    final logs = <String>[];
    final original = debugPrint;
    debugPrint = (String? message, {int? wrapWidth}) {
      logs.add(message ?? '');
    };
    addTearDown(() => debugPrint = original);

    final session = InMemorySessionRepository();
    final controller = _controller(
      session: session,
      http: _HttpMock(),
      reader: FakeBiometricReader(available: false),
      identity: InMemorySessionIdentityStore(),
      platform: 'android',
      biometricType: 'FACE',
    );
    addTearDown(controller.dispose);

    await controller.loginWithPin(
      userRef: 'u-secreto',
      deviceId: 'd-1',
      pin: '1234',
    );

    final joined = logs.join('\n');
    expect(joined, isNot(contains('1234')));
    expect(joined, isNot(contains('u-secreto')));
    expect(joined, isNot(contains('hmac:')));
    expect(joined, isNot(contains('device_public_key')));
  });

  test('PIN incorrecto y facial invalido: MISMO mensaje generico', () async {
    final session = InMemorySessionRepository();
    final http = _HttpMock(facialFails: true);
    final controller = _controller(
      session: session,
      http: http,
      reader: FakeBiometricReader(available: true, succeeds: true),
    );
    addTearDown(controller.dispose);

    // Facial invalido (firma rechazada por el servidor).
    final bioOk = await controller.loginWithBiometrics(
      userRef: 'u-1',
      deviceId: 'd-1',
      reason: 'Confirma tu identidad para ingresar',
    );
    expect(bioOk, isFalse);
    final facialMessage = controller.errorMessage;
    expect(facialMessage, LoginController.genericAuthErrorMessage);
    expect(facialMessage, isNot(contains('firma')));
    expect(session.isAuthenticated, isFalse);

    // PIN mal: identico mensaje, sin filtrar la via que fallo.
    final pinOk = await controller.loginWithPin(
      userRef: 'u-1',
      deviceId: 'd-1',
      pin: '0000',
    );
    expect(pinOk, isFalse);
    expect(controller.errorMessage, LoginController.genericAuthErrorMessage);
    expect(controller.errorMessage, facialMessage);
    expect(controller.errorMessage, isNot(contains('pin')));
  });

  test('loginWithPin libera busy y reintenta si falla la clave del dispositivo',
      () async {
    final session = _ThrowingBindingSession();
    final http = _HttpMock();
    final controller = _controller(
      session: session,
      http: http,
      reader: FakeBiometricReader(available: false),
    );
    addTearDown(controller.dispose);

    final first = await controller.loginWithPin(
      userRef: 'u-1',
      deviceId: 'd-1',
      pin: '1234',
    );

    expect(first, isFalse);
    expect(controller.busy, isFalse);
    expect(controller.errorMessage, LoginController.genericAuthErrorMessage);
    expect(controller.errorMessage, isNot(contains('secreto')));
    // No se envio el PIN porque la clave fallo antes del POST.
    expect(http.pinCalls, 0);

    // Reintento posible una vez que la clave se puede derivar.
    session.throwOnBinding = false;
    final second = await controller.loginWithPin(
      userRef: 'u-1',
      deviceId: 'd-1',
      pin: '1234',
    );
    expect(second, isTrue);
    expect(controller.busy, isFalse);
    expect(controller.succeeded, isTrue);
    expect(http.pinCalls, 1);
  });

  test('loginWithBiometrics libera busy si falla guardar la sesion', () async {
    final session = _ThrowingSaveSession();
    final controller = _controller(
      session: session,
      http: _HttpMock(),
      reader: FakeBiometricReader(available: true, succeeds: true),
    );
    addTearDown(controller.dispose);

    final ok = await controller.loginWithBiometrics(
      userRef: 'u-1',
      deviceId: 'd-1',
      reason: 'Confirma tu identidad para ingresar',
    );

    expect(ok, isFalse);
    expect(controller.busy, isFalse);
    expect(controller.errorMessage, LoginController.genericAuthErrorMessage);
  });

  test('inactividad expira la sesion (reloj manual)', () async {
    final session = InMemorySessionRepository();
    await session.saveSession(accessToken: 'acc-1', refreshToken: 'ref-1');
    final controller = _controller(
      session: session,
      http: _HttpMock(),
      reader: FakeBiometricReader(),
      inactivityTimeoutSeconds: 3,
    );
    addTearDown(controller.dispose);

    var expiredCalls = 0;
    controller.startInactivityTimer(
      autoTick: false,
      onExpired: () async => expiredCalls++,
    );
    expect(controller.remainingSeconds, 3);

    controller.tick();
    controller.tick();
    expect(controller.expired, isFalse);
    expect(session.isAuthenticated, isTrue);

    // Actividad reinicia el contador: aun no expira.
    controller.notifyActivity();
    expect(controller.remainingSeconds, 3);

    controller.tick();
    controller.tick();
    controller.tick();
    // La limpieza es asincrona: dar paso a los microtasks.
    await Future<void>.delayed(Duration.zero);
    await Future<void>.delayed(Duration.zero);

    expect(controller.expired, isTrue);
    expect(controller.errorMessage, LoginController.sessionExpiredMessage);
    expect(session.isAuthenticated, isFalse);
    expect(expiredCalls, 1);
  });

  test('F-T49: PIN con biometric_enabled=true sincroniza el flag (CA-01)',
      () async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    final controller = _controller(
      session: session,
      http: _HttpMock(pinBiometricEnabled: true),
      reader: FakeBiometricReader(available: false),
      identity: identity,
    );
    addTearDown(controller.dispose);

    final ok = await controller.loginWithPin(
      userRef: 'u-1',
      deviceId: 'd-1',
      pin: '1234',
    );

    expect(ok, isTrue);
    expect(controller.biometricEnabled, isTrue);
    expect(identity.biometricEnabled, isTrue);
    expect(await identity.readBiometricEnabled(), isTrue);
  });

  test('F-T49: PIN con biometric_enabled=false sincroniza el flag (CA-01)',
      () async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore(biometricEnabled: true);
    final controller = _controller(
      session: session,
      http: _HttpMock(pinBiometricEnabled: false),
      reader: FakeBiometricReader(available: false),
      identity: identity,
    );
    addTearDown(controller.dispose);

    final ok = await controller.loginWithPin(
      userRef: 'u-1',
      deviceId: 'd-1',
      pin: '1234',
    );

    expect(ok, isTrue);
    expect(controller.biometricEnabled, isFalse);
    expect(await identity.readBiometricEnabled(), isFalse);
  });

  test('F-T49: PIN sin biometric_enabled no escribe el flag', () async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    final controller = _controller(
      session: session,
      http: _HttpMock(),
      reader: FakeBiometricReader(available: false),
      identity: identity,
    );
    addTearDown(controller.dispose);

    final ok = await controller.loginWithPin(
      userRef: 'u-1',
      deviceId: 'd-1',
      pin: '1234',
    );

    expect(ok, isTrue);
    expect(controller.biometricEnabled, isNull);
    expect(await identity.readBiometricEnabled(), isNull);
  });

  test('F-T49: fallo del store biometrico no tumba el login (CA-01)',
      () async {
    final session = InMemorySessionRepository();
    final identity = _ThrowingBiometricStore();
    final controller = _controller(
      session: session,
      http: _HttpMock(pinBiometricEnabled: true),
      reader: FakeBiometricReader(available: false),
      identity: identity,
    );
    addTearDown(controller.dispose);

    final ok = await controller.loginWithPin(
      userRef: 'u-1',
      deviceId: 'd-1',
      pin: '1234',
    );

    expect(ok, isTrue);
    expect(controller.succeeded, isTrue);
    expect(session.isAuthenticated, isTrue);
    // La cache del controlador refleja el servidor aunque el store falle.
    expect(controller.biometricEnabled, isTrue);
  });

  test('F-T49: facial fallido guia al PIN sin revelar la causa (CA-03)',
      () async {
    final session = InMemorySessionRepository();
    final controller = _controller(
      session: session,
      http: _HttpMock(facialFails: true),
      reader: FakeBiometricReader(available: true, succeeds: true),
    );
    addTearDown(controller.dispose);

    final ok = await controller.loginWithBiometrics(
      userRef: 'u-1',
      deviceId: 'd-1',
      reason: 'Confirma tu identidad para ingresar',
    );

    expect(ok, isFalse);
    // Nunca queda solo el error generico: hay salida al PIN.
    expect(controller.showPinFallback, isTrue);
    expect(controller.infoMessage, LoginController.pinFallbackMessage);
    // El mensaje generico se mantiene y no filtra la causa (400 por falta
    // de consentimiento, firma invalida, red, etc.).
    expect(controller.errorMessage, LoginController.genericAuthErrorMessage);
    expect(controller.errorMessage, isNot(contains('consentimiento')));
    expect(controller.infoMessage, isNot(contains('consentimiento')));
  });

  test('F-T49: controlador hidrata biometricEnabled desde el store', () async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore(biometricEnabled: true);
    final controller = _controller(
      session: session,
      http: _HttpMock(),
      reader: FakeBiometricReader(available: false),
      identity: identity,
    );
    addTearDown(controller.dispose);

    expect(controller.biometricEnabled, isTrue);
  });

  test('F-T49: sin store el flag es null (boton oculto)', () async {
    final session = InMemorySessionRepository();
    final controller = _controller(
      session: session,
      http: _HttpMock(),
      reader: FakeBiometricReader(available: false),
    );
    addTearDown(controller.dispose);

    expect(controller.biometricEnabled, isNull);
  });
}
