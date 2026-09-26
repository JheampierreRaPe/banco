import 'package:flutter/material.dart';

import 'core/router/app_router.dart';
import 'core/session/session_identity_store.dart';
import 'core/session/session_repository.dart';
import 'core/theme/app_theme.dart';

/// Raiz de la app (Material 3 + go_router).
///
/// Recibe la sesion y la identidad ya hidratadas por `main` (F-T02/F-T20) y
/// las monta en el router: el `refreshListenable` escucha sesion + identidad
/// (F-T37) para que la guarda reevalue al guardar el `userRef`.
class BancaOnlineApp extends StatelessWidget {
  const BancaOnlineApp({super.key, required this.session, this.identity});

  final SessionRepository session;

  /// Identidad local hidratada; `null` = el router resuelve la fabrica
  /// global (produccion) o un store seguro por defecto.
  final SessionIdentityStore? identity;

  @override
  Widget build(BuildContext context) {
    // El arranque entra por `/splash`: la splash delega en la guarda, que
    // resuelve onboarding (`/welcome`), login (`/login`) o `/home`.
    final router = buildRouter(
      session,
      identity: identity,
      initialLocation: '/splash',
    );
    return MaterialApp.router(
      title: 'Banca Online',
      theme: AppTheme.light,
      darkTheme: AppTheme.dark,
      // Diseno canonico solo en modo claro (F-T34, docs/20).
      themeMode: ThemeMode.light,
      routerConfig: router,
    );
  }
}
