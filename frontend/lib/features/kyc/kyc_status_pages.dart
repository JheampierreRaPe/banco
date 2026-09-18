/// Pantallas de estado del flujo KYC (E1-T06, HU01 CA-04).
///
/// - [KycActionableErrorView]: error accionable (mensaje en español + motivo
///   del servidor + botón de reintento y, si aplica, derivación a revisión).
/// - [KycManualReviewPage]: derivación a revisión manual con folio.
///
/// Siguen la convención de `lib/features/README.md` (estados con
/// `LoadingView`/`EmptyView`/`ErrorView` de `lib/core/widgets/`).
library;

import 'package:flutter/material.dart';

import '../../../core/widgets/error_view.dart';
import 'kyc_error_handler.dart';

/// Error accionable: muestra el mensaje en español y, cuando el servidor
/// informó un motivo, lo muestra tal cual (`Motivo: ...`) en lugar de un
/// genérico (requisito E1-T06 "fallo persistente muestra motivo").
class KycActionableErrorView extends StatelessWidget {
  const KycActionableErrorView({
    super.key,
    required this.error,
    required this.onRetry,
    this.onManualReview,
    this.retryLabel = 'Reintentar',
  });

  /// Error ya clasificado por [KycErrorHandler].
  final KycErrorInfo error;

  /// Reintento (misma tarea / submit / challenge nuevo según
  /// [KycErrorInfo.action]; lo decide el llamador).
  final VoidCallback onRetry;

  /// Derivación explícita a revisión manual (solo se muestra cuando
  /// [KycErrorInfo.action] es `manualReview` y hay folio).
  final VoidCallback? onManualReview;

  final String retryLabel;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ErrorView(
              message: error.message,
              onRetry: onRetry,
              retryLabel: retryLabel,
            ),
            if (error.serverReason != null &&
                error.serverReason!.isNotEmpty) ...[
              const SizedBox(height: 8),
              Text(
                'Motivo: ${error.serverReason}',
                key: const Key('kycServerReason'),
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
            if (error.action == KycErrorAction.manualReview &&
                onManualReview != null) ...[
              const SizedBox(height: 12),
              OutlinedButton(
                key: const Key('goManualReviewButton'),
                onPressed: onManualReview,
                child: const Text('Ir a revisión manual'),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// Pantalla de derivación a revisión manual con folio de seguimiento.
class KycManualReviewPage extends StatelessWidget {
  const KycManualReviewPage({
    super.key,
    required this.folio,
    this.reason,
    this.onRestart,
  });

  /// Folio generado por [KycErrorHandler.newManualReviewFolio].
  final String folio;

  /// Motivo del servidor que originó la derivación (se muestra, no genérico).
  final String? reason;

  /// Reinicio documentado del flujo (el servidor no permitió conservar el
  /// progreso o el usuario decide empezar de cero).
  final VoidCallback? onRestart;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Revisión manual')),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(
                Icons.mark_email_read_outlined,
                size: 72,
              ),
              const SizedBox(height: 16),
              const Text(
                'Tu verificación pasó a revisión manual.',
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 8),
              const Text(
                'Un especialista la revisará. Guarda tu folio para '
                'seguimiento:',
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 12),
              SelectableText(
                folio,
                key: const Key('manualReviewFolio'),
                style: Theme.of(context).textTheme.titleLarge,
              ),
              if (reason != null && reason!.isNotEmpty) ...[
                const SizedBox(height: 8),
                Text(
                  'Motivo: $reason',
                  key: const Key('manualReviewReason'),
                  textAlign: TextAlign.center,
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ],
              if (onRestart != null) ...[
                const SizedBox(height: 24),
                FilledButton(
                  key: const Key('restartKycButton'),
                  onPressed: onRestart,
                  child: const Text('Empezar de nuevo'),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
