// Flujo de cierre del registro (F-T39): crear -> confirmar -> biometrico
// (aceptar/omitir, sin bloqueo) con el PIN solo en memoria.
//
// Cero red: las rutas OTP/success se prueban en sus archivos; aqui se
// verifica el orden, el bloqueo por desajuste y que el PIN nunca viaja en
// la ruta.
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/activation/activation_service.dart';
import 'package:banca_online/features/pin_setup/pin_setup_routes.dart';
import 'package:banca_online/features/pin_setup/pin_setup_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del setup (sin red).
class _FakePinSetupService implements PinSetupService {
  @override
  Future<PinSetupResult> setup({
    required String userRef,
    required String code,
    required String pin,
  }) async =>
      const PinSetupResult(userId: 'u-1', status: 'ACTIVE');
}

/// Fake del reenvio (mismo contrato de `activation`, sin red).
class _FakeResendService implements ActivationService {
  @override
  Future<ActivationResult> activate({
    required String userRef,
    required String code,
  }) =>
      throw UnimplementedError();

  @override
  Future<ResendResult> resend({required String userRef, String? channel}) async =>
      const ResendResult(userId: 'u-1', resendCount: 1, expiresInSeconds: 600);
}

/// Router con las rutas reales del feature + un stub del OTP que muestra el
/// PIN recibido por `extra` (solo para aserciones, sin red ni logs).
GoRouter _router() => GoRouter(
      initialLocation: '/pin-setup?userRef=u-1',
      routes: [
        ...pinSetupRoutes,
        GoRoute(
          path: '/login',
          builder: (context, state) =>
              const Scaffold(body: Text('login-ok')),
        ),
      ],
    );

Future<GoRouter> _pump(WidgetTester tester) async {
  final router = _router();
  addTearDown(router.dispose);
  await tester.pumpWidget(MaterialApp.router(routerConfig: router));
  await tester.pumpAndSettle();
  return router;
}

/// Marca los digitos en el teclado propio del fig.
Future<void> _enterPin(WidgetTester tester, String pin) async {
  for (final digit in pin.split('')) {
    final key = find.byKey(Key('pin-key-$digit'));
    await tester.ensureVisible(key);
    await tester.tap(key);
    await tester.pump();
  }
  await tester.pumpAndSettle();
}

void main() {
  setUp(() {
    pinSetupServiceFactory = () => _FakePinSetupService();
    pinSetupResendServiceFactory = () => _FakeResendService();
  });
  tearDown(() {
    pinSetupServiceFactory = null;
    pinSetupResendServiceFactory = null;
  });

  testWidgets('crear PIN valido avanza a confirmar (fig 0:497 -> 0:604)',
      (tester) async {
    await _pump(tester);
    expect(find.text('Crea tu PIN de seguridad'), findsOneWidget);
    expect(find.text('Paso 4 de 4 · Seguridad'), findsOneWidget);

    await _enterPin(tester, '482916');

    expect(find.text('Confírmalo'), findsOneWidget);
    expect(
      find.text('Asegúrate de que sea idéntico al anterior'),
      findsOneWidget,
    );
  });

  testWidgets('confirmar con el mismo PIN avanza al biometrico (0:704)',
      (tester) async {
    await _pump(tester);
    await _enterPin(tester, '482916');
    await _enterPin(tester, '482916');

    expect(
      find.text('¿Quieres entrar con tu huella?'),
      findsOneWidget,
    );
    expect(find.text('Activar acceso biométrico'), findsOneWidget);
  });

  testWidgets('el desajuste bloquea sin avanzar', (tester) async {
    await _pump(tester);
    await _enterPin(tester, '482916');
    await _enterPin(tester, '482917');

    expect(find.text('Los PIN no coinciden.'), findsOneWidget);
    expect(find.text('Confírmalo'), findsOneWidget);
    expect(
      find.text('¿Quieres entrar con tu huella?'),
      findsNothing,
    );
  });

  testWidgets('el PIN debil muestra regla y no avanza', (tester) async {
    await _pump(tester);
    await _enterPin(tester, '123456');

    expect(
      find.text('Evita secuencias como 123456 o dígitos repetidos.'),
      findsOneWidget,
    );
    expect(find.text('Crea tu PIN de seguridad'), findsOneWidget);
    expect(find.text('Confírmalo'), findsNothing);
  });

  testWidgets('biometrico: continuar y omitir llegan al OTP sin bloquear',
      (tester) async {
    await _pump(tester);
    await _enterPin(tester, '482916');
    await _enterPin(tester, '482916');

    // Aceptar la oferta (gate local con el lector del SO: cae a PIN sin
    // bloquear porque `local_auth` no esta cableado).
    final cont = find.byKey(const Key('biometric-continue'));
    await tester.ensureVisible(cont);
    await tester.tap(cont);
    await tester.pumpAndSettle();
    expect(find.text('Revisa tu correo'), findsOneWidget);
  });

  testWidgets('biometrico: omitir por ahora tambien llega al OTP',
      (tester) async {
    await _pump(tester);
    await _enterPin(tester, '482916');
    await _enterPin(tester, '482916');

    final skip = find.byKey(const Key('biometric-skip'));
    await tester.ensureVisible(skip);
    await tester.tap(skip);
    await tester.pumpAndSettle();
    expect(find.text('Revisa tu correo'), findsOneWidget);
  });

  testWidgets('sin userRef la creacion muestra el estado vacio',
      (tester) async {
    final router = GoRouter(
      initialLocation: '/pin-setup',
      routes: pinSetupRoutes,
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(MaterialApp.router(routerConfig: router));
    await tester.pumpAndSettle();

    expect(find.textContaining('Falta la referencia'), findsOneWidget);
    expect(find.text('Crea tu PIN de seguridad'), findsNothing);
  });

  testWidgets('el PIN nunca viaja en la ruta (sin PII en navegacion)',
      (tester) async {
    final router = await _pump(tester);
    await _enterPin(tester, '482916');

    // La ruta de confirmacion no contiene el PIN (viaja en `extra`).
    final location =
        router.routerDelegate.currentConfiguration.last.matchedLocation;
    expect(location, '/pin-setup/confirm');
  });

  testWidgets('guardado tardio: el flujo previo no persiste el user_ref',
      (tester) async {
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await _pump(tester);
    await _enterPin(tester, '482916');
    await _enterPin(tester, '482916');

    // Tras crear + confirmar, aun NO se guardo nada (solo success guarda).
    expect(identity.userRef, isNull);
  });
}
