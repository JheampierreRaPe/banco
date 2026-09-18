import 'package:banca_online/core/router/app_router.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/login/login_routes.dart';
import 'package:banca_online/features/welcome/welcome_seen_store.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

Widget _harness(
  InMemorySessionRepository session,
  InMemoryWelcomeSeenStore seen, {
  String initial = '/home',
}) {
  final router = buildRouter(
    session,
    welcomeSeen: seen,
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
  testWidgets('primera vez sin sesion en /home muestra bienvenida',
      (tester) async {
    final session = InMemorySessionRepository();
    final seen = InMemoryWelcomeSeenStore(initialSeen: false);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen));
    await tester.pumpAndSettle();

    expect(find.text('Banca Online Integral'), findsWidgets);
    expect(find.text('Comenzar'), findsOneWidget);
    expect(find.text('Inicia sesión'), findsNothing);
  });

  testWidgets('sin sesion y bienvenida vista en /home redirige a /entry',
      (tester) async {
    final session = InMemorySessionRepository();
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen));
    await tester.pumpAndSettle();

    expect(find.text('Iniciar sesión'), findsOneWidget);
    expect(find.text('Crear cuenta'), findsOneWidget);
    expect(find.text('Comenzar'), findsNothing);
  });

  testWidgets('sin sesion y bienvenida vista en /welcome redirige a /entry',
      (tester) async {
    final session = InMemorySessionRepository();
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen, initial: '/welcome'));
    await tester.pumpAndSettle();

    expect(find.text('Iniciar sesión'), findsOneWidget);
    expect(find.text('Comenzar'), findsNothing);
  });

  testWidgets('con sesion navegando a /home muestra inicio', (tester) async {
    final session = InMemorySessionRepository(
      initialAccessToken: 'token-de-prueba',
    );
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen));
    await tester.pumpAndSettle();

    expect(
      find.text('Pantalla de inicio (placeholder F-T01).'),
      findsOneWidget,
    );
  });

  testWidgets('con sesion navegando a /login redirige a /home',
      (tester) async {
    final session = InMemorySessionRepository(
      initialAccessToken: 'token-de-prueba',
    );
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen, initial: '/login'));
    await tester.pumpAndSettle();

    expect(
      find.text('Pantalla de inicio (placeholder F-T01).'),
      findsOneWidget,
    );
  });

  testWidgets('con sesion navegando a /entry redirige a /home',
      (tester) async {
    final session = InMemorySessionRepository(
      initialAccessToken: 'token-de-prueba',
    );
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen, initial: '/entry'));
    await tester.pumpAndSettle();

    expect(
      find.text('Pantalla de inicio (placeholder F-T01).'),
      findsOneWidget,
    );
  });

  testWidgets('sin sesion en /login muestra login (ruta publica directa)',
      (tester) async {
    // La pagina real usa un Timer periodico; en tests del router se
    // desactiva para que el framework pueda asentar (ver login_routes.dart).
    debugDisableLoginAutoTick = true;
    addTearDown(() => debugDisableLoginAutoTick = false);
    final session = InMemorySessionRepository();
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen, initial: '/login'));
    // Sin pumpAndSettle: la pagina real de login (E1-T16) mantiene un Timer
    // periodico de inactividad que nunca deja "asentar" al framework.
    await _pumpSettled(tester);

    // Tras el ensamblado de fase 6, /login es la pantalla real (E1-T16).
    expect(find.text('Inicia sesión'), findsOneWidget);
    expect(find.text('Iniciar sesión'), findsNothing);
  });
}
