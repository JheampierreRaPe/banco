import 'package:go_router/go_router.dart';

import 'presentation/home_placeholder_page.dart';

/// Rutas del feature `home` (ver convencion en `auth_routes.dart`).
final List<GoRoute> homeRoutes = [
  GoRoute(
    path: '/home',
    builder: (context, state) => const HomePlaceholderPage(),
  ),
];
