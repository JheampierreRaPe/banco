// Oferta biometrica del alta (F-T39, fig `0:704`): aceptar, rechazar u
// omitir continuan al OTP sin bloquear (fallback a PIN). El switch es la
// fuente del consentimiento: viaja en el borrador en memoria como
// `biometric_enabled` al `pin/setup` (F-T46).
import 'package:banca_online/features/biometrics/biometric_offer_page.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/pin_setup/pin_setup_draft.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

GoRouter _router({BiometricReader? reader}) => GoRouter(
      initialLocation: '/offer',
      routes: [
        GoRoute(
          path: '/offer',
          builder: (context, state) => BiometricOfferPage(
            userRef: 'u-1',
            pin: '482916',
            reader: reader,
          ),
        ),
        GoRoute(
          path: '/pin-setup/otp',
          builder: (context, state) {
            final draft = PinSetupDraft.fromExtra(state.extra);
            return Scaffold(
              body: Text('otp-ok:${draft.pin}:${draft.biometricEnabled}'),
            );
          },
        ),
      ],
    );

Future<void> _pump(WidgetTester tester, {BiometricReader? reader}) async {
  final router = _router(reader: reader);
  addTearDown(router.dispose);
  await tester.pumpWidget(MaterialApp.router(routerConfig: router));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('muestra la oferta del fig con el switch activo',
      (tester) async {
    await _pump(tester);

    expect(
      find.text('¿Quieres entrar con tu huella?'),
      findsOneWidget,
    );
    expect(find.text('Activar acceso biométrico'), findsOneWidget);
    final gate = tester.widget<Switch>(
      find.byKey(const Key('biometric-switch')),
    );
    expect(gate.value, isTrue);
  });

  testWidgets('aceptar con biometria disponible llega al OTP', (tester) async {
    final reader = FakeBiometricReader(available: true, succeeds: true);
    await _pump(tester, reader: reader);

    await tester.tap(find.byKey(const Key('biometric-continue')));
    await tester.pumpAndSettle();

    expect(reader.authenticateCalls, 1);
    // Switch activo -> el borrador lleva el consentimiento en `true`.
    expect(find.text('otp-ok:482916:true'), findsOneWidget);
  });

  testWidgets('biometria no disponible cae al PIN sin bloquear',
      (tester) async {
    final reader = FakeBiometricReader(available: false);
    await _pump(tester, reader: reader);

    await tester.tap(find.byKey(const Key('biometric-continue')));
    await tester.pumpAndSettle();

    // El gate best-effort no bloquea y el consentimiento elegido se conserva.
    expect(find.text('otp-ok:482916:true'), findsOneWidget);
  });

  testWidgets('biometria fallida/cancelada tampoco bloquea', (tester) async {
    final reader = FakeBiometricReader(available: true, succeeds: false);
    await _pump(tester, reader: reader);

    await tester.tap(find.byKey(const Key('biometric-continue')));
    await tester.pumpAndSettle();

    expect(find.text('otp-ok:482916:true'), findsOneWidget);
  });

  testWidgets('apagar el switch y continuar envia biometric_enabled=false',
      (tester) async {
    final reader = FakeBiometricReader(available: true, succeeds: true);
    await _pump(tester, reader: reader);

    await tester.tap(find.byKey(const Key('biometric-switch')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('biometric-continue')));
    await tester.pumpAndSettle();

    // Con el switch apagado no hay gate y el consentimiento es `false`.
    expect(reader.authenticateCalls, 0);
    expect(find.text('otp-ok:482916:false'), findsOneWidget);
  });

  testWidgets('apagar el switch omite el gate y llega al OTP',
      (tester) async {
    final reader = FakeBiometricReader(available: true, succeeds: true);
    await _pump(tester, reader: reader);

    await tester.tap(find.byKey(const Key('biometric-switch')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('biometric-skip')));
    await tester.pumpAndSettle();

    expect(reader.authenticateCalls, 0);
    expect(find.text('otp-ok:482916:false'), findsOneWidget);
  });

  testWidgets('omitir por ahora llega al OTP con biometric_enabled=false',
      (tester) async {
    final reader = FakeBiometricReader();
    await _pump(tester, reader: reader);

    await tester.tap(find.byKey(const Key('biometric-skip')));
    await tester.pumpAndSettle();

    expect(reader.authenticateCalls, 0);
    expect(find.text('otp-ok:482916:false'), findsOneWidget);
  });
}
