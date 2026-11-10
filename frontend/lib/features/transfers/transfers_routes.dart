import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/widgets/error_view.dart';
import 'data/transfers_service.dart';
import 'models/transfer.dart';
import 'presentation/amount_page.dart';
import 'presentation/confirm_page.dart';
import 'presentation/destination_page.dart';
import 'presentation/interbank_form_page.dart';
import 'presentation/receipt_page.dart';
import 'presentation/tracking_page.dart';
import 'presentation/transfers_home_page.dart';

/// Rutas del feature `transfers` (E2-T11 / HU06).
///
/// Convencion F-T01 (`features/README.md`): el feature NUNCA toca
/// `lib/core/router/app_router.dart`; el orquestador importa esta lista y
/// hace `...transfersRoutes` (mas la fabrica `transfersServiceFactory`,
/// igual que `beneficiariesServiceFactory`).
///
/// - `/transfers` -> [TransfersHomePage] (hub propia/tercero/interbancaria).
/// - `/transfers/destination?kind=own|third` -> [DestinationPage] (origen +
///   destino propio o beneficiario de la agenda).
/// - `/transfers/amount` -> [AmountPage] (monto + motivo, `extra`:
///   [TransferFlowArgs]).
/// - `/transfers/confirm` -> [ConfirmPage] (resumen + POST idempotente,
///   `extra`: [TransferFlowArgs]; devuelve [CciOutcome] al formulario CCI).
/// - `/transfers/interbank` -> [InterbankFormPage] (formulario CCI + 4
///   estados de verificacion).
/// - `/transfers/tracking/:id` -> [TrackingPage] (polling `202`, `extra`:
///   [TrackingArgs]).
/// - `/transfers/receipt/:id` -> [ReceiptPage] (detalle + comprobante PDF).
///
/// Las paginas reciben el servicio desde `transfersServiceFactory`
/// (cableada por el orquestador con el `ApiClient` compartido). Si la
/// fabrica es `null`, muestran un error accionable en vez de romper.
final List<GoRoute> transfersRoutes = [
  GoRoute(
    path: '/transfers',
    builder: (context, state) => const TransfersHomePage(),
  ),
  GoRoute(
    path: '/transfers/destination',
    // H1: `?kind=interbank` NO degrada a propia: redirige al formulario
    // interbancario (`/transfers/interbank`); propia/tercero van al destino.
    redirect: (context, state) {
      if (state.uri.queryParameters['kind'] == 'interbank') {
        return '/transfers/interbank';
      }
      return null;
    },
    builder: (context, state) {
      final kind = TransferKind.fromQuery(state.uri.queryParameters['kind']);
      return DestinationPage(
        kind: kind,
        service: transfersServiceFactory?.call(),
      );
    },
  ),
  GoRoute(
    path: '/transfers/amount',
    builder: (context, state) {
      final extra = state.extra;
      if (extra is! TransferFlowArgs) {
        return const _MissingArgs(hint: 'Falta el borrador de transferencia.');
      }
      return AmountPage(args: extra);
    },
  ),
  GoRoute(
    path: '/transfers/confirm',
    builder: (context, state) {
      final extra = state.extra;
      if (extra is! TransferFlowArgs) {
        return const _MissingArgs(hint: 'Falta el borrador de transferencia.');
      }
      return ConfirmPage(
        args: extra,
        service: transfersServiceFactory?.call(),
      );
    },
  ),
  GoRoute(
    path: '/transfers/interbank',
    builder: (context, state) => InterbankFormPage(
      service: transfersServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/transfers/tracking/:id',
    builder: (context, state) {
      final id = state.pathParameters['id'] ?? '';
      final extra = state.extra;
      return TrackingPage(
        args:
            extra is TrackingArgs
                ? extra
                : TrackingArgs(transactionId: id),
        service: transfersServiceFactory?.call(),
      );
    },
  ),
  GoRoute(
    path: '/transfers/receipt/:id',
    builder: (context, state) {
      final id = state.pathParameters['id'] ?? '';
      return ReceiptPage(
        transactionId: id,
        service: transfersServiceFactory?.call(),
      );
    },
  ),
];

/// Error accionable cuando una ruta llega sin su `extra` (navegacion
/// directa o deep-link sin borrador): en vez de romper, explica y vuelve.
class _MissingArgs extends StatelessWidget {
  const _MissingArgs({required this.hint});

  final String hint;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Transferencias')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: ErrorView(
          message: '$hint Vuelve e inicia el flujo de nuevo.',
          onRetry: () => context.pop(),
          retryLabel: 'Volver',
        ),
      ),
    );
  }
}
