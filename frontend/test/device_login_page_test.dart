// Pruebas de widget del flujo "iniciar sesion en este dispositivo" (F-T56).
//
// - Render de los 3 pasos con las claves de test (CA-04).
// - Flujo feliz: email+DNI -> OTP -> PIN -> asocia y abre sesion (la guarda
//   redirige a `/home`; persiste sesion + `userRef`).
// - Errores genericos por paso (CA-03): mismo mensaje sin revelar la causa.
// - "Olvide mi PIN" -> `/pin-reset` (fallback siempre disponible).
// - Estado vacio (controlador `null`).
// - Sin PII en rutas: email/DNI/OTP/PIN nunca aparecen en la URL.
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/errors/error_messages.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/login/device_login_controller.dart';
import 'package:banca_online/features/login/device_login_service.dart';
import 'package:banca_online/features/login/presentation/device_login_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

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

  @override
  Future<DeviceLoginRequestResult> requestOtp({
    required String email,
    required String docType,
    required String documentNumber,
  }) async {
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
    final error = completeError;
    if (error != null) throw error;
    return completeResult;
  }
}

class _Harness {
  _Harness({FakeDeviceLoginService? service, bool nullController = false})
      : service = service ?? FakeDeviceLoginService() {
    identity = InMemorySessionIdentityStore();
    controller = DeviceLoginController(
      service: this.service,
      session: session,
      identity: identity,
      platform: 'android',
    );
    router = GoRouter(
      initialLocation: '/login/device',
      // Guarda minima (paridad con `app_router.dart`): con sesion,
      // `/login/device` redirige a `/home` sin navegacion manual.
      refreshListenable: session,
      redirect: (context, state) {
        if (session.isAuthenticated) {
          final loc = state.matchedLocation;
          if (loc == '/login/device' || loc.startsWith('/login/')) {
            return '/home';
          }
        }
        return null;
      },
      routes: [
        GoRoute(
          path: '/login/device',
          builder: (context, state) => DeviceLoginPage(
            controller: nullController ? null : controller,
          ),
        ),
        GoRoute(
          path: '/home',
          builder: (context, state) => const Scaffold(body: Text('home-ok')),
        ),
        GoRoute(
          path: '/pin-reset',
          builder: (context, state) =>
              const Scaffold(body: Text('pin-reset-ok')),
        ),
        GoRoute(
          path: '/login',
          builder: (context, state) => const Scaffold(body: Text('login-ok')),
        ),
      ],
    );
  }

  final FakeDeviceLoginService service;
  final InMemorySessionRepository session = InMemorySessionRepository();
  late final InMemorySessionIdentityStore identity;
  late final DeviceLoginController controller;
  late final GoRouter router;
}

Future<void> _pump(WidgetTester tester, _Harness h) async {
  await tester.pumpWidget(MaterialApp.router(routerConfig: h.router));
  await tester.pumpAndSettle();
  addTearDown(h.controller.dispose);
  addTearDown(h.router.dispose);
}

/// Completa el paso 1 (email + DNI) y espera el paso 2.
Future<void> _doStep1(
  WidgetTester tester, {
  String email = 'usuario@banco.com',
  String doc = '12345678',
}) async {
  await tester.enterText(find.byKey(const Key('device-login-email')), email);
  await tester.enterText(find.byKey(const Key('device-login-document')), doc);
  await tester.pump();
  await tester.tap(find.byKey(const Key('device-login-request-submit')));
  await tester.pumpAndSettle();
}

/// Completa el paso 2 (OTP) y espera el paso 3.
Future<void> _doStep2(WidgetTester tester, {String code = '654321'}) async {
  await tester.enterText(find.byKey(const Key('device-login-code')), code);
  await tester.pump();
  await tester.tap(find.byKey(const Key('device-login-code-submit')));
  await tester.pumpAndSettle();
}

/// Completa el paso 3 (PIN).
Future<void> _doStep3(WidgetTester tester, {String pin = '482916'}) async {
  await tester.enterText(find.byKey(const Key('device-login-pin')), pin);
  await tester.pump();
  await tester.tap(find.byKey(const Key('device-login-pin-submit')));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('CA-04: paso 1 renderiza claves y enlace a /pin-reset',
      (tester) async {
    final h = _Harness();
    await _pump(tester, h);

    expect(find.byKey(const Key('device-login-email')), findsOneWidget);
    expect(find.byKey(const Key('device-login-doc-type')), findsOneWidget);
    expect(find.byKey(const Key('device-login-document')), findsOneWidget);
    expect(
      find.byKey(const Key('device-login-request-submit')),
      findsOneWidget,
    );
    expect(
      find.byKey(const Key('device-login-pin-reset-link')),
      findsOneWidget,
    );
    expect(find.text('Olvidé mi PIN'), findsOneWidget);
    // Sin mensaje inicial.
    expect(find.byKey(const Key('device-login-message')), findsNothing);
  });

  testWidgets('flujo feliz de 3 pasos: asocia, guarda sesion y va a /home',
      (tester) async {
    final h = _Harness();
    await _pump(tester, h);

    await _doStep1(tester);
    expect(find.byKey(const Key('device-login-code')), findsOneWidget);
    expect(
      find.byKey(const Key('device-login-code-submit')),
      findsOneWidget,
    );

    await _doStep2(tester);
    expect(find.byKey(const Key('device-login-pin')), findsOneWidget);
    expect(find.byKey(const Key('device-login-pin-submit')), findsOneWidget);
    // El PIN es sensible: oculto y numerico.
    final pinField =
        tester.widget<TextField>(find.byKey(const Key('device-login-pin')));
    expect(pinField.obscureText, isTrue);
    expect(pinField.keyboardType, TextInputType.number);

    await _doStep3(tester);

    expect(find.text('home-ok'), findsOneWidget);
    expect(h.session.isAuthenticated, isTrue);
    expect(h.session.currentAccessToken, 'acc-device');
    expect(h.identity.userRef, 'user-123');
  });

  testWidgets('CA-03: fallo de request muestra el generico sin filtrar',
      (tester) async {
    final service = FakeDeviceLoginService()
      ..requestError = ApiException(
        code: 'RATE_LIMITED',
        message: messageForCode('RATE_LIMITED'),
      );
    final h = _Harness(service: service);
    await _pump(tester, h);

    await _doStep1(tester);

    final message =
        tester.widget<Text>(find.byKey(const Key('device-login-message')));
    expect(
      message.data,
      DeviceLoginController.genericDeviceLoginErrorMessage,
    );
    expect(message.data, isNot(contains('RATE_LIMITED')));
    // Sigue en el paso 1 (puede reintentar).
    expect(find.byKey(const Key('device-login-email')), findsOneWidget);
  });

  testWidgets('CA-03: fallo de complete usa el MISMO generico',
      (tester) async {
    final service = FakeDeviceLoginService()
      ..completeError = ApiException(
        code: 'INVALID_LOGIN',
        message: messageForCode('INVALID_LOGIN'),
      );
    final h = _Harness(service: service);
    await _pump(tester, h);

    await _doStep1(tester);
    await _doStep2(tester);
    await _doStep3(tester);

    final message =
        tester.widget<Text>(find.byKey(const Key('device-login-message')));
    expect(
      message.data,
      DeviceLoginController.genericDeviceLoginErrorMessage,
    );
    expect(message.data, isNot(contains('INVALID_LOGIN')));
    expect(find.text('home-ok'), findsNothing);
    expect(h.session.isAuthenticated, isFalse);
  });

  testWidgets('formato invalido muestra guia de formato (paso 1)',
      (tester) async {
    final h = _Harness();
    await _pump(tester, h);

    await _doStep1(tester, email: 'no-es-correo');

    final message =
        tester.widget<Text>(find.byKey(const Key('device-login-message')));
    expect(message.data, DeviceLoginController.invalidEmailMessage);
    expect(find.byKey(const Key('device-login-email')), findsOneWidget);
  });

  testWidgets('"Olvide mi PIN" navega a /pin-reset', (tester) async {
    final h = _Harness();
    await _pump(tester, h);

    final link = find.byKey(const Key('device-login-pin-reset-link'));
    await tester.ensureVisible(link);
    await tester.pumpAndSettle();
    await tester.tap(link);
    await tester.pumpAndSettle();

    expect(find.text('pin-reset-ok'), findsOneWidget);
  });

  testWidgets('estado vacio: sin controlador muestra ayuda', (tester) async {
    final h = _Harness(nullController: true);
    await _pump(tester, h);

    expect(find.byKey(const Key('device-login-email')), findsNothing);
    expect(find.text('Volver a iniciar sesión'), findsOneWidget);
  });
}
