// Pruebas de widget del LoginPage (F-T40, diseño canónico `0:860`/`0:901`).
//
// - Biometría exitosa (fakes) navega a `/home`.
// - Biometría no disponible -> fallback a PIN de 6 casillas funciona.
// - PIN correcto entra; PIN incorrecto -> mensaje genérico.
// - Inactividad expira la sesión (timeout corto inyectado + `tick()` manual;
//   `autoTick: false` para que `pumpAndSettle` no espere al `Timer` real).
// - Estado vacío sin `userRef`.
import 'package:banca_online/core/app_version.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/widgets/app_version_label.dart';
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
  _Harness({
    required this.reader,
    this.inactivityTimeoutSeconds = 180,
    this.userRef = 'u-1',
  }) {
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
            if (pin == '123456') {
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
            userRef: userRef,
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
  final String userRef;
  late final LoginController controller;
  late final GoRouter router;
}

Future<void> _pump(WidgetTester tester, _Harness h) async {
  await tester.pumpWidget(MaterialApp.router(routerConfig: h.router));
  await tester.pumpAndSettle();
  addTearDown(h.controller.dispose);
}

/// Ingresa el PIN en las 6 casillas canónicas (`0:901`).
Future<void> _enterPin(WidgetTester tester, String pin) async {
  for (var i = 0; i < 6; i++) {
    await tester.enterText(
      find.byKey(Key('login-pin-$i')),
      pin[i],
    );
    await tester.pump();
  }
}

void main() {
  testWidgets('muestra la version visible del build (F-T30)', (tester) async {
    final h = _Harness(reader: FakeBiometricReader(available: false));
    await _pump(tester, h);

    expect(find.byType(AppVersionLabel), findsOneWidget);
    expect(find.byKey(const Key('app-version')), findsOneWidget);
    expect(find.text('Version $kAppVersion'), findsOneWidget);
  });

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

    // Las 6 casillas del PIN están siempre visibles como contingencia.
    for (var i = 0; i < 6; i++) {
      expect(find.byKey(Key('login-pin-$i')), findsOneWidget);
    }

    await tester.tap(find.byKey(const Key('login-biometric-button')));
    await tester.pumpAndSettle();

    // Cae al PIN con mensaje dirigido (sin navegar).
    expect(find.text('home-ok'), findsNothing);
    expect(
      find.text(LoginController.pinFallbackMessage),
      findsOneWidget,
    );

    await _enterPin(tester, '123456');
    await tester.tap(find.byKey(const Key('login-pin-submit')));
    await tester.pumpAndSettle();

    expect(find.text('home-ok'), findsOneWidget);
    expect(h.session.isAuthenticated, isTrue);
  });

  testWidgets('PIN correcto entra directo', (tester) async {
    final h = _Harness(reader: FakeBiometricReader(available: false));
    await _pump(tester, h);

    await _enterPin(tester, '123456');
    await tester.tap(find.byKey(const Key('login-pin-submit')));
    await tester.pumpAndSettle();

    expect(find.text('home-ok'), findsOneWidget);
  });

  testWidgets('PIN incorrecto muestra mensaje generico', (tester) async {
    final h = _Harness(reader: FakeBiometricReader(available: false));
    await _pump(tester, h);

    await _enterPin(tester, '000000');
    await tester.tap(find.byKey(const Key('login-pin-submit')));
    await tester.pumpAndSettle();

    expect(find.text('home-ok'), findsNothing);
    expect(
      find.text(LoginController.genericAuthErrorMessage),
      findsOneWidget,
    );
    expect(h.session.isAuthenticated, isFalse);
  });

  testWidgets('sin userRef muestra el estado vacio', (tester) async {
    final h = _Harness(
      reader: FakeBiometricReader(available: false),
      userRef: '',
    );
    await _pump(tester, h);

    expect(find.byKey(const Key('login-empty')), findsOneWidget);
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
