import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/activation/activation_controller.dart';
import 'package:banca_online/features/activation/activation_page.dart';
import 'package:banca_online/features/activation/activation_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake configurable del contrato (sin red).
class FakeActivationService implements ActivationService {
  ActivationResult? activateResult;
  ApiException? activateError;

  @override
  Future<ActivationResult> activate({
    required String userRef,
    required String code,
  }) async {
    final error = activateError;
    if (error != null) throw error;
    return activateResult ??
        const ActivationResult(userId: 'u-1', status: 'ACTIVE');
  }

  @override
  Future<ResendResult> resend({required String userRef, String? channel}) async {
    return const ResendResult(
      userId: 'u-1',
      resendCount: 1,
      expiresInSeconds: 600,
    );
  }
}

/// Router de prueba: `/activate` con el fake + `/pin-setup` visible (el flujo
/// de alta continúa a crear el PIN tras activar).
/// `autoTick: false` para que `pumpAndSettle` no espere al `Timer` real
/// (el contador se verifica a nivel controller).
GoRouter _router(FakeActivationService service) => GoRouter(
      initialLocation: '/activate',
      routes: [
        GoRoute(
          path: '/activate',
          builder: (context, state) => ActivationPage(
            userRef: 'u-1',
            service: service,
            resendWaitSeconds: 0,
            autoTick: false,
          ),
        ),
        GoRoute(
          path: '/pin-setup',
          builder: (context, state) =>
              const Scaffold(body: Text('pin-setup-ok')),
        ),
      ],
    );

Future<void> _pump(WidgetTester tester, FakeActivationService service) async {
  await tester.pumpWidget(MaterialApp.router(routerConfig: _router(service)));
  await tester.pumpAndSettle();
}

Future<void> _enterCode(WidgetTester tester, String code) async {
  for (var i = 0; i < 6; i++) {
    await tester.enterText(find.byKey(Key('otp-$i')), code[i]);
    await tester.pump();
  }
}

void main() {
  testWidgets('ingreso correcto navega a /pin-setup (sin auto-login)',
      (tester) async {
    await _pump(tester, FakeActivationService());

    await _enterCode(tester, '123456');
    await tester.tap(find.byKey(const Key('activate-submit')));
    await tester.pumpAndSettle();

    expect(find.text('pin-setup-ok'), findsOneWidget);
  });

  testWidgets('código expirado muestra mensaje y ofrece reenvío',
      (tester) async {
    final service = FakeActivationService()
      ..activateError = ApiException(code: 'EXPIRED_OTP', message: 'srv');
    await _pump(tester, service);

    await _enterCode(tester, '123456');
    await tester.tap(find.byKey(const Key('activate-submit')));
    await tester.pumpAndSettle();

    expect(
      find.text(ActivationController.expiredMessage),
      findsOneWidget,
    );
    // El reenvío queda habilitado como acción de recuperación.
    final resend =
        tester.widget<TextButton>(find.byKey(const Key('activate-resend')));
    expect(resend.onPressed, isNotNull);
  });

  testWidgets('límite alcanzado muestra estado límite', (tester) async {
    final service = FakeActivationService()
      ..activateError = ApiException(code: 'RESEND_LIMIT', message: 'srv');
    await _pump(tester, service);

    await _enterCode(tester, '123456');
    await tester.tap(find.byKey(const Key('activate-submit')));
    await tester.pumpAndSettle();

    expect(
      find.text(ActivationController.limitMessage),
      findsOneWidget,
    );
  });
}
