import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../kyc_dependencies.dart';
import '../kyc_flow_controller.dart';

/// Paso 3 del KYC: resultado y motivo de fallo si aplica.
class KycResultPage extends StatelessWidget {
  const KycResultPage({super.key, this.controller});

  /// Controlador inyectable (tests). Por defecto, el compartido de
  /// [KycDependencies].
  final KycFlowController? controller;

  KycFlowController _resolve(BuildContext context) =>
      controller ?? KycDependencies.controller;

  @override
  Widget build(BuildContext context) {
    final c = _resolve(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Resultado de verificacion')),
      body: ListenableBuilder(
        listenable: c,
        builder: (context, _) {
          if (c.busy) {
            return const LoadingView(message: 'Enviando verificacion...');
          }
          final result = c.result;
          if (result == null) {
            if (c.errorMessage != null) {
              return ErrorView(
                message: c.errorMessage!,
                onRetry: () => _retry(context, c),
              );
            }
            return EmptyView(
              message: 'Aun no hay un resultado de verificacion.',
              actionLabel: 'Volver a las tareas',
              onAction: () => context.go('/kyc/task'),
            );
          }
          if (result.overallResult) {
            return Center(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(
                      Icons.check_circle_outline,
                      size: 72,
                      color: Colors.green,
                    ),
                    const SizedBox(height: 16),
                    const Text('Verificacion exitosa'),
                    const SizedBox(height: 24),
                    FilledButton(
                      onPressed: () => context.go('/home'),
                      child: const Text('Ir al inicio'),
                    ),
                  ],
                ),
              ),
            );
          }
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(
                    Icons.cancel_outlined,
                    size: 72,
                    color: Colors.red,
                  ),
                  const SizedBox(height: 16),
                  const Text('No se pudo verificar tu identidad.'),
                  const SizedBox(height: 8),
                  Text('Motivo: ${result.detailCode}'),
                  const SizedBox(height: 24),
                  FilledButton(
                    onPressed: () => _retry(context, c),
                    child: const Text('Reintentar verificacion'),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }

  void _retry(BuildContext context, KycFlowController c) {
    c.reset();
    context.go('/kyc');
  }
}
