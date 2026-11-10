// Paso success del alta (F-T39, fig `0:740`): UNICO lugar donde se persiste
// el `user_ref` (guardado tardio, SCR-005).
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/pin_setup/registration_success_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Store en memoria que cuenta las persistencias (regresion del guardado
/// temprano: debe llamarse UNA vez y solo aqui).
class _CountingIdentityStore extends InMemorySessionIdentityStore {
  _CountingIdentityStore({super.deviceId});

  int saveCalls = 0;

  @override
  Future<void> saveUserRef(String userRef) async {
    saveCalls++;
    await super.saveUserRef(userRef);
  }
}

/// Store que falla al guardar (secure storage caido).
class _ThrowingIdentityStore extends InMemorySessionIdentityStore {
  @override
  Future<void> saveUserRef(String userRef) async =>
      throw StateError('secure storage caido');
}

GoRouter _router(SessionIdentityStore identity, {String userRef = 'u-42'}) =>
    GoRouter(
      initialLocation: '/success?userRef=$userRef',
      routes: [
        GoRoute(
          path: '/success',
          builder: (context, state) => RegistrationSuccessPage(
            userRef: state.uri.queryParameters['userRef'] ?? '',
            identity: identity,
          ),
        ),
        GoRoute(
          path: '/login',
          builder: (context, state) => Scaffold(
            body: Text(
              'login-ok:${state.uri.queryParameters['userRef']}/'
              '${state.uri.queryParameters['deviceId']}',
            ),
          ),
        ),
        GoRoute(
          path: '/kyc',
          builder: (context, state) =>
              const Scaffold(body: Text('kyc-ok')),
        ),
      ],
    );

Future<void> _pump(
  WidgetTester tester,
  InMemorySessionIdentityStore identity, {
  String userRef = 'u-42',
}) async {
  final router = _router(identity, userRef: userRef);
  addTearDown(router.dispose);
  addTearDown(identity.dispose);
  await tester.pumpWidget(MaterialApp.router(routerConfig: router));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('success guarda el user_ref UNA vez y muestra el contenido',
      (tester) async {
    final identity = _CountingIdentityStore(deviceId: 'd-1');
    await _pump(tester, identity);

    expect(find.text('¡Tu cuenta está activa!'), findsOneWidget);
    expect(
      find.textContaining('Ya puedes empezar a mover tu dinero'),
      findsOneWidget,
    );
    expect(identity.userRef, 'u-42');
    expect(identity.saveCalls, 1);
  });

  testWidgets('continuar al login lleva userRef + deviceId', (tester) async {
    final identity = _CountingIdentityStore(deviceId: 'd-1');
    await _pump(tester, identity);

    final login = find.byKey(const Key('registration-success-login'));
    await tester.ensureVisible(login);
    await tester.tap(login);
    await tester.pumpAndSettle();

    expect(find.text('login-ok:u-42/d-1'), findsOneWidget);
    // Sin doble guardado al navegar.
    expect(identity.saveCalls, 1);
  });

  testWidgets('fallo del secure storage avisa pero no bloquea',
      (tester) async {
    await _pump(tester, _ThrowingIdentityStore());

    expect(find.text('¡Tu cuenta está activa!'), findsOneWidget);
    expect(find.textContaining('No pudimos guardar'), findsOneWidget);

    // Drenar el temporizador del SnackBar para no dejar timers pendientes.
    await tester.pump(const Duration(seconds: 5));
    await tester.pumpAndSettle();
  });

  testWidgets('sin userRef muestra el estado vacio', (tester) async {
    final identity = _CountingIdentityStore(deviceId: 'd-1');
    await _pump(tester, identity, userRef: '');

    expect(find.textContaining('Falta la referencia'), findsOneWidget);
    expect(identity.saveCalls, 0);

    await tester.tap(find.text('Volver al registro'));
    await tester.pumpAndSettle();
    expect(find.text('kyc-ok'), findsOneWidget);
  });
}
