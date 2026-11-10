import 'package:go_router/go_router.dart';

import 'data/accounts_service.dart';
import 'presentation/account_detail_page.dart';
import 'presentation/dashboard_page.dart';

/// Rutas del feature `accounts` (E2-T05).
///
/// Convencion F-T01 (`features/README.md`): el feature NUNCA toca
/// `lib/core/router/app_router.dart`; el orquestador importa esta lista y
/// hace `...accountsRoutes`.
///
/// - `/accounts` -> [DashboardPage] (consolidado de cuentas y saldos).
/// - `/accounts/:accountId` -> [AccountDetailPage] (detalle + movimientos).
///
/// Nota de convivencia: las paginas reciben el servicio desde
/// `accountsServiceFactory` (cableada por el orquestador `app_router.dart`
/// con el `ApiClient` compartido, F-T41). Si la fabrica es `null` (DI no
/// configurada, p. ej. en tests de rutas), las pantallas muestran un error
/// accionable en vez de romper.
final List<GoRoute> accountsRoutes = [
  GoRoute(
    path: '/accounts',
    builder: (context, state) =>
        DashboardPage(service: accountsServiceFactory?.call()),
  ),
  GoRoute(
    path: '/accounts/:accountId',
    builder: (context, state) {
      final accountId = state.pathParameters['accountId'] ?? '';
      return AccountDetailPage(
        accountId: accountId,
        service: accountsServiceFactory?.call(),
      );
    },
  ),
];
