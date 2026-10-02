import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/widgets/error_view.dart';
import 'data/movements_service.dart';
import 'presentation/movement_detail_page.dart';
import 'presentation/movements_page.dart';

/// Rutas del feature `movements` (Historial + Reverso, E2-T24/E2-T25).
///
/// Convencion F-T01 (`features/README.md`): el feature NUNCA toca
/// `lib/core/router/app_router.dart`; el orquestador importa esta lista y
/// hace `...movementsRoutes` (mas la fabrica `movementsServiceFactory`,
/// igual que `beneficiariesServiceFactory`).
///
/// - `/movements` -> [MovementsPage] (historial paginado + filtros
///   fecha/tipo/estado + busqueda local).
/// - `/movements/:id` -> [MovementDetailPage] (detalle + reverso con
///   confirmacion + constancia).
///
/// Las paginas reciben el servicio desde `movementsServiceFactory`
/// (cableada por el orquestador con el `ApiClient` compartido). Si la
/// fabrica es `null`, muestran un error accionable en vez de romper.
final List<GoRoute> movementsRoutes = [
  GoRoute(
    path: '/movements',
    builder: (context, state) => MovementsPage(
      service: movementsServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/movements/:id',
    builder: (context, state) {
      final id = state.pathParameters['id'] ?? '';
      if (id.isEmpty) {
        return const _MissingId();
      }
      return MovementDetailPage(
        transactionId: id,
        service: movementsServiceFactory?.call(),
      );
    },
  ),
];

/// Error accionable cuando la ruta llega sin `:id`.
class _MissingId extends StatelessWidget {
  const _MissingId();

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Detalle de movimiento')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: ErrorView(
          message: 'Falta el identificador del movimiento. '
              'Vuelve al historial e inténtalo de nuevo.',
          onRetry: () => context.pop(),
          retryLabel: 'Volver',
        ),
      ),
    );
  }
}
