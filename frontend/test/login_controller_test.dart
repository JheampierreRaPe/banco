// Pruebas unitarias del LoginController delgado (E1-T16).
//
// - Biometria exitosa (fake) -> guarda sesion.
// - Biometria no disponible -> senala PIN, facial NUNCA llamado.
// - PIN correcto entra y guarda sesion.
// - PIN incorrecto -> mensaje GENERICO (igual que facial invalido: no filtra).
// - Inactividad expira la sesion (reloj manual via `tick()`).
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/biometrics/biometric_service.dart';
import 'package:banca_online/features/biometrics/login_controller.dart' as bio;
import 'package:banca_online/features/login/login_controller.dart';
import 'package:dio/dio.dart';
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
  _HttpMock({this.facialFails = false}) {
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
            final pin = (options.data as Map)['pin'] as String?;
            if (pin == '1234') {
              handler.resolve(
                Response(
                  requestOptions: options,
                  statusCode: 200,
                  data: _sessionEnvelope('acc-pin', 'ref-pin'),
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
  int challengeCalls = 0;
  int facialCalls = 0;
  int pinCalls = 0;
}

LoginController _controller({
  required InMemorySessionRepository session,
  required _HttpMock http,
  required BiometricReader reader,
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
    inactivityTimeoutSeconds: inactivityTimeoutSeconds,
  );
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
}
