import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/error_view.dart';
import '../data/transfers_service.dart';
import '../models/transfer.dart';
import 'tracking_page.dart';
import 'widgets/cci_status_banner.dart';

/// Confirmacion de transferencia (mockup `Confirmación de Transferencia`).
///
/// Muestra el resumen y ejecuta el POST con la `Idempotency-Key` del
/// borrador; el reintento reutiliza la MISMA clave. El total exhibido viene
/// del backend (`amount + fee` de la respuesta; el cliente no calcula).
///
/// Destinos tras el intento:
/// - propia/tercero `SETTLED` -> `/transfers/receipt/:id`.
/// - interbancaria aceptada (`202`) -> `/transfers/tracking/:id`.
/// - `REJECTED` por fondos -> tarjeta de saldo insuficiente (mockup
///   `Error Pago 1`; sin debitar, reintentable con otra cuenta).
/// - `HOLDER_NOT_FOUND` / `HOLDER_LOOKUP_UNAVAILABLE` (interbancaria) ->
///   banner CCI + [Corregir / Reintentar] (misma clave).
class ConfirmPage extends StatefulWidget {
  const ConfirmPage({super.key, required this.args, this.service});

  final TransferFlowArgs args;
  final TransfersServiceBase? service;

  @override
  State<ConfirmPage> createState() => _ConfirmPageState();
}

class _ConfirmPageState extends State<ConfirmPage> {
  bool _submitting = false;
  ApiException? _error;
  TransferResult? _rejected;
  CciState _cciState = CciState.idle;
  final String _holderMasked = '';

  TransferDraft get _draft => widget.args.draft;

  Future<void> _submit() async {
    final service = widget.service;
    if (service == null) {
      setState(() {
        _error = ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de transferencias no configurado.',
        );
      });
      return;
    }
    setState(() {
      _submitting = true;
      _error = null;
      _rejected = null;
      _cciState = CciState.idle;
    });
    try {
      final result = await _post(service);
      if (!mounted) return;
      if (result.isInsufficientFunds) {
        setState(() {
          _submitting = false;
          _rejected = result;
        });
        return;
      }
      if (result.isRejected) {
        setState(() {
          _submitting = false;
          _error = ApiException(
            code: result.failureCode ?? 'UNKNOWN',
            message:
                'La transferencia fue rechazada (${result.failureCode ?? 'sin motivo'}). '
                'No se debitó nada.',
          );
        });
        return;
      }
      if (_draft.kind == TransferKind.interbank) {
        context.pushReplacement(
          '/transfers/tracking/${result.transactionId}',
          extra: TrackingArgs(
            transactionId: result.transactionId,
            holderMasked: result.holderMasked,
            amountMinor: result.amountMinor,
            currency: result.currency,
          ),
        );
      } else {
        context.pushReplacement(
          '/transfers/receipt/${result.transactionId}',
        );
      }
      if (mounted) setState(() => _submitting = false);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _submitting = false;
        if (e.code == 'HOLDER_NOT_FOUND') {
          _cciState = CciState.notFound;
        } else if (e.code == 'HOLDER_LOOKUP_UNAVAILABLE' ||
            e.code == 'HOLDER_LOOKUP_TIMEOUT' ||
            e.code == 'TIMEOUT_ERROR') {
          _cciState = CciState.unavailable;
        } else {
          _error = e;
        }
      });
    }
  }

  Future<TransferResult> _post(TransfersServiceBase service) {
    final d = _draft;
    final motiveMeta =
        d.motive.isEmpty ? null : <String, dynamic>{'motive': d.motive};
    switch (d.kind) {
      case TransferKind.own:
        return service.createOwnTransfer(
          sourceAccountId: d.sourceAccountId,
          targetAccountId: d.targetAccountId ?? '',
          amountMinor: d.amountMinor,
          currency: d.currency,
          metadata: motiveMeta,
          idempotencyKey: d.idempotencyKey,
        );
      case TransferKind.thirdParty:
        return service.createThirdPartyTransfer(
          sourceAccountId: d.sourceAccountId,
          targetAccountId: d.targetAccountId ?? '',
          amountMinor: d.amountMinor,
          currency: d.currency,
          beneficiaryId: d.beneficiaryId,
          metadata: motiveMeta,
          idempotencyKey: d.idempotencyKey,
        );
      case TransferKind.interbank:
        return service.createInterbankTransfer(
          sourceAccountId: d.sourceAccountId,
          amountMinor: d.amountMinor,
          cci: d.cci,
          currency: d.currency,
          holderName: d.holderName.isEmpty ? null : d.holderName,
          bankCode: d.bankCode.isEmpty ? null : d.bankCode,
          metadata: motiveMeta,
          idempotencyKey: d.idempotencyKey,
        );
    }
  }

  void _correctCci() {
    if (_draft.kind == TransferKind.interbank) {
      context.pop(CciOutcome(_cciState));
    } else {
      context.pop();
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_rejected != null) return _insufficientView(_rejected!);
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Confirma tu transferencia')),
      body: Column(
        children: [
          Expanded(
            child: ListView(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
              children: [
                if (_cciState == CciState.notFound ||
                    _cciState == CciState.unavailable) ...[
                  CciStatusBanner(
                    state: _cciState,
                    holderMasked: _holderMasked,
                    onRetry:
                        _cciState == CciState.unavailable ? _submit : null,
                    retryKey: const Key('confirm-retry'),
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    width: double.infinity,
                    child: OutlinedButton(
                      onPressed: _correctCci,
                      child: const Text('Corregir datos'),
                    ),
                  ),
                  const SizedBox(height: 16),
                ],
                if (_error != null) ...[
                  ErrorView(
                    key: const Key('confirm-error'),
                    message: transferUserMessage(_error!),
                    onRetry: _submit,
                    retryLabel: 'Reintentar',
                  ),
                  const SizedBox(height: 16),
                ],
                _Hero(draft: _draft),
                const SizedBox(height: 16),
                _SummaryCard(args: widget.args),
                const SizedBox(height: 16),
                Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: AppColors.warningContainer,
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                  child: Row(
                    children: [
                      const Icon(
                        Icons.warning_amber,
                        size: 20,
                        color: AppColors.onWarningContainer,
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Text(
                          'Revisa los datos. Las transferencias no se pueden revertir.',
                          style: AppTypography.bodyMd.copyWith(
                            fontSize: 13,
                            color: AppColors.onSurface,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  SizedBox(
                    width: double.infinity,
                    child: FilledButton(
                      key: const Key('confirm-submit'),
                      onPressed: _submitting ? null : _submit,
                      style: FilledButton.styleFrom(
                        minimumSize: const Size(88, 52),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(AppRadii.md),
                        ),
                      ),
                      child: _submitting
                          ? const SizedBox.square(
                              key: Key('confirm-loading'),
                              dimension: 20,
                              child: CircularProgressIndicator(
                                strokeWidth: 2.5,
                                color: AppColors.onPrimary,
                              ),
                            )
                          : const Text('Confirmar transferencia'),
                    ),
                  ),
                  TextButton(
                    key: const Key('confirm-cancel'),
                    onPressed: _submitting ? null : () => context.pop(),
                    child: const Text('Cancelar'),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _insufficientView(TransferResult result) {
    final draft = _draft;
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.close),
          onPressed: () => context.pop(),
        ),
        title: const Text('Estado del pago'),
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          AppCard(
            key: const Key('insufficient-banner'),
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
                    Icons.account_balance_wallet_outlined,
                    size: 32,
                    color: AppColors.error,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  'No tienes saldo suficiente',
                  textAlign: TextAlign.center,
                  style: AppTypography.titleMd.copyWith(
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  'El monto más la comisión '
                  '(${formatMoneyMinor(result.totalMinor, result.currency)}) '
                  'supera tu saldo disponible.',
                  textAlign: TextAlign.center,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.onSurfaceVariant,
                  ),
                ),
                const SizedBox(height: 12),
                Container(
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: AppColors.surfaceContainerLow,
                    borderRadius: BorderRadius.circular(AppRadii.def),
                  ),
                  child: Row(
                    children: [
                      const Icon(
                        Icons.verified_outlined,
                        size: 18,
                        color: AppColors.primary,
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          'Tu dinero está seguro. No se debitó nada.',
                          style: AppTypography.bodyMd.copyWith(
                            fontSize: 12,
                            color: AppColors.onSurfaceVariant,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 12),
          AppCard(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'TRANSFERENCIA PREVISTA',
                  style: AppTypography.labelMd.copyWith(
                    fontSize: 12,
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(height: 8),
                _kv(
                  'Destinatario',
                  draft.beneficiaryAlias.isEmpty
                      ? 'Destinatario'
                      : draft.beneficiaryAlias,
                ),
                _kv('Monto', formatMoneyMinor(draft.amountMinor, draft.currency)),
                _kv(
                  'Total intentado',
                  formatMoneyMinor(result.totalMinor, result.currency),
                  bold: true,
                ),
              ],
            ),
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              onPressed: () => context.pop(),
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Elegir otra cuenta de origen'),
            ),
          ),
          const SizedBox(height: 8),
          Center(
            child: Text(
              'Transacción protegida CuyCash',
              style: AppTypography.bodyMd.copyWith(
                fontSize: 11,
                color: AppColors.onSurfaceVariant,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _kv(String k, String v, {bool bold = false}) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(
            k,
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              color: AppColors.onSurfaceVariant,
            ),
          ),
          Text(
            v,
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              fontWeight: bold ? FontWeight.bold : FontWeight.w600,
              color: AppColors.onSurface,
            ),
          ),
        ],
      ),
    );
  }
}

class _Hero extends StatelessWidget {
  const _Hero({required this.draft});

  final TransferDraft draft;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Text(
          'Vas a enviar',
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.onSurfaceVariant,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          formatMoneyMinor(draft.amountMinor, draft.currency),
          style: AppTypography.displayLg.copyWith(
            fontSize: 40,
            color: AppColors.onSurface,
          ),
        ),
        const SizedBox(height: 8),
        if (draft.kind == TransferKind.own)
          const AppStatusChip.success(label: 'SIN COMISIÓN')
        else
          const AppStatusChip.info(
            label: 'COMISIÓN LA CALCULA EL BANCO',
          ),
      ],
    );
  }
}

class _SummaryCard extends StatelessWidget {
  const _SummaryCard({required this.args});

  final TransferFlowArgs args;

  @override
  Widget build(BuildContext context) {
    final draft = args.draft;
    final isInterbank = draft.kind == TransferKind.interbank;
    final destAccount =
        isInterbank ? maskCciShort(draft.cci) : draft.destinationMasked;
    final bank =
        isInterbank
            ? (draft.bankCode.isEmpty
                ? 'Otro banco (CCE)'
                : draft.bankCode)
            : 'CuyCash';
    return AppCard(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
      child: Column(
        children: [
          _row('Destinatario',
              draft.beneficiaryAlias.isEmpty ? '—' : draft.beneficiaryAlias),
          _row('Cuenta destino',
              destAccount.isEmpty ? '—' : destAccount),
          _row('Banco', bank),
          _row('Desde',
              '${args.source.accountNumberMasked} · ${args.source.currency}'),
          _row('Motivo', draft.motive.isEmpty ? '—' : draft.motive),
          _row(
            'Comisión',
            draft.kind == TransferKind.own
                ? formatMoneyMinor(0, draft.currency)
                : 'Calculada por el banco',
          ),
          _row('Fecha', 'Hoy'),
          Container(
            margin: const EdgeInsets.symmetric(vertical: 8),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerLow,
              borderRadius: BorderRadius.circular(AppRadii.def),
            ),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  'Total a debitar',
                  style: AppTypography.bodyLg.copyWith(
                    fontWeight: FontWeight.w600,
                    color: AppColors.onSurface,
                  ),
                ),
                Text(
                  formatMoneyMinor(draft.amountMinor, draft.currency),
                  style: AppTypography.titleMd.copyWith(
                    color: AppColors.onSurface,
                  ),
                ),
              ],
            ),
          ),
          if (draft.kind != TransferKind.own)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(
                'Más la comisión que calcule el banco (la verás en el comprobante).',
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 12,
                  color: AppColors.outline,
                ),
              ),
            ),
        ],
      ),
    );
  }

  Widget _row(String k, String v) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        SizedBox(
          height: 52,
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
                    fontWeight: FontWeight.w500,
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
}
