import 'package:banca_online/core/router/app_router.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/login/login_routes.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

// Guarda por `userRef` + sesion (F-T37, SCR-005 d5): sin `WelcomeSeenStore`.

Widget _harness(
  InMemorySessionRepository session,
  InMemorySessionIdentityStore identity, {
  String initial = '/home',
}) {
  final router = buildRouter(
    session,
    identity: identity,
    initialLocation: initial,
  );
  return MaterialApp.router(routerConfig: router);
}

Future<void> _pumpSettled(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 100));
  await tester.pump(const Duration(milliseconds: 100));
}

void main() {
  testWidgets('sin sesion y sin userRef en /home muestra el onboarding',
      (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await tester.pumpWidget(_harness(session, identity));
    await tester.pumpAndSettle();

    expect(find.text('Tu banco, sin colas ni papeles'), findsOneWidget);
    expect(find.text('Crear mi cuenta'), findsOneWidget);
    expect(find.text('Inicia sesión'), findsNothing);
  });

  testWidgets('sin sesion y con userRef en /home resuelve /login',
      (tester) async {
    debugDisableLoginAutoTick = true;
    addTearDown(() => debugDisableLoginAutoTick = false);
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore(userRef: 'u-1');
    addTearDown(identity.dispose);
    await tester.pumpWidget(_harness(session, identity));
    await _pumpSettled(tester);

    // Tras el ensamblado de fase 6, /login es la pantalla real (E1-T16).
    expect(find.text('Inicia sesión'), findsOneWidget);
    expect(find.text('Crear mi cuenta'), findsNothing);
  });

  testWidgets('sin sesion y sin userRef en /welcome permanece ahi',
      (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await tester.pumpWidget(
      _harness(session, identity, initial: '/welcome'),
    );
    await tester.pumpAndSettle();

    expect(find.text('Crear mi cuenta'), findsOneWidget);
    expect(find.text('Comenzar'), findsNothing);
  });

  testWidgets('sin sesion y sin userRef en /login redirige al onboarding',
      (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await tester.pumpWidget(
      _harness(session, identity, initial: '/login'),
    );
    await tester.pumpAndSettle();

    expect(find.text('Crear mi cuenta'), findsOneWidget);
    expect(find.text('Inicia sesión'), findsNothing);
  });

  testWidgets('con sesion navegando a /home muestra inicio', (tester) async {
    final session = InMemorySessionRepository(
      initialAccessToken: 'token-de-prueba',
    );
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await tester.pumpWidget(_harness(session, identity));
    await tester.pumpAndSettle();

    // F-T41: `/home` monta el inicio real (cabecera fija "Hola" en los 4
    // estados); el placeholder quedo eliminado.
    expect(find.text('Hola'), findsOneWidget);
    expect(
      find.text('Pantalla de inicio (placeholder F-T01).'),
      findsNothing,
    );
  });

  testWidgets('con sesion navegando a /login redirige a /home',
      (tester) async {
    final session = InMemorySessionRepository(
      initialAccessToken: 'token-de-prueba',
    );
    final identity = InMemorySessionIdentityStore(userRef: 'u-1');
    addTearDown(identity.dispose);
    await tester.pumpWidget(_harness(session, identity, initial: '/login'));
    await tester.pumpAndSettle();

    // F-T41: `/home` monta el inicio real (cabecera fija "Hola" en los 4
    // estados); el placeholder quedo eliminado.
    expect(find.text('Hola'), findsOneWidget);
    expect(
      find.text('Pantalla de inicio (placeholder F-T01).'),
      findsNothing,
    );
  });

  testWidgets('con sesion navegando a /welcome redirige a /home',
      (tester) async {
    final session = InMemorySessionRepository(
      initialAccessToken: 'token-de-prueba',
    );
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await tester.pumpWidget(
      _harness(session, identity, initial: '/welcome'),
    );
    await tester.pumpAndSettle();

    // F-T41: `/home` monta el inicio real (cabecera fija "Hola" en los 4
    // estados); el placeholder quedo eliminado.
    expect(find.text('Hola'), findsOneWidget);
    expect(
      find.text('Pantalla de inicio (placeholder F-T01).'),
      findsNothing,
    );
  });

  testWidgets('con sesion navegando a /entry redirige a /home',
      (tester) async {
    final session = InMemorySessionRepository(
      initialAccessToken: 'token-de-prueba',
    );
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await tester.pumpWidget(_harness(session, identity, initial: '/entry'));
    await tester.pumpAndSettle();

    // F-T41: `/home` monta el inicio real (cabecera fija "Hola" en los 4
    // estados); el placeholder quedo eliminado.
    expect(find.text('Hola'), findsOneWidget);
    expect(
      find.text('Pantalla de inicio (placeholder F-T01).'),
      findsNothing,
    );
  });

  testWidgets('sin sesion con userRef en /login muestra login (publica)',
      (tester) async {
    // La pagina real usa un Timer periodico; en tests del router se
    // desactiva para que el framework pueda asentar (ver login_routes.dart).
    debugDisableLoginAutoTick = true;
    addTearDown(() => debugDisableLoginAutoTick = false);
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore(userRef: 'u-1');
    addTearDown(identity.dispose);
    await tester.pumpWidget(_harness(session, identity, initial: '/login'));
    // Sin pumpAndSettle: la pagina real de login (E1-T16) mantiene un Timer
    // periodico de inactividad que nunca deja "asentar" al framework.
    await _pumpSettled(tester);

    // Tras el ensamblado de fase 6, /login es la pantalla real (E1-T16).
    expect(find.text('Inicia sesión'), findsOneWidget);
    expect(find.text('Iniciar sesión'), findsNothing);
  });

  testWidgets('sin sesion y sin userRef en /pin-reset permanece ahi',
      (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await tester.pumpWidget(
      _harness(session, identity, initial: '/pin-reset'),
    );
    await tester.pumpAndSettle();

    // F-T43: `/pin-reset` es pública pre-login (no abre sesión).
    expect(find.text('Crea un nuevo PIN'), findsOneWidget);
    expect(find.text('Tu banco, sin colas ni papeles'), findsNothing);
  });

  testWidgets('sin sesion y con userRef en /pin-reset muestra el flujo',
      (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore(userRef: 'u-1');
    addTearDown(identity.dispose);
    await tester.pumpWidget(
      _harness(session, identity, initial: '/pin-reset'),
    );
    await tester.pumpAndSettle();

    expect(find.text('Crea un nuevo PIN'), findsOneWidget);
    expect(find.text('Inicia sesión'), findsNothing);
  });

  testWidgets('buildRouter hidrata userRef/deviceId en LoginRouteDeps',
      (tester) async {
    debugDisableLoginAutoTick = true;
    addTearDown(() => debugDisableLoginAutoTick = false);
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore(
      userRef: 'user-123',
      deviceId: 'device-abc',
    );
    addTearDown(identity.dispose);
    final router = buildRouter(
      session,
      identity: identity,
      initialLocation: '/login',
    );
    addTearDown(router.dispose);

    final deps = loginRouteDepsFactory!();
    expect(deps.userRef, 'user-123');
    expect(deps.deviceId, 'device-abc');

    await tester.pumpWidget(MaterialApp.router(routerConfig: router));
    await _pumpSettled(tester);
    expect(find.text('Inicia sesión'), findsOneWidget);
  });
}
