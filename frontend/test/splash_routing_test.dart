import 'dart:async';

import 'package:banca_online/core/router/app_router.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/login/login_routes.dart';
import 'package:banca_online/features/splash/splash_routes.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

// Arranque por `/splash` con la guarda por `userRef` (F-T37, SCR-005 d5):
// sin `WelcomeSeenStore`; la splash delega en la guarda.

Widget _harness(
  InMemorySessionRepository session,
  InMemorySessionIdentityStore identity, {
  String initial = '/splash',
}) {
  final router = buildRouter(
    session,
    identity: identity,
    initialLocation: initial,
  );
  return MaterialApp.router(routerConfig: router);
}

void main() {
  setUp(() {
    // Espera inmediata: la splash delega en la guarda sin temporizador real.
    debugSplashWaitOverride = () async {};
  });
  tearDown(() {
    debugSplashWaitOverride = null;
  });

  testWidgets('/splash es publica sin sesion (se queda con espera pendiente)',
      (tester) async {
    debugSplashWaitOverride = () => Completer<void>().future;
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await tester.pumpWidget(_harness(session, identity));
    // Sin pumpAndSettle: el indicador indeterminado de la splash anima en
    // bucle y el framework nunca "asienta".
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pump(const Duration(milliseconds: 100));

    expect(find.text('CuyCash'), findsOneWidget);
    expect(find.text('Comenzar'), findsNothing);
    expect(find.text('Iniciar sesión'), findsNothing);
  });

  testWidgets('sin userRef: tras la espera la guarda lleva a /welcome',
      (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);
    await tester.pumpWidget(_harness(session, identity));
    await tester.pumpAndSettle();

    expect(find.text('Crear mi cuenta'), findsOneWidget);
    expect(find.text('CuyCash'), findsNothing);
  });

  testWidgets('con userRef: tras la espera la guarda lleva a /login',
      (tester) async {
    // Sin el tick periodico de la pagina real de login (E1-T16).
    debugDisableLoginAutoTick = true;
    addTearDown(() => debugDisableLoginAutoTick = false);
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore(userRef: 'u-1');
    addTearDown(identity.dispose);
    await tester.pumpWidget(_harness(session, identity));
    await tester.pumpAndSettle();

    // La splash navega a `/home` (privada); con `userRef` y sin sesion la
    // guarda resuelve `/login`.
    expect(find.text('Inicia sesión'), findsOneWidget);
    expect(find.text('CuyCash'), findsNothing);
    expect(find.text('Crear mi cuenta'), findsNothing);
  });

  testWidgets('con sesion: tras la espera la guarda deja /home',
      (tester) async {
    final session = InMemorySessionRepository(
      initialAccessToken: 'token-de-prueba',
    );
    final identity = InMemorySessionIdentityStore(userRef: 'u-1');
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
    expect(find.text('CuyCash'), findsNothing);
  });

  testWidgets('con sesion en /splash redirige a /home', (tester) async {
    debugSplashWaitOverride = () => Completer<void>().future;
    final session = InMemorySessionRepository(
      initialAccessToken: 'token-de-prueba',
    );
    final identity = InMemorySessionIdentityStore(userRef: 'u-1');
    addTearDown(identity.dispose);
    await tester.pumpWidget(_harness(session, identity));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pump(const Duration(milliseconds: 100));

    // La guarda redirige la splash a `/home` con sesion activa.
    // F-T41: `/home` monta el inicio real (cabecera fija "Hola" en los 4
    // estados); el placeholder quedo eliminado.
    expect(find.text('Hola'), findsOneWidget);
    expect(
      find.text('Pantalla de inicio (placeholder F-T01).'),
      findsNothing,
    );
    expect(find.text('CuyCash'), findsNothing);
  });
}
