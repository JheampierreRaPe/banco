import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/activation/activation_service.dart';
import 'package:banca_online/features/pin_setup/pin_setup_controller.dart';
import 'package:banca_online/features/pin_setup/pin_setup_page.dart';
import 'package:banca_online/features/pin_setup/pin_setup_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake configurable del contrato de setup (sin red).
class FakePinSetupService implements PinSetupService {
  int calls = 0;
  PinSetupResult? result;
  ApiException? error;
  Map<String, String> lastArgs = {};

  @override
  Future<PinSetupResult> setup({
    required String userRef,
    required String code,
    required String pin,
  }) async {
    calls++;
    lastArgs = {'userRef': userRef, 'code': code, 'pin': pin};
    final e = error;
    if (e != null) throw e;
    return result ?? const PinSetupResult(userId: 'u-1', status: 'PIN_SET');
  }
}

/// Fake del MISMO contrato de reenvío de `activation` (sin red).
class FakeResendService implements ActivationService {
  int calls = 0;

  @override
  Future<ActivationResult> activate({
    required String userRef,
    required String code,
  }) =>
      throw UnimplementedError();

  @override
  Future<ResendResult> resend({required String userRef, String? channel}) async {
    calls++;
    return const ResendResult(
      userId: 'u-1',
      resendCount: 1,
      expiresInSeconds: 600,
    );
  }
}

GoRouter _router(
  FakePinSetupService setup,
  FakeResendService resend,
) =>
    GoRouter(
      initialLocation: '/pin-setup',
      routes: [
        GoRoute(
          path: '/pin-setup',
          builder: (context, state) => PinSetupPage(
            userRef: 'u-1',
            setupService: setup,
            resendService: resend,
          ),
        ),
        GoRoute(
          path: '/login',
          builder: (context, state) =>
              const Scaffold(body: Text('login-ok')),
        ),
      ],
    );

Future<void> _pump(
  WidgetTester tester,
  FakePinSetupService setup,
  FakeResendService resend,
) async {
  await tester.pumpWidget(
    MaterialApp.router(routerConfig: _router(setup, resend)),
  );
  await tester.pumpAndSettle();
}

Future<void> _fill(
  WidgetTester tester, {
  required String code,
  required String pin,
  required String confirm,
}) async {
  await tester.enterText(find.byKey(const Key('pin-setup-code')), code);
  await tester.enterText(find.byKey(const Key('pin-setup-pin')), pin);
  await tester.enterText(find.byKey(const Key('pin-setup-confirm')), confirm);
  await tester.pump();
}

void main() {
  testWidgets('setup OK navega a /login (servicio mockeado)',
      (tester) async {
    final setup = FakePinSetupService();
    await _pump(tester, setup, FakeResendService());

    await _fill(tester, code: '123456', pin: '1234', confirm: '1234');
    await tester.tap(find.byKey(const Key('pin-setup-submit')));
    await tester.pumpAndSettle();

    expect(setup.calls, 1);
    expect(setup.lastArgs['pin'], '1234');
    expect(find.text('login-ok'), findsOneWidget);
  });

  testWidgets('PINs que no coinciden bloquean el envío', (tester) async {
    final setup = FakePinSetupService();
    await _pump(tester, setup, FakeResendService());

    await _fill(tester, code: '123456', pin: '1234', confirm: '5678');
    await tester.tap(find.byKey(const Key('pin-setup-submit')));
    await tester.pumpAndSettle();

    expect(setup.calls, 0);
    expect(find.text(PinSetupController.mismatchMessage), findsOneWidget);
    expect(find.text('login-ok'), findsNothing);
  });

  testWidgets('409 PIN_ALREADY_SET muestra ir-a-login', (tester) async {
    final setup = FakePinSetupService()
      ..error = ApiException(code: 'PIN_ALREADY_SET', message: 'srv');
    await _pump(tester, setup, FakeResendService());

    await _fill(tester, code: '123456', pin: '1234', confirm: '1234');
    await tester.tap(find.byKey(const Key('pin-setup-submit')));
    await tester.pumpAndSettle();

    expect(
      find.text(PinSetupController.alreadySetMessage),
      findsOneWidget,
    );
    expect(find.byKey(const Key('pin-setup-goto-login')), findsOneWidget);

    await tester.tap(find.byKey(const Key('pin-setup-goto-login')));
    await tester.pumpAndSettle();
    expect(find.text('login-ok'), findsOneWidget);
  });

  testWidgets('401 INVALID_SETUP_CODE muestra pedir-código-nuevo',
      (tester) async {
    final setup = FakePinSetupService()
      ..error = ApiException(code: 'INVALID_SETUP_CODE', message: 'srv');
    await _pump(tester, setup, FakeResendService());

    await _fill(tester, code: '000000', pin: '1234', confirm: '1234');
    await tester.tap(find.byKey(const Key('pin-setup-submit')));
    await tester.pumpAndSettle();

    expect(
      find.text(PinSetupController.invalidCodeMessage),
      findsOneWidget,
    );
    expect(find.text('login-ok'), findsNothing);
  });
}
