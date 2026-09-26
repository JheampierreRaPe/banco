import 'package:go_router/go_router.dart';

import '../accounts/data/accounts_service.dart';
import 'data/profile_service.dart';
import 'presentation/home_page.dart';

/// Rutas del feature `home` (ver convencion en `auth_routes.dart`).
///
/// `/home` monta el inicio redisenado (F-T54, diseno `dash`). Los servicios
/// se resuelven desde `accountsServiceFactory`/`homeProfileServiceFactory`
/// (cableadas por el orquestador); `null` degrada con elegancia (fallback
/// `Hola` para el perfil; error accionable para las cuentas).
final List<GoRoute> homeRoutes = [
  GoRoute(
    path: '/home',
    builder: (context, state) => HomePage(
      service: accountsServiceFactory?.call(),
      profileService: homeProfileServiceFactory?.call(),
    ),
  ),
];
