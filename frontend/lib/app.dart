import 'package:flutter/material.dart';

import 'core/router/app_router.dart';
import 'core/session/session_repository.dart';
import 'core/theme/app_theme.dart';

/// Raiz de la app (Material 3 + go_router).
class BancaOnlineApp extends StatelessWidget {
  const BancaOnlineApp({super.key, required this.session});

  final SessionRepository session;

  @override
  Widget build(BuildContext context) {
    final router = buildRouter(session);
    return MaterialApp.router(
      title: 'Banca Online',
      theme: AppTheme.light,
      darkTheme: AppTheme.dark,
      routerConfig: router,
    );
  }
}
