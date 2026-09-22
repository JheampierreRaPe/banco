import 'package:go_router/go_router.dart';

import '../accounts/data/accounts_service.dart';
import 'presentation/home_page.dart';

/// Rutas del feature `home` (ver convencion en `auth_routes.dart`).
///
/// `/home` monta el inicio real (F-T41, diseno F-T42). El servicio se resuelve
/// desde `accountsServiceFactory` (cableada por el orquestador); `null`
/// muestra el estado de error accionable de la pantalla.
final List<GoRoute> homeRoutes = [
  GoRoute(
    path: '/home',
    builder: (context, state) =>
        HomePage(service: accountsServiceFactory?.call()),
  ),
];
