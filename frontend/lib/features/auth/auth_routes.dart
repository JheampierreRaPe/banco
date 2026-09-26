import 'package:go_router/go_router.dart';

import 'presentation/login_placeholder_page.dart';

/// Rutas del feature `auth`.
///
/// Convencion F-T01: cada feature expone `List<GoRoute> <feature>Routes` en su
/// archivo `*_routes.dart` para que el orquestador (`core/router`) las agregue
/// SIN que cada feature toque el router global.
final List<GoRoute> authRoutes = [
  GoRoute(
    path: '/login',
    builder: (context, state) => const LoginPlaceholderPage(),
  ),
];
