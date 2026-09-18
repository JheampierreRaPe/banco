// Pruebas de widget del LoginPage (E1-T16).
//
// - Biometria exitosa (fakes) navega a `/home`.
// - Biometria no disponible -> fallback a PIN funciona.
// - PIN correcto entra; PIN incorrecto -> mensaje generico.
// - Inactividad expira la sesion (timeout corto inyectado + `tick()` manual;
//   `autoTick: false` para que `pumpAndSettle` no espere al `Timer` real).
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/biometrics/biometric_service.dart';
import 'package:banca_online/features/biometrics/login_controller.dart' as bio;
import 'package:banca_online/features/login/login_controller.dart';
import 'package:banca_online/features/login/login_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
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

class _Harness {
  _Harness({required this.reader, this.inactivityTimeoutSeconds = 180}) {
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          if (options.path.endsWith(bio.LoginController.challengePath)) {
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
    final api = ApiClient.create(
      session: session,
      dioOverride: dio,
      refreshDioOverride: Dio(),
      uuid: const Uuid(),
    );
    controller = LoginController(
      api: api,
      session: session,
      biometricLogin: bio.LoginController(
        api: api,
        biometrics: BiometricService(session: session, reader: reader),
      ),
      inactivityTimeoutSeconds: inactivityTimeoutSeconds,
    );
    router = GoRouter(
      initialLocation: '/login',
      routes: [
        GoRoute(
          path: '/login',
          builder: (context, state) => LoginPage(
            controller: controller,
            userRef: 'u-1',
            deviceId: 'd-1',
            autoTick: false,
          ),
        ),
        GoRoute(
          path: '/home',
          builder: (context, state) =>
              const Scaffold(body: Text('home-ok')),
        ),
      ],
    );
  }

  final Dio dio = Dio();
  final InMemorySessionRepository session = InMemorySessionRepository();
  final FakeBiometricReader reader;
  final int inactivityTimeoutSeconds;
  late final LoginController controller;
  late final GoRouter router;
}

Future<void> _pump(WidgetTester tester, _Harness h) async {
  await tester.pumpWidget(MaterialApp.router(routerConfig: h.router));
  await tester.pumpAndSettle();
  addTearDown(h.controller.dispose);
}

void main() {
  testWidgets('biometria exitosa navega a /home', (tester) async {
    final h = _Harness(reader: FakeBiometricReader());
    await _pump(tester, h);

    await tester.tap(find.byKey(const Key('login-biometric-button')));
    await tester.pumpAndSettle();

    expect(find.text('home-ok'), findsOneWidget);
    expect(h.session.isAuthenticated, isTrue);
  });

  testWidgets('biometria no disponible: fallback a PIN funciona', (tester) async {
    final h = _Harness(reader: FakeBiometricReader(available: false));
    await _pump(tester, h);

    // El campo PIN esta siempre visible como contingencia.
    expect(find.byKey(const Key('login-pin-field')), findsOneWidget);

    await tester.tap(find.byKey(const Key('login-biometric-button')));
    await tester.pumpAndSettle();

    // Cae al PIN con mensaje dirigido (sin navegar).
    expect(find.text('home-ok'), findsNothing);
    expect(
      find.text(LoginController.pinFallbackMessage),
      findsOneWidget,
    );

    await tester.enterText(
      find.byKey(const Key('login-pin-field')),
      '1234',
    );
    await tester.pump();
    await tester.tap(find.byKey(const Key('login-pin-submit')));
    await tester.pumpAndSettle();

    expect(find.text('home-ok'), findsOneWidget);
    expect(h.session.isAuthenticated, isTrue);
  });

  testWidgets('PIN correcto entra directo', (tester) async {
    final h = _Harness(reader: FakeBiometricReader(available: false));
    await _pump(tester, h);

    await tester.enterText(
      find.byKey(const Key('login-pin-field')),
      '1234',
    );
    await tester.pump();
    await tester.tap(find.byKey(const Key('login-pin-submit')));
    await tester.pumpAndSettle();

    expect(find.text('home-ok'), findsOneWidget);
  });

  testWidgets('PIN incorrecto muestra mensaje generico', (tester) async {
    final h = _Harness(reader: FakeBiometricReader(available: false));
    await _pump(tester, h);

    await tester.enterText(
      find.byKey(const Key('login-pin-field')),
      '0000',
    );
    await tester.pump();
    await tester.tap(find.byKey(const Key('login-pin-submit')));
    await tester.pumpAndSettle();

    expect(find.text('home-ok'), findsNothing);
    expect(
      find.text(LoginController.genericAuthErrorMessage),
      findsOneWidget,
    );
    expect(h.session.isAuthenticated, isFalse);
  });

  testWidgets('inactividad expira la sesion (timeout corto inyectado)',
      (tester) async {
    final h = _Harness(
      reader: FakeBiometricReader(available: false),
      inactivityTimeoutSeconds: 2,
    );
    await h.session.saveSession(accessToken: 'acc-1', refreshToken: 'ref-1');
    await _pump(tester, h);

    // Temporizador visible con el timeout inyectado.
    final countdown =
        tester.widget<Text>(find.byKey(const Key('login-inactivity-countdown')));
    expect(countdown.data, contains('00:02'));

    h.controller.tick();
    h.controller.tick();
    await tester.pump();
    await tester.pump();

    expect(
      find.text(LoginController.sessionExpiredMessage),
      findsOneWidget,
    );
    expect(h.session.isAuthenticated, isFalse);
  });
}
