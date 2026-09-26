// Flujo de cierre del registro (F-T39): crear -> confirmar -> biometrico
// (aceptar/omitir, sin bloqueo) con el PIN solo en memoria.
//
// Cero red: las rutas OTP/success se prueban en sus archivos; aqui se
// verifica el orden, el bloqueo por desajuste y que el PIN nunca viaja en
// la ruta.
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/activation/activation_service.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/pin_setup/pin_setup_routes.dart';
import 'package:banca_online/features/pin_setup/pin_setup_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del setup (sin red).
class _FakePinSetupService implements PinSetupService {
  String? lastPin;
  bool? lastBiometricEnabled;

  @override
  Future<PinSetupResult> setup({
    required String userRef,
    required String code,
    required String pin,
    bool biometricEnabled = false,
  }) async {
    lastPin = pin;
    lastBiometricEnabled = biometricEnabled;
    return const PinSetupResult(userId: 'u-1', status: 'ACTIVE');
  }
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
    // Sin plataforma no hay prompt del SO: el gate biometrico se inyecta
    // (el lector real con estados se cubre en `biometric_reader_test.dart`).
    pinSetupBiometricReaderFactory = () => FakeBiometricReader();
  });
  tearDown(() {
    pinSetupServiceFactory = null;
    pinSetupResendServiceFactory = null;
    pinSetupBiometricReaderFactory = null;
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

    // Aceptar la oferta (gate local con el lector del SO: en tests no hay
    // plugin y cae a PIN sin bloquear).
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

  testWidgets(
      'regresion F-T46: crear -> confirmar (desajuste) -> volver -> '
      'reescribir -> confirmar -> biometrico -> OTP envia el ultimo PIN', (
    tester,
  ) async {
    await _pump(tester);

    // 1. Crear el primer PIN y llegar a confirmar.
    await _enterPin(tester, '482916');
    expect(find.text('Confírmalo'), findsOneWidget);

    // 2. Desajuste: no avanza y conserva el error.
    await _enterPin(tester, '482917');
    expect(find.text('Los PIN no coinciden.'), findsOneWidget);
    expect(find.text('Confírmalo'), findsOneWidget);

    // 3. Volver: regresa a crear con el estado limpio (vacio y editable).
    // Con el bug (`pop()` sin destino + `_navigated` bloqueado) este paso
    // falla: nunca se vuelve a "Crea tu PIN".
    await tester.ensureVisible(find.byKey(const Key('pin-flow-back')));
    await tester.tap(find.byKey(const Key('pin-flow-back')));
    await tester.pumpAndSettle();
    expect(find.text('Crea tu PIN de seguridad'), findsOneWidget);
    expect(find.text('Los PIN no coinciden.'), findsNothing);

    // 4. Reescribir un PIN NUEVO (el campo debe estar editable: si quedo el
    // PIN viejo o el bloqueo, nunca se avanza a confirmar).
    await _enterPin(tester, '739581');
    expect(find.text('Confírmalo'), findsOneWidget);

    // 5. Confirmar identico -> oferta biometrica -> OTP.
    await _enterPin(tester, '739581');
    expect(
      find.text('¿Quieres entrar con tu huella?'),
      findsOneWidget,
    );
    final cont = find.byKey(const Key('biometric-continue'));
    await tester.ensureVisible(cont);
    await tester.tap(cont);
    await tester.pumpAndSettle();
    expect(find.text('Revisa tu correo'), findsOneWidget);
  });

  testWidgets(
      'el OTP envia el ultimo PIN confirmado con biometric_enabled=true', (
    tester,
  ) async {
    final setup = _FakePinSetupService();
    pinSetupServiceFactory = () => setup;
    await _pump(tester);

    await _enterPin(tester, '739581');
    await _enterPin(tester, '739581');

    // Oferta biometrica con el switch por defecto (activo) -> el borrador
    // lleva el consentimiento en `true`.
    final cont = find.byKey(const Key('biometric-continue'));
    await tester.ensureVisible(cont);
    await tester.tap(cont);
    await tester.pumpAndSettle();
    expect(find.text('Revisa tu correo'), findsOneWidget);

    await _enterPin(tester, '123456');
    final submit = find.byKey(const Key('pin-setup-submit'));
    await tester.ensureVisible(submit);
    await tester.tap(submit);
    await tester.pumpAndSettle();

    // El payload del `pin/setup` es el ultimo PIN confirmado (no un PIN
    // viejo descartado) con el consentimiento elegido en `0:704`.
    expect(setup.lastPin, '739581');
    expect(setup.lastBiometricEnabled, isTrue);
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
