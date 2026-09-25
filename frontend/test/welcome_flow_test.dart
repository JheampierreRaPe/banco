import 'package:banca_online/core/app_version.dart';
import 'package:banca_online/core/router/app_router.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/login/login_routes.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

// Flujo de onboarding con la guarda por `userRef` (F-T37, SCR-005 d5):
// sin `WelcomeSeenStore`; la guarda depende solo de
// `SessionIdentityStore.userRef` + sesion.

GoRouter _buildRouter(
  InMemorySessionRepository session,
  InMemorySessionIdentityStore identity, {
  String initial = '/home',
}) {
  return buildRouter(
    session,
    identity: identity,
    initialLocation: initial,
  );
}

Widget _harnessWith(GoRouter router) =>
    MaterialApp.router(routerConfig: router);

void main() {
  testWidgets('onboarding muestra el slide 1, indicador y acciones',
      (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    final router = _buildRouter(session, identity);
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();

    expect(find.text('Tu banco, sin colas ni papeles'), findsOneWidget);
    expect(
      find.text(
        'Abre tu cuenta en minutos validando tu DNI y tu rostro.',
      ),
      findsOneWidget,
    );
    expect(
      find.byKey(const Key('onboarding-pageview')),
      findsOneWidget,
    );
    expect(
      find.byKey(const Key('onboarding-indicator')),
      findsOneWidget,
    );
    expect(find.text('Crear mi cuenta'), findsOneWidget);
    expect(
      find.text('Ya tengo cuenta · Restablecer PIN'),
      findsOneWidget,
    );
    expect(find.byKey(const Key('app-version')), findsOneWidget);
    expect(find.text('Version $kAppVersion'), findsOneWidget);
  });

  testWidgets('onboarding permite avanzar por los 3 slides', (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    final router = _buildRouter(session, identity, initial: '/welcome');
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();

    expect(find.text('Tu banco, sin colas ni papeles'), findsOneWidget);

    await tester.drag(
      find.byKey(const Key('onboarding-pageview')),
      const Offset(-400, 0),
    );
    await tester.pumpAndSettle();
    expect(find.text('Envía y cobra en segundos'), findsOneWidget);
    expect(
      find.text(
        'Manda dinero a cualquier persona en CuyCash sin comisiones, '
        'o cobra mostrando tu código.',
      ),
      findsOneWidget,
    );

    await tester.drag(
      find.byKey(const Key('onboarding-pageview')),
      const Offset(-400, 0),
    );
    await tester.pumpAndSettle();
    expect(find.text('Recibe desde otras billeteras'), findsOneWidget);
    expect(
      find.text(
        'El dinero que te envían desde otras apps llega directo '
        'a tu billetera CuyCash.',
      ),
      findsOneWidget,
    );
  });

  testWidgets('Crear mi cuenta va al flujo KYC', (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    final router = _buildRouter(session, identity, initial: '/welcome');
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Crear mi cuenta'));
    await tester.pumpAndSettle();

    // `/kyc` es publico sin `userRef` (alta): muestra el paso de datos
    // (F-T39: `KycStartPage` con titulo `Crear cuenta`).
    expect(router.state.matchedLocation, '/kyc');
    expect(find.text('Crear cuenta'), findsOneWidget);
  });

  testWidgets('Ya tengo cuenta va a /pin-reset (F-T51)', (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    final router = _buildRouter(session, identity, initial: '/welcome');
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Ya tengo cuenta · Restablecer PIN'));
    await tester.pumpAndSettle();

    expect(router.state.matchedLocation, '/pin-reset');
  });

  testWidgets('sin sesion y sin userRef en /home resuelve el onboarding',
      (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    final router = _buildRouter(session, identity);
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();

    expect(find.text('Crear mi cuenta'), findsOneWidget);
    expect(
      find.text('Ya tengo cuenta · Restablecer PIN'),
      findsOneWidget,
    );
  });

  testWidgets('sin sesion y con userRef en /home resuelve /login',
      (tester) async {
    // La pagina real de login mantiene un Timer periodico de inactividad:
    // se desactiva para que el framework pueda asentar.
    debugDisableLoginAutoTick = true;
    addTearDown(() => debugDisableLoginAutoTick = false);
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore(userRef: 'u-1');
    addTearDown(identity.dispose);
    final router = _buildRouter(session, identity);
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();

    expect(router.state.matchedLocation, '/login');
    expect(find.text('Crear mi cuenta'), findsNothing);
  });

  testWidgets('sin userRef /login redirige al onboarding', (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    final router = _buildRouter(session, identity, initial: '/login');
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();

    expect(router.state.matchedLocation, '/welcome');
    expect(find.text('Crear mi cuenta'), findsOneWidget);
  });

  testWidgets('legacy /entry redirige a /welcome', (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    final router = _buildRouter(session, identity, initial: '/entry');
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();

    expect(router.state.matchedLocation, '/welcome');
    expect(find.text('Crear mi cuenta'), findsOneWidget);
  });

  testWidgets('/kyc sigue publica sin sesion ni userRef', (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    final router = _buildRouter(session, identity, initial: '/kyc');
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();

    expect(router.state.matchedLocation, '/kyc');
    expect(find.text('Crear cuenta'), findsOneWidget);
    expect(find.text('Ya tengo cuenta · Restablecer PIN'), findsNothing);
  });

  testWidgets('/activate sigue publica sin sesion ni userRef', (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    final router = _buildRouter(session, identity, initial: '/activate');
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();

    // Sin userRef muestra el error accionable, NO redirige al onboarding.
    expect(find.text('Activa tu cuenta'), findsOneWidget);
    expect(find.text('Crear mi cuenta'), findsNothing);
  });

  testWidgets('saveUserRef reevalua la guarda (habilita /login)',
      (tester) async {
    debugDisableLoginAutoTick = true;
    addTearDown(() => debugDisableLoginAutoTick = false);
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    // La identidad es el miembro del `refreshListenable` del router: al
    // guardar el `userRef` debe notificar sin navegacion manual.
    var notified = 0;
    identity.addListener(() => notified++);
    final router = _buildRouter(session, identity);
    addTearDown(router.dispose);
    await tester.pumpWidget(_harnessWith(router));
    await tester.pumpAndSettle();
    expect(router.state.matchedLocation, '/welcome');

    // El paso success del registro guarda el `userRef` (F-T39).
    await identity.saveUserRef('u-1');
    expect(notified, greaterThan(0));

    // La guarda reevalue con el nuevo estado: una ruta privada ya resuelve
    // a `/login` en lugar del onboarding.
    router.go('/home');
    await tester.pumpAndSettle();
    expect(router.state.matchedLocation, '/login');
  });
}
