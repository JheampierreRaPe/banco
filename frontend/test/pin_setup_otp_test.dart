// Paso OTP del alta (F-T39): verifica el codigo por email y crea el PIN con
// `POST /auth/pin/setup`. Exito -> `/registration-success`.
//
// Cero red: fakes de servicios. Incluye errores accionables (OTP invalido,
// PIN ya definido, rate limit) y la garantia de que PIN/OTP/`user_ref` no
// aparecen en mensajes.
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/core/widgets/app_button.dart';
import 'package:banca_online/features/activation/activation_service.dart';
import 'package:banca_online/features/pin_setup/pin_setup_otp_controller.dart';
import 'package:banca_online/features/pin_setup/pin_setup_otp_page.dart';
import 'package:banca_online/features/pin_setup/pin_setup_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

const String kUserRef = 'u-1';
const String kPin = '482916';

/// Fake configurable del setup (sin red).
class FakePinSetupService implements PinSetupService {
  int calls = 0;
  ApiException? error;
  Map<String, String> lastArgs = {};
  bool? lastBiometricEnabled;

  @override
  Future<PinSetupResult> setup({
    required String userRef,
    required String code,
    required String pin,
    bool biometricEnabled = false,
  }) async {
    calls++;
    lastArgs = {'userRef': userRef, 'code': code, 'pin': pin};
    lastBiometricEnabled = biometricEnabled;
    final e = error;
    if (e != null) throw e;
    return const PinSetupResult(userId: kUserRef, status: 'ACTIVE');
  }
}

/// Fake del reenvio (mismo contrato de `activation`, sin red).
class FakeResendService implements ActivationService {
  int calls = 0;
  String? lastChannel;

  @override
  Future<ActivationResult> activate({
    required String userRef,
    required String code,
  }) =>
      throw UnimplementedError();

  @override
  Future<ResendResult> resend({required String userRef, String? channel}) async {
    calls++;
    lastChannel = channel;
    return const ResendResult(
      userId: kUserRef,
      resendCount: 1,
      expiresInSeconds: 600,
    );
  }
}

GoRouter _router(
  FakePinSetupService setup,
  FakeResendService resend, {
  SessionIdentityStore? identity,
  bool biometricEnabled = false,
}) =>
    GoRouter(
      initialLocation: '/otp',
      routes: [
        GoRoute(
          path: '/otp',
          builder: (context, state) => PinSetupOtpPage(
            userRef: kUserRef,
            pin: kPin,
            biometricEnabled: biometricEnabled,
            setupService: setup,
            resendService: resend,
            identity: identity,
          ),
        ),
        GoRoute(
          path: '/registration-success',
          builder: (context, state) => Scaffold(
            body: Text(
              'success-ok:${state.uri.queryParameters['userRef']}',
            ),
          ),
        ),
        GoRoute(
          path: '/login',
          builder: (context, state) => const Scaffold(
            body: Text('login-ok'),
          ),
        ),
      ],
    );

Future<void> _pump(
  WidgetTester tester,
  FakePinSetupService setup,
  FakeResendService resend, {
  SessionIdentityStore? identity,
  bool biometricEnabled = false,
}) async {
  final router = _router(
    setup,
    resend,
    identity: identity,
    biometricEnabled: biometricEnabled,
  );
  addTearDown(router.dispose);
  await tester.pumpWidget(MaterialApp.router(routerConfig: router));
  await tester.pumpAndSettle();
}

/// Marca el codigo en el teclado propio.
Future<void> _enterCode(WidgetTester tester, String code) async {
  for (final digit in code.split('')) {
    final key = find.byKey(Key('pin-key-$digit'));
    await tester.ensureVisible(key);
    await tester.tap(key);
    await tester.pump();
  }
  await tester.pumpAndSettle();
}

/// Borra el codigo con la tecla de retroceso.
Future<void> _clearCode(WidgetTester tester) async {
  for (var i = 0; i < 6; i++) {
    final key = find.byKey(const Key('pin-key-back'));
    await tester.ensureVisible(key);
    await tester.tap(key);
    await tester.pump();
  }
  await tester.pumpAndSettle();
}

/// Toca un boton con scroll previo (las pantallas F-T39 son mas altas que
/// el viewport de pruebas).
Future<void> _tapKey(WidgetTester tester, Key key) async {
  final finder = find.byKey(key);
  await tester.ensureVisible(finder);
  await tester.tap(finder);
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('OTP valido crea el PIN y navega a registration-success',
      (tester) async {
    final setup = FakePinSetupService();
    await _pump(tester, setup, FakeResendService());

    expect(find.text('Revisa tu correo'), findsOneWidget);
    expect(
      find.text(PinSetupOtpController.codeHint),
      findsOneWidget,
    );

    await _enterCode(tester, '123456');
    await _tapKey(tester, const Key('pin-setup-submit'));

    expect(setup.calls, 1);
    expect(setup.lastArgs['userRef'], kUserRef);
    expect(setup.lastArgs['code'], '123456');
    expect(setup.lastArgs['pin'], kPin);
    expect(find.text('success-ok:$kUserRef'), findsOneWidget);
  });

  testWidgets('reenvio usa el canal email', (tester) async {
    final resend = FakeResendService();
    await _pump(tester, FakePinSetupService(), resend);

    await _tapKey(tester, const Key('pin-setup-resend'));

    expect(resend.calls, 1);
    expect(resend.lastChannel, 'email');
    expect(find.text(PinSetupOtpController.resentMessage), findsOneWidget);
  });

  testWidgets('OTP invalido muestra pedir-codigo-nuevo y reintenta',
      (tester) async {
    final setup = FakePinSetupService()
      ..error = ApiException(code: 'INVALID_SETUP_CODE', message: 'srv');
    await _pump(tester, setup, FakeResendService());

    await _enterCode(tester, '000000');
    await _tapKey(tester, const Key('pin-setup-submit'));

    expect(
      find.text(PinSetupOtpController.invalidCodeMessage),
      findsOneWidget,
    );
    expect(find.text('success-ok:$kUserRef'), findsNothing);

    // Reintento con un codigo valido.
    setup.error = null;
    await _clearCode(tester);
    await _enterCode(tester, '123456');
    await _tapKey(tester, const Key('pin-setup-submit'));
    expect(setup.calls, 2);
    expect(find.text('success-ok:$kUserRef'), findsOneWidget);
  });

  testWidgets('409 PIN_ALREADY_SET ofrece ir al login', (tester) async {
    final setup = FakePinSetupService()
      ..error = ApiException(code: 'PIN_ALREADY_SET', message: 'srv');
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await _pump(tester, setup, FakeResendService(), identity: identity);

    await _enterCode(tester, '123456');
    await _tapKey(tester, const Key('pin-setup-submit'));

    expect(
      find.text(PinSetupOtpController.alreadySetMessage),
      findsOneWidget,
    );
    expect(find.byKey(const Key('pin-setup-goto-login')), findsOneWidget);

    await _tapKey(tester, const Key('pin-setup-goto-login'));
    expect(find.text('login-ok'), findsOneWidget);
  });

  testWidgets('rate limit muestra mensaje accionable', (tester) async {
    final setup = FakePinSetupService()
      ..error = ApiException(code: 'RATE_LIMITED', message: 'srv');
    await _pump(tester, setup, FakeResendService());

    await _enterCode(tester, '123456');
    await _tapKey(tester, const Key('pin-setup-submit'));

    expect(
      find.text(PinSetupOtpController.rateLimitedMessage),
      findsOneWidget,
    );
    expect(find.text('success-ok:$kUserRef'), findsNothing);
  });
  testWidgets('codigo incompleto no envia', (tester) async {
    final setup = FakePinSetupService();
    await _pump(tester, setup, FakeResendService());

    await _enterCode(tester, '123');
    // Boton deshabilitado con codigo incompleto.
    final submit = tester.widget<AppPrimaryButton>(
      find.byKey(const Key('pin-setup-submit')),
    );
    expect(submit.onPressed, isNull);
    expect(setup.calls, 0);
  });

  testWidgets('el setup envia biometric_enabled=true cuando se eligio',
      (tester) async {
    final setup = FakePinSetupService();
    await _pump(
      tester,
      setup,
      FakeResendService(),
      biometricEnabled: true,
    );

    await _enterCode(tester, '123456');
    await _tapKey(tester, const Key('pin-setup-submit'));

    expect(setup.calls, 1);
    expect(setup.lastArgs['pin'], kPin);
    expect(setup.lastBiometricEnabled, isTrue);
    expect(find.text('success-ok:$kUserRef'), findsOneWidget);
  });

  testWidgets('el setup envia biometric_enabled=false por defecto',
      (tester) async {
    final setup = FakePinSetupService();
    await _pump(tester, setup, FakeResendService());

    await _enterCode(tester, '123456');
    await _tapKey(tester, const Key('pin-setup-submit'));

    expect(setup.calls, 1);
    expect(setup.lastBiometricEnabled, isFalse);
    expect(find.text('success-ok:$kUserRef'), findsOneWidget);
  });

  test('el controlador propaga biometric_enabled al servicio', () async {
    final setup = FakePinSetupService();
    final controller = PinSetupOtpController(
      setupService: setup,
      resendService: FakeResendService(),
      userRef: kUserRef,
      biometricEnabled: true,
    );
    addTearDown(controller.dispose);

    controller.setCode('123456');
    expect(await controller.submit(kPin), isTrue);
    expect(setup.lastBiometricEnabled, isTrue);
  });

  test('los mensajes del controlador nunca contienen secretos', () async {
    const userRef = 'u-secreto';
    const pin = '482916';
    const code = '123456';
    final controller = PinSetupOtpController(
      setupService: _ThrowingSetupService(),
      resendService: FakeResendService(),
      userRef: userRef,
    );
    addTearDown(controller.dispose);

    controller.setCode(code);
    await controller.submit(pin);
    expect(controller.errorMessage, isNotNull);
    expect(controller.errorMessage, isNot(contains(pin)));
    expect(controller.errorMessage, isNot(contains(code)));
    expect(controller.errorMessage, isNot(contains(userRef)));

    await controller.resend();
    final info = controller.infoMessage ?? '';
    final error = controller.errorMessage ?? '';
    expect(info, isNot(contains(pin)));
    expect(info, isNot(contains(code)));
    expect(error, isNot(contains(pin)));
    expect(error, isNot(contains(code)));
  });
}

/// Setup que siempre falla con un mensaje generico del servidor.
class _ThrowingSetupService implements PinSetupService {
  @override
  Future<PinSetupResult> setup({
    required String userRef,
    required String code,
    required String pin,
    bool biometricEnabled = false,
  }) async {
    throw ApiException(code: 'UNKNOWN', message: 'Fallo el servidor');
  }
}
