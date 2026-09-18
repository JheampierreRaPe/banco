import 'package:go_router/go_router.dart';

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
/// Nota de convivencia: las paginas quedan con servicio inyectable (`service`
/// opcional). El orquestador las montara con el `AccountsService` real cuando
/// exista DI global de sesion/API (pendiente F-futuro); mientras tanto las
/// rutas navegan a las pantallas y estas muestran un error accionable si el
/// servicio no esta configurado.
final List<GoRoute> accountsRoutes = [
  GoRoute(
    path: '/accounts',
    builder: (context, state) => const DashboardPage(),
  ),
  GoRoute(
    path: '/accounts/:accountId',
    builder: (context, state) {
      final accountId = state.pathParameters['accountId'] ?? '';
      return AccountDetailPage(accountId: accountId);
    },
  ),
];
