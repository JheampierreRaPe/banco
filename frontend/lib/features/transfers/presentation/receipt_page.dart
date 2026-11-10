import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/transfers_service.dart';
import '../models/transfer.dart';

/// Comprobante de transferencia (HU06 CA-04, E2-T10).
///
/// Carga el detalle (`GET /transactions/{id}`) + el PDF
/// (`GET /transactions/{id}/receipt`, 4 estados). El número de operación
/// (`OP-...`) sale del nombre del comprobante (derivado determinista del
/// servidor). Para `REJECTED`/`FAILED` muestra el motivo en vez del
/// comprobante. La descarga solo informa nombre/tamaño: el guardado nativo
/// queda pendiente (F-futuro, patrón E2-T05).
class ReceiptPage extends StatefulWidget {
  const ReceiptPage({super.key, required this.transactionId, this.service});

  final String transactionId;
  final TransfersServiceBase? service;

  @override
  State<ReceiptPage> createState() => _ReceiptPageState();
}

class _ReceiptPageState extends State<ReceiptPage> {
  Future<_ReceiptData>? _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<_ReceiptData> _load() async {
    final service = widget.service;
    if (service == null) {
      throw ApiException(
        code: 'UNKNOWN',
        message: 'Servicio de transferencias no configurado.',
      );
    }
    final detail = await service.getTransaction(widget.transactionId);
    ReceiptResult? receipt;
    if (detail.isSuccess) {
      receipt = await service.getReceipt(widget.transactionId);
    }
    return _ReceiptData(detail: detail, receipt: receipt);
  }

  void _retry() {
    setState(() => _future = _load());
  }

  Future<void> _download(ReceiptResult receipt) async {
    final size = receipt.bytes.length;
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          'Comprobante ${receipt.suggestedFilename} '
          '($size bytes). El guardado nativo está pendiente.',
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    // M1: el bloqueo de retroceso (`PopScope`) aplica SOLO al exito (estado
    // terminal con "Volver al inicio"). En carga/error/fallo el retroceso
    // queda libre y el error ofrece "Volver" (canPop ? pop : go('/home')):
    // nadie queda atrapado aunque falle el fetch.
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        title: const Text('Comprobante'),
        automaticallyImplyLeading: false,
      ),
      body: FutureBuilder<_ReceiptData>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const LoadingView(
              key: Key('receipt-loading'),
              message: 'Cargando comprobante...',
            );
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            return Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  ErrorView(
                    message:
                        error is ApiException
                            ? transferUserMessage(error)
                            : 'Ocurrió un error al consultar el servidor.',
                    onRetry: _retry,
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    width: double.infinity,
                    child: OutlinedButton(
                      key: const Key('receipt-back-cta'),
                      onPressed: () => context.canPop()
                          ? context.pop()
                          : context.go('/home'),
                      style: OutlinedButton.styleFrom(
                        minimumSize: const Size(88, 52),
                      ),
                      child: const Text('Volver'),
                    ),
                  ),
                ],
              ),
            );
          }
          final data = snapshot.data!;
          final detail = data.detail;
          if (!detail.isSuccess) return _failureView(detail);
          final receipt = data.receipt;
          return PopScope(
            canPop: false,
            child: ListView(
              key: const Key('receipt-success'),
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
              children: [
                AppCard(
                  padding: const EdgeInsets.all(20),
                  child: Column(
                    children: [
                      Container(
                        width: 64,
                        height: 64,
                        alignment: Alignment.center,
                        decoration: const BoxDecoration(
                          color: AppColors.successContainer,
                          shape: BoxShape.circle,
                        ),
                        child: const Icon(
                          Icons.check,
                          size: 32,
                          color: AppColors.success,
                        ),
                      ),
                      const SizedBox(height: 12),
                      Text(
                        'Transferencia exitosa',
                        style: AppTypography.titleMd.copyWith(
                          color: AppColors.primary,
                        ),
                      ),
                      const SizedBox(height: 8),
                      Text(
                        formatMoneyMinor(
                          detail.amountMinor,
                          detail.currency,
                        ),
                        style: AppTypography.displayLg.copyWith(
                          fontSize: 32,
                          color: AppColors.onSurface,
                        ),
                      ),
                      const SizedBox(height: 8),
                      AppStatusChip.info(
                        label: _statusLabel(detail.status),
                      ),
                      if (receipt != null &&
                          receipt.operationNumber.isNotEmpty) ...[
                        const SizedBox(height: 8),
                        Text(
                          key: const Key('receipt-op-number'),
                          'Nº ${receipt.operationNumber}',
                          style: AppTypography.labelMd.copyWith(
                            color: AppColors.onSurfaceVariant,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                AppCard(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 20,
                    vertical: 8,
                  ),
                  child: Column(
                    children: [
                      _row('Tipo', _typeLabel(detail.type)),
                      _row('Estado', _statusLabel(detail.status)),
                      _row(
                        'Monto',
                        formatMoneyMinor(detail.amountMinor, detail.currency),
                      ),
                      _row(
                        'Comisión',
                        formatMoneyMinor(detail.feeMinor, detail.currency),
                      ),
                      _row(
                        'Total debitado',
                        formatMoneyMinor(detail.totalMinor, detail.currency),
                        bold: true,
                      ),
                      if ((detail.externalRef ?? '').isNotEmpty)
                        _row('Referencia', detail.externalRef!),
                      if (detail.holderMasked.isNotEmpty)
                        _row('Titular destino', detail.holderMasked),
                    ],
                  ),
                ),
                const SizedBox(height: 16),
                if (receipt != null)
                  SizedBox(
                    width: double.infinity,
                    child: OutlinedButton.icon(
                      key: const Key('receipt-download'),
                      onPressed: () => _download(receipt),
                      icon: const Icon(Icons.download_outlined, size: 18),
                      label: const Text('Descargar comprobante (PDF)'),
                      style: OutlinedButton.styleFrom(
                        minimumSize: const Size(88, 52),
                      ),
                    ),
                  ),
                const SizedBox(height: 8),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton(
                    key: const Key('receipt-home-cta'),
                    onPressed: () => context.go('/home'),
                    style: FilledButton.styleFrom(
                      minimumSize: const Size(88, 52),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(AppRadii.md),
                      ),
                    ),
                    child: const Text('Volver al inicio'),
                  ),
                ),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _failureView(TransactionDetail detail) {
    // Rechazo terminal (no es error de fetch): mantiene el bloqueo de
    // retroceso como estado terminal deliberado; la salida es
    // "Volver al inicio" (M1 conforme: tiene Volver, nadie queda atrapado).
    return PopScope(
      canPop: false,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          AppCard(
            padding: const EdgeInsets.all(20),
            child: Column(
              children: [
                Container(
                  width: 64,
                  height: 64,
                  alignment: Alignment.center,
                  decoration: const BoxDecoration(
                    color: AppColors.errorContainer,
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(
                    Icons.close,
                    size: 32,
                    color: AppColors.error,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  'La transferencia no prosperó',
                  textAlign: TextAlign.center,
                  style: AppTypography.titleMd.copyWith(
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  transferUserMessage(
                    ApiException(
                      code: detail.failureCode ?? 'UNKNOWN',
                      message: '',
                    ),
                  ),
                  textAlign: TextAlign.center,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.onSurfaceVariant,
                  ),
                ),
                const SizedBox(height: 8),
                AppStatusChip.error(label: _statusLabel(detail.status)),
              ],
            ),
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              key: const Key('receipt-home-cta'),
              onPressed: () => context.go('/home'),
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Volver al inicio'),
            ),
          ),
        ],
      ),
    );
  }

  Widget _row(String k, String v, {bool bold = false}) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        SizedBox(
          height: 48,
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                k,
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.onSurfaceVariant,
                ),
              ),
              Flexible(
                child: Text(
                  v,
                  textAlign: TextAlign.end,
                  style: AppTypography.bodyLg.copyWith(
                    fontSize: 15,
                    fontWeight:
                        bold ? FontWeight.bold : FontWeight.w500,
                    color: AppColors.onSurface,
                  ),
                ),
              ),
            ],
          ),
        ),
        const Divider(height: 1, color: AppColors.divider),
      ],
    );
  }

  String _typeLabel(String type) {
    switch (type.toUpperCase()) {
      case 'OWN_TRANSFER':
        return 'Entre mis cuentas';
      case 'THIRD_PARTY_TRANSFER':
        return 'A un tercero';
      case 'INTERBANK_TRANSFER':
        return 'Interbancaria CCI';
      default:
        return type;
    }
  }

  String _statusLabel(String status) {
    switch (status.toUpperCase()) {
      case 'SETTLED':
        return 'COMPLETADA';
      case 'POSTED':
        return 'PUBLICADA';
      case 'CONCILIATED':
        return 'CONCILIADA';
      case 'REJECTED':
        return 'RECHAZADA';
      case 'FAILED':
        return 'FALLIDA';
      default:
        return status.toUpperCase();
    }
  }
}

class _ReceiptData {
  const _ReceiptData({required this.detail, this.receipt});

  final TransactionDetail detail;
  final ReceiptResult? receipt;
}
