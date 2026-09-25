// Pruebas de la ruta `/login` (F-T40): el `userRef` llega por query
// (`/login?userRef=`, destino tras el restablecimiento de PIN) o por
// `LoginRouteDeps.userRef` (store F-T20).
//
// - Con query y store vacío: la página usa el `userRef` de la query.
// - Sin query: la página usa el `userRef` del store.
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/login/login_page.dart';
import 'package:banca_online/features/login/login_routes.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

Future<void> _pumpLogin(
  WidgetTester tester,
  GoRouter router,
) async {
  await tester.pumpWidget(MaterialApp.router(routerConfig: router));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 100));
}

GoRouter _routerWithDeps({
  required InMemorySessionRepository session,
  required String storeUserRef,
  required String initialLocation,
}) {
  final api = ApiClient.create(
    session: session,
    dioOverride: Dio(),
    refreshDioOverride: Dio(),
    uuid: const Uuid(),
  );
  loginRouteDepsFactory = () => LoginRouteDeps(
        api: api,
        session: session,
        reader: FakeBiometricReader(available: false),
        userRef: storeUserRef,
        deviceId: 'd-1',
      );
  return GoRouter(
    initialLocation: initialLocation,
    routes: loginRoutes,
  );
}

void main() {
  testWidgets('login?userRef= usa el userRef de query cuando no hay store',
      (tester) async {
    debugDisableLoginAutoTick = true;
    addTearDown(() => debugDisableLoginAutoTick = false);
    addTearDown(() => loginRouteDepsFactory = null);
    final session = InMemorySessionRepository();
    final router = _routerWithDeps(
      session: session,
      storeUserRef: '',
      initialLocation: '/login?userRef=q-9',
    );
    addTearDown(router.dispose);

    await _pumpLogin(tester, router);

    final page = tester.widget<LoginPage>(find.byType(LoginPage));
    expect(page.userRef, 'q-9');
  });

  testWidgets('sin query usa el userRef del store', (tester) async {
    debugDisableLoginAutoTick = true;
    addTearDown(() => debugDisableLoginAutoTick = false);
    addTearDown(() => loginRouteDepsFactory = null);
    final session = InMemorySessionRepository();
    final router = _routerWithDeps(
      session: session,
      storeUserRef: 'store-u',
      initialLocation: '/login',
    );
    addTearDown(router.dispose);

    await _pumpLogin(tester, router);

    final page = tester.widget<LoginPage>(find.byType(LoginPage));
    expect(page.userRef, 'store-u');
  });
}
