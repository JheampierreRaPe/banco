import 'package:flutter/material.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../models/services.dart';

/// Comprobante estructurado de pago/recarga (E4-T19, HU16 CA-04).
///
/// Muestra el `receipt` tal cual del backend (numero de transaccion
/// oficial `receipt_no` `OP-…` + referencia enmascarada); la app no genera
/// numeros oficiales ni recalcula montos.
class ServiceReceiptCard extends StatelessWidget {
  const ServiceReceiptCard({
    super.key,
    required this.result,
    this.title = 'Comprobante',
    this.onDone,
    this.doneLabel = 'Volver a servicios',
  });

  final ServiceOperationResult result;
  final String title;
  final VoidCallback? onDone;
  final String doneLabel;

  @override
  Widget build(BuildContext context) {
    final receipt = result.receipt;
    return AppCard(
      key: const Key('services-receipt'),
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Container(
                width: 44,
                height: 44,
                alignment: Alignment.center,
                decoration: const BoxDecoration(
                  shape: BoxShape.circle,
                  color: AppColors.successContainer,
                ),
                child: const Icon(
                  Icons.receipt_long_outlined,
                  color: AppColors.onSuccessContainer,
                  size: 24,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Text(title, style: AppTypography.titleMd),
              ),
              const AppStatusChip.success(label: 'Pagado'),
            ],
          ),
          const SizedBox(height: 12),
          Text(
            formatServiceMinor(result.amountMinor, result.currency),
            key: const Key('services-receipt-amount'),
            textAlign: TextAlign.center,
            style: AppTypography.displayLg.copyWith(
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: 12),
          _row(
            'N.º de transacción',
            receipt == null || receipt.receiptNo.isEmpty
                ? result.transactionId
                : receipt.receiptNo,
            key: 'services-receipt-no',
          ),
          if ((receipt?.concept ?? '').isNotEmpty)
            _row('Concepto', receipt!.concept),
          if ((receipt?.maskedRef ?? '').isNotEmpty)
            _row('Referencia', receipt!.maskedRef!),
          if ((receipt?.paidAt ?? '').isNotEmpty)
            _row('Fecha', receipt!.paidAt!),
          _row('Moneda', result.currency),
          if (result.replayed)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                'Operación ya registrada antes: se muestra el comprobante '
                'original sin duplicar el cobro.',
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 12,
                  color: AppColors.secondaryText,
                ),
              ),
            ),
          if (onDone != null) ...[
            const SizedBox(height: 16),
            SizedBox(
              width: double.infinity,
              height: 52,
              child: FilledButton(
                key: const Key('services-receipt-done'),
                onPressed: onDone,
                child: Text(doneLabel),
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _row(String label, String value, {String? key}) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Text(
              label,
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            flex: 2,
            child: Text(
              value,
              key: key == null ? null : Key(key),
              textAlign: TextAlign.right,
              style: AppTypography.labelMd.copyWith(fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }
}

/// Tarjeta de error de servicios/recargas: mensaje accionable en español
/// + Reintentar (mismo intento, misma `Idempotency-Key`) cuando aplica.
///
/// Nunca muestra `supply_ref`/`phone` completos (sin PII).
class ServiceErrorCard extends StatelessWidget {
  const ServiceErrorCard({
    super.key,
    required this.failure,
    this.onRetry,
    this.onBack,
  });

  final ServiceFailure failure;
  final VoidCallback? onRetry;
  final VoidCallback? onBack;

  @override
  Widget build(BuildContext context) {
    return Container(
      key: const Key('services-error'),
      width: double.infinity,
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: AppColors.errorContainer,
        borderRadius: BorderRadius.circular(16),
      ),
      child: Column(
        children: [
          const Icon(
            Icons.error_outline,
            size: 48,
            color: AppColors.onErrorContainer,
          ),
          const SizedBox(height: 8),
          const AppStatusChip.error(label: 'Operación no realizada'),
          const SizedBox(height: 8),
          Text(
            failure.message,
            key: const Key('services-error-message'),
            textAlign: TextAlign.center,
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onErrorContainer,
            ),
          ),
          const SizedBox(height: 12),
          if (onRetry != null && failure.retry)
            SizedBox(
              width: double.infinity,
              height: 48,
              child: FilledButton(
                key: const Key('services-retry'),
                onPressed: onRetry,
                child: const Text('Reintentar'),
              ),
            ),
          if (onBack != null) ...[
            const SizedBox(height: 8),
            SizedBox(
              width: double.infinity,
              height: 48,
              child: OutlinedButton(
                key: const Key('services-back'),
                onPressed: onBack,
                child: const Text('Volver'),
              ),
            ),
          ],
        ],
      ),
    );
  }
}
