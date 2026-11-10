import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../data/merchant_qr_service.dart';
import '../models/merchant_qr.dart';

/// Detalle del estado de un cobro QR (`GET /merchants/qr/{id}`, HU14
/// CA-02/CA-04 parte, E4-T09; UI desde tokens `docs/20`).
///
/// Muestra el `status` efectivo del servidor (`ACTIVE`/`EXPIRED`/`PAID`,
/// on-read `EXPIRED` si vencio) con su monto y vigencia, y permite
/// refrescar para reflejar el abono. Cliente delgado: no decide el
/// estado, solo lo presenta.
///
/// Estados (docs/20 §7): carga, contenido, vacio (sin identificador) y
/// error + Reintentar. Mensajes en espanol.
class MerchantQrStatusPage extends StatefulWidget {
  const MerchantQrStatusPage({
    super.key,
    required this.chargeId,
    this.service,
  });

  final String chargeId;
  final MerchantQrServiceBase? service;

  @override
  State<MerchantQrStatusPage> createState() => _MerchantQrStatusPageState();
}

class _MerchantQrStatusPageState extends State<MerchantQrStatusPage> {
  Future<QrChargeStatus?>? _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<QrChargeStatus?> _load() {
    final service = widget.service;
    if (widget.chargeId.isEmpty) return Future.value(null);
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de cobros no configurado.',
        ),
      );
    }
    return service.getChargeStatus(widget.chargeId);
  }

  void _retry() {
    setState(() {
      _future = _load();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Estado del cobro')),
      body: FutureBuilder<QrChargeStatus?>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(
              child: CircularProgressIndicator(
                key: Key('merchant-qr-status-loading'),
              ),
            );
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            final message = error is ApiException
                ? _errorMessage(error)
                : 'Ocurrió un error al consultar el servidor. '
                    'Revisa tu conexión a internet o intenta de nuevo.';
            return Padding(
              padding: const EdgeInsets.all(16),
              child: ErrorView(
                message: message,
                retryLabel: 'Reintentar',
                onRetry: _retry,
              ),
            );
          }
          final status = snapshot.data;
          if (status == null) {
            return EmptyView(
              message: 'Sin cobro seleccionado.\n'
                  'Genera un cobro QR para ver su estado.',
              actionLabel: 'Generar cobro',
              onAction: () => context.go('/wallet/merchant-qr'),
            );
          }
          return RefreshIndicator(
            onRefresh: () async => _retry(),
            child: SingleChildScrollView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
              child: AppCard(
                key: const Key('merchant-qr-status-card'),
                padding: const EdgeInsets.all(20),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(
                          child: Text(
                            'Cobro',
                            style: AppTypography.titleMd
                                .copyWith(fontSize: 18),
                          ),
                        ),
                        _StatusBadge(status: status.status),
                      ],
                    ),
                    const SizedBox(height: 8),
                    Text(
                      formatMinor(status.amountMinor, status.currency),
                      key: const Key('merchant-qr-status-amount'),
                      style: AppTypography.displayLg.copyWith(
                        color: AppColors.primary,
                      ),
                    ),
                    const SizedBox(height: 12),
                    _Row(
                      label: 'Vigencia',
                      value: status.expiresAt == null ||
                              status.expiresAt!.isEmpty
                          ? 'Sin vencimiento (cobro estático)'
                          : formatIsoLocal(status.expiresAt),
                      valueKey: const Key('merchant-qr-status-expires'),
                    ),
                    if (status.paidAt != null &&
                        status.paidAt!.isNotEmpty) ...[
                      const SizedBox(height: 8),
                      _Row(
                        label: 'Fecha de pago',
                        value: formatIsoLocal(status.paidAt),
                        valueKey: const Key('merchant-qr-status-paid'),
                      ),
                    ],
                    const SizedBox(height: 8),
                    Text(
                      'Referencia: ${status.chargeId}',
                      style: AppTypography.bodyMd.copyWith(
                        fontSize: 12,
                        color: AppColors.secondaryText,
                      ),
                    ),
                    const SizedBox(height: 16),
                    SizedBox(
                      width: double.infinity,
                      height: 48,
                      child: FilledButton.icon(
                        key: const Key('merchant-qr-status-refresh'),
                        onPressed: _retry,
                        icon: const Icon(Icons.refresh, size: 20),
                        label: const Text('Actualizar estado'),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          );
        },
      ),
    );
  }
}

/// Badge del estado efectivo del servidor (presentacion; no decide).
class _StatusBadge extends StatelessWidget {
  const _StatusBadge({required this.status});

  final String status;

  @override
  Widget build(BuildContext context) {
    switch (status) {
      case 'PAID':
        return const AppStatusChip.success(label: 'Pagado');
      case 'EXPIRED':
        return const AppStatusChip.error(label: 'Vencido');
      case 'ACTIVE':
        return const AppStatusChip.warning(label: 'Pendiente');
      default:
        return AppStatusChip.info(
          label: status.isEmpty ? 'Sin estado' : status,
        );
    }
  }
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.value, this.valueKey});

  final String label;
  final String value;
  final Key? valueKey;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 88,
          child: Text(
            label,
            style: AppTypography.labelMd.copyWith(
              fontSize: 12,
              color: AppColors.secondaryText,
            ),
          ),
        ),
        Expanded(
          child: Text(
            value,
            key: valueKey,
            style: AppTypography.bodyMd,
          ),
        ),
      ],
    );
  }
}

/// Mensaje accionable por codigo (sin tocar `core`, fuera de frontera).
String _errorMessage(ApiException error) {
  switch (error.code) {
    case 'NOT_FOUND':
      return 'No encontramos ese cobro. Verifica el identificador.';
    case 'NOT_AUTHORIZED':
      return 'Tu cuenta no tiene rol de comercio. Contacta a tu banco.';
    case 'VALIDATION_ERROR':
      return 'Revisa los datos ingresados e inténtalo de nuevo.';
    default:
      if (error.isNetworkError ||
          error.code == 'UNKNOWN' ||
          error.code == 'SERVER_ERROR') {
        return 'Ocurrió un error al consultar el servidor. '
            'Revisa tu conexión a internet o intenta de nuevo.';
      }
      return error.message;
  }
}
