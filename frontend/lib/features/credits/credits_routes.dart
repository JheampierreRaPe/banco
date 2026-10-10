import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/widgets/error_view.dart';
import 'data/credits_service.dart';
import 'models/credit.dart';
import 'presentation/credits_simulator_page.dart';

/// Rutas del feature `credits` (E3-T04 / HU09).
///
/// Convencion F-T01 (`features/README.md`): el feature NUNCA toca
/// `lib/core/router/app_router.dart`; el orquestador importa esta lista y
/// hace `...creditsRoutes` (mas la fabrica `creditsServiceFactory`, igual
/// que `transfersServiceFactory`).
///
/// - `/credits` -> [CreditsSimulatorPage] (simulador: producto, monto,
///   plazo, desgravamen, cuota, cronograma, CTAs).
final List<GoRoute> creditsRoutes = [
  GoRoute(
    path: '/credits',
    builder: (context, state) => CreditsSimulatorPage(
      service: creditsServiceFactory?.call(),
    ),
  ),
];

/// Error accionable cuando una ruta llega sin su `extra` (navegacion
/// directa o deep-link sin borrador): en vez de romper, explica y vuelve.
class MissingCreditArgs extends StatelessWidget {
  const MissingCreditArgs({super.key, required this.hint});

  final String hint;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Créditos')),
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
