// Pruebas de widget del feature `recovery` (F-T40, CA-02/CA-03/CA-04).
//
// - Email: inválido bloquea; 200 navega a `/recovery/otp` con el mismo texto
//   neutro para email existente y no existente; error de red con "Reintentar".
// - OTP (contrato E1-T33): incompleto deshabilita el submit; correcto persiste
//   `user_ref` y navega a `/login?userRef=` SIN abrir sesión ni ir a `/home`;
//   `INVALID_RECOVERY_CODE` -> mismo mensaje genérico; `429` -> espera;
//   reenvío con cooldown.
// - Router: `/recovery` y `/recovery/otp` son públicas sin sesión; el enlace
//   "Recuperar acceso" del login navega a `/recovery`.
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/router/app_router.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/core/widgets/app_button.dart';
import 'package:banca_online/features/login/login_routes.dart';
import 'package:banca_online/features/recovery/presentation/recovery_email_page.dart';
import 'package:banca_online/features/recovery/presentation/recovery_otp_page.dart';
import 'package:banca_online/features/recovery/recovery_controller.dart';
import 'package:banca_online/features/recovery/recovery_routes.dart';
import 'package:banca_online/features/recovery/recovery_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

class FakeRecoveryService implements RecoveryService {
  FakeRecoveryService({
    this.requestResult = const RecoveryRequestResult(
      accepted: true,
      ttlSeconds: 600,
      resendWaitSeconds: 0,
    ),
    this.verifyResult = const RecoveryVerifyResult(
      userRef: 'user-123',
      deviceBound: true,
    ),
  });

  RecoveryRequestResult requestResult;
  RecoveryVerifyResult verifyResult;
  ApiException? requestError;
  ApiException? verifyError;
  int requestCalls = 0;

  @override
  Future<RecoveryRequestResult> request({required String email}) async {
    requestCalls++;
    final error = requestError;
    if (error != null) throw error;
    return requestResult;
  }

  @override
  Future<RecoveryVerifyResult> verify({
    required String email,
    required String code,
    String? deviceId,
    String? devicePublicKey,
    String? platform,
  }) async {
    final error = verifyError;
    if (error != null) throw error;
    return verifyResult;
  }
}

GoRouter _emailRouter(FakeRecoveryService service) {
  final controller = RecoveryEmailController(service: service);
  return GoRouter(
    initialLocation: '/recovery',
    routes: [
      GoRoute(
        path: '/recovery',
        builder: (context, state) =>
            RecoveryEmailPage(controller: controller),
      ),
      GoRoute(
        path: '/recovery/otp',
        builder: (context, state) =>
            const Scaffold(body: Text('otp-ok')),
      ),
    ],
  );
}

GoRouter _otpRouter(
  FakeRecoveryService service, {
  required InMemorySessionRepository session,
  int resendWaitSeconds = 0,
}) {
  final controller = RecoveryOtpController(
    service: service,
    session: session,
    email: 'a@b.com',
    resendWaitSeconds: resendWaitSeconds,
    identity: InMemorySessionIdentityStore(deviceId: 'device-1'),
    platform: 'android',
  );
  return GoRouter(
    initialLocation: '/recovery/otp',
    routes: [
      GoRoute(
        path: '/recovery/otp',
        builder: (context, state) => RecoveryOtpPage(
          email: 'a@b.com',
          controller: controller,
          autoTick: false,
        ),
      ),
      // Destino E1-T33: el login recibe el `user_ref` por query.
      GoRoute(
        path: '/login',
        builder: (context, state) => Scaffold(
          body: Text(
            'login-ok:${state.uri.queryParameters['userRef'] ?? ''}',
          ),
        ),
      ),
    ],
  );
}

Future<void> _enterCode(WidgetTester tester, String code) async {
  for (var i = 0; i < 6; i++) {
    await tester.enterText(
      find.byKey(Key('recovery-otp-$i')),
      code[i],
    );
    await tester.pump();
  }
}

void main() {
  group('pantalla de email (CA-01)', () {
    testWidgets('email inválido bloquea el envío', (tester) async {
      final service = FakeRecoveryService();
      final router = _emailRouter(service);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('recovery-email-field')),
        'no-es-un-correo',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('recovery-email-submit')));
      await tester.pumpAndSettle();

      expect(service.requestCalls, 0);
      expect(
        find.text(RecoveryEmailController.invalidEmailMessage),
        findsOneWidget,
      );
      expect(find.text('otp-ok'), findsNothing);
    });

    testWidgets('200 navega a OTP con mensaje neutro (email existente)',
        (tester) async {
      final service = FakeRecoveryService();
      final router = _emailRouter(service);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('recovery-email-field')),
        'existe@banco.com',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('recovery-email-submit')));
      await tester.pumpAndSettle();

      expect(service.requestCalls, 1);
      expect(find.text('otp-ok'), findsOneWidget);
    });

    testWidgets('mismo flujo neutro para email no existente (CA-04)',
        (tester) async {
      // Respuesta 200 idéntica: la UI no distingue existencia.
      final service = FakeRecoveryService();
      final router = _emailRouter(service);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('recovery-email-field')),
        'nadie@banco.com',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('recovery-email-submit')));
      await tester.pumpAndSettle();

      expect(service.requestCalls, 1);
      expect(find.text('otp-ok'), findsOneWidget);
      // Sin mensajes tipo "usuario no encontrado".
      expect(find.textContaining('no encontrado'), findsNothing);
      expect(find.textContaining('no existe'), findsNothing);
    });

    testWidgets('error de red muestra estado de error con Reintentar',
        (tester) async {
      final service = FakeRecoveryService()
        ..requestError = ApiException.network();
      final router = _emailRouter(service);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('recovery-email-field')),
        'a@b.com',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('recovery-email-submit')));
      await tester.pumpAndSettle();

      expect(find.text('otp-ok'), findsNothing);
      expect(
        find.byKey(const Key('recovery-email-message')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('recovery-email-retry')),
        findsOneWidget,
      );
    });
  });

  group('pantalla OTP (CA-02)', () {
    testWidgets('código incompleto deshabilita el submit', (tester) async {
      final session = InMemorySessionRepository();
      final router = _otpRouter(
        FakeRecoveryService(),
        session: session,
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      final submit = tester.widget<AppPrimaryButton>(
        find.byKey(const Key('recovery-otp-submit')),
      );
      expect(submit.onPressed, isNull);

      // Mensaje neutro visible siempre (sin filtrar existencia).
      expect(
        find.text(RecoveryOtpController.neutralMessage),
        findsOneWidget,
      );
    });

    testWidgets('OTP correcto navega a /login?userRef= sin abrir sesion',
        (tester) async {
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore(deviceId: 'device-1');
      final service = FakeRecoveryService();
      final controller = RecoveryOtpController(
        service: service,
        session: session,
        email: 'a@b.com',
        identity: identity,
        platform: 'android',
      );
      final router = GoRouter(
        initialLocation: '/recovery/otp',
        routes: [
          GoRoute(
            path: '/recovery/otp',
            builder: (context, state) => RecoveryOtpPage(
              email: 'a@b.com',
              controller: controller,
              autoTick: false,
            ),
          ),
          GoRoute(
            path: '/login',
            builder: (context, state) => Scaffold(
              body: Text(
                'login-ok:${state.uri.queryParameters['userRef'] ?? ''}',
              ),
            ),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _enterCode(tester, '123456');
      await tester.tap(find.byKey(const Key('recovery-otp-submit')));
      await tester.pumpAndSettle();

      // E1-T33: continúa al login con el `user_ref`; NO abre sesión.
      expect(find.text('login-ok:user-123'), findsOneWidget);
      expect(session.isAuthenticated, isFalse);
      expect(await identity.readUserRef(), 'user-123');
    });

    testWidgets('INVALID_RECOVERY_CODE muestra el mensaje genérico',
        (tester) async {
      final service = FakeRecoveryService()
        ..verifyError = ApiException(
          code: 'INVALID_RECOVERY_CODE',
          message: 'detalle interno',
        );
      final session = InMemorySessionRepository();
      final router = _otpRouter(service, session: session);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _enterCode(tester, '000000');
      await tester.tap(find.byKey(const Key('recovery-otp-submit')));
      await tester.pumpAndSettle();

      expect(find.textContaining('login-ok'), findsNothing);
      expect(
        find.text(RecoveryOtpController.invalidCodeMessage),
        findsOneWidget,
      );
      // Permite reintentar: el submit sigue habilitado con código completo.
      final submit = tester.widget<AppPrimaryButton>(
        find.byKey(const Key('recovery-otp-submit')),
      );
      expect(submit.onPressed, isNotNull);
    });

    testWidgets('429 muestra mensaje de espera', (tester) async {
      final service = FakeRecoveryService()
        ..verifyError = ApiException(
          code: 'RATE_LIMITED',
          message: 'detalle interno',
        );
      final session = InMemorySessionRepository();
      final router = _otpRouter(service, session: session);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _enterCode(tester, '123456');
      await tester.tap(find.byKey(const Key('recovery-otp-submit')));
      await tester.pumpAndSettle();

      expect(find.textContaining('login-ok'), findsNothing);
      expect(
        find.text(RecoveryOtpController.rateMessage),
        findsOneWidget,
      );
    });

    testWidgets('reenvío respeta el cooldown y vuelve a llamar a request',
        (tester) async {
      final service = FakeRecoveryService(
        requestResult: const RecoveryRequestResult(
          accepted: true,
          ttlSeconds: 600,
          resendWaitSeconds: 30,
        ),
      );
      final session = InMemorySessionRepository();
      final router = _otpRouter(
        service,
        session: session,
        resendWaitSeconds: 30,
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      // Primer reenvío disponible (cooldown inicial en 0).
      await tester.tap(find.byKey(const Key('recovery-otp-resend')));
      await tester.pumpAndSettle();
      expect(service.requestCalls, 1);
      // Tras reenviar aplica el cooldown: el botón se deshabilita.
      final resend = tester.widget<AppGhostButton>(
        find.byKey(const Key('recovery-otp-resend')),
      );
      expect(resend.onPressed, isNull);
      expect(
        find.textContaining('Reenviar en'),
        findsOneWidget,
      );
    });

    testWidgets('cuenta atrás de vigencia visible', (tester) async {
      final session = InMemorySessionRepository();
      final router = _otpRouter(
        FakeRecoveryService(),
        session: session,
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(
        find.byKey(const Key('recovery-otp-countdown')),
        findsOneWidget,
      );
    });
  });

  group('router y enlace de login (CA-05)', () {
    testWidgets('/recovery es pública sin sesión', (tester) async {
      debugDisableRecoveryAutoTick = true;
      addTearDown(() => debugDisableRecoveryAutoTick = false);
      final session = InMemorySessionRepository();
      final router = buildRouter(
        session,
        identity: InMemorySessionIdentityStore(),
        initialLocation: '/recovery',
      );
      addTearDown(router.dispose);

      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Recuperar acceso'), findsWidgets);
      expect(find.byKey(const Key('recovery-email-field')), findsOneWidget);
    });

    testWidgets('/recovery/otp es pública sin sesión', (tester) async {
      debugDisableRecoveryAutoTick = true;
      addTearDown(() => debugDisableRecoveryAutoTick = false);
      final session = InMemorySessionRepository();
      final router = buildRouter(
        session,
        identity: InMemorySessionIdentityStore(),
        initialLocation: '/recovery/otp?email=a%40b.com',
      );
      addTearDown(router.dispose);

      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.byKey(const Key('recovery-otp-0')), findsOneWidget);
      expect(
        find.text(RecoveryOtpController.neutralMessage),
        findsOneWidget,
      );
    });

    testWidgets('enlace de login navega a /recovery', (tester) async {
      debugDisableLoginAutoTick = true;
      addTearDown(() => debugDisableLoginAutoTick = false);
      debugDisableRecoveryAutoTick = true;
      addTearDown(() => debugDisableRecoveryAutoTick = false);
      final session = InMemorySessionRepository();
      final router = buildRouter(
        session,
        identity: InMemorySessionIdentityStore(
          userRef: 'u-1',
          deviceId: 'd-1',
        ),
        initialLocation: '/login',
      );
      addTearDown(router.dispose);

      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(
        find.byKey(const Key('login-recovery-link')),
        findsOneWidget,
      );
      await tester.tap(find.byKey(const Key('login-recovery-link')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.byKey(const Key('recovery-email-field')), findsOneWidget);
    });

    testWidgets('login conserva biometría/PIN junto al enlace', (tester) async {
      debugDisableLoginAutoTick = true;
      addTearDown(() => debugDisableLoginAutoTick = false);
      debugDisableRecoveryAutoTick = true;
      addTearDown(() => debugDisableRecoveryAutoTick = false);
      final session = InMemorySessionRepository();
      final router = buildRouter(
        session,
        identity: InMemorySessionIdentityStore(
          userRef: 'u-1',
          deviceId: 'd-1',
        ),
        initialLocation: '/login',
      );
      addTearDown(router.dispose);

      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      // Regresión: el enlace es aditivo, el login sigue intacto.
      expect(find.byKey(const Key('login-biometric-button')), findsOneWidget);
      for (var i = 0; i < 6; i++) {
        expect(find.byKey(Key('login-pin-$i')), findsOneWidget);
      }
      expect(find.byKey(const Key('login-pin-submit')), findsOneWidget);
      expect(find.byKey(const Key('login-recovery-link')), findsOneWidget);
    });
  });
}
