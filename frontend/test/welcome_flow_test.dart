import 'package:banca_online/core/router/app_router.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/secure_key_value_storage.dart';
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

void main() {
  testWidgets('Comenzar marca el flag y lleva a /entry', (tester) async {
    final session = InMemorySessionRepository();
    final seen = InMemoryWelcomeSeenStore(initialSeen: false);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen));
    await tester.pumpAndSettle();
    expect(find.text('Comenzar'), findsOneWidget);

    await tester.tap(find.text('Comenzar'));
    await tester.pumpAndSettle();

    expect(seen.hasSeenWelcome, isTrue);
    expect(find.text('Iniciar sesión'), findsOneWidget);
    expect(find.text('Crear cuenta'), findsOneWidget);
  });

  testWidgets('segunda apertura salta la bienvenida (va a /entry)',
      (tester) async {
    final session = InMemorySessionRepository();
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen));
    await tester.pumpAndSettle();

    expect(find.text('Comenzar'), findsNothing);
    expect(find.text('Iniciar sesión'), findsOneWidget);
  });

  testWidgets('entry: Iniciar sesión va a /login', (tester) async {
    debugDisableLoginAutoTick = true;
    addTearDown(() => debugDisableLoginAutoTick = false);
    final session = InMemorySessionRepository();
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen, initial: '/entry'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Iniciar sesión'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pump(const Duration(milliseconds: 100));

    expect(find.text('Inicia sesión'), findsOneWidget);
  });

  testWidgets('entry: Crear cuenta va al flujo KYC', (tester) async {
    final session = InMemorySessionRepository();
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen, initial: '/entry'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Crear cuenta'));
    await tester.pumpAndSettle();

    expect(find.text('Verifica tu identidad'), findsOneWidget);
  });

  testWidgets('/kyc sigue publica sin sesion', (tester) async {
    final session = InMemorySessionRepository();
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen, initial: '/kyc'));
    await tester.pumpAndSettle();

    expect(find.text('Verifica tu identidad'), findsOneWidget);
    expect(find.text('Iniciar sesión'), findsNothing);
  });

  testWidgets('/activate sigue publica sin sesion', (tester) async {
    final session = InMemorySessionRepository();
    final seen = InMemoryWelcomeSeenStore(initialSeen: true);
    addTearDown(seen.dispose);
    await tester.pumpWidget(_harness(session, seen, initial: '/activate'));
    await tester.pumpAndSettle();

    // Sin userRef muestra el error accionable, NO redirige a /entry.
    expect(find.text('Activa tu cuenta'), findsOneWidget);
    expect(find.text('Iniciar sesión'), findsNothing);
  });

  test('flag persiste en almacenamiento seguro', () async {
    final storage = InMemorySecureStorage();
    final first = SecureWelcomeSeenStore(storage: storage);
    addTearDown(first.dispose);
    await first.load();
    expect(first.hasSeenWelcome, isFalse);

    await first.markSeenWelcome();
    expect(first.hasSeenWelcome, isTrue);

    final second = SecureWelcomeSeenStore(storage: storage);
    addTearDown(second.dispose);
    await second.load();
    expect(second.hasSeenWelcome, isTrue);
  });
}
