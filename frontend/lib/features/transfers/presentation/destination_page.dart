import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../../accounts/models/account.dart';
import '../../beneficiaries/models/beneficiary.dart';
import '../data/transfers_service.dart';
import '../models/transfer.dart';
import 'widgets/account_option_list.dart';

/// Seleccion de origen y destino (mockups `Transferencia Entre Mis Cuentas`
/// y `Enviar - Selección Beneficiario`, `pantallas.fig`).
///
/// - Origen: cuentas propias (`GET /accounts`, 4 estados).
/// - Destino propio: otra cuenta propia (se excluye el origen).
/// - Destino tercero: beneficiario de la agenda (`GET /beneficiaries`,
///   datos enmascarados; 4 estados).
///
/// Al continuar navega a `/transfers/amount` con [TransferFlowArgs] (la
/// `Idempotency-Key` del intento se genera aqui una sola vez).
class DestinationPage extends StatefulWidget {
  const DestinationPage({super.key, required this.kind, this.service});

  final TransferKind kind;
  final TransfersServiceBase? service;

  @override
  State<DestinationPage> createState() => _DestinationPageState();
}

class _DestinationPageState extends State<DestinationPage> {
  Future<_DestinationData>? _future;
  String? _sourceId;
  String? _targetId;
  String? _beneficiaryId;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<_DestinationData> _load() async {
    final service = widget.service;
    if (service == null) {
      throw ApiException(
        code: 'UNKNOWN',
        message: 'Servicio de transferencias no configurado.',
      );
    }
    final accounts = await service.accounts();
    final beneficiaries =
        widget.kind == TransferKind.thirdParty
            ? await service.beneficiaries()
            : const <Beneficiary>[];
    return _DestinationData(accounts: accounts, beneficiaries: beneficiaries);
  }

  void _retry() {
    setState(() {
      _sourceId = null;
      _targetId = null;
      _beneficiaryId = null;
      _future = _load();
    });
  }

  bool get _canContinue {
    if (_sourceId == null) return false;
    if (widget.kind == TransferKind.own) {
      return _targetId != null && _targetId != _sourceId;
    }
    return _beneficiaryId != null;
  }

  void _continue(_DestinationData data) {
    final source = data.accounts.firstWhere((a) => a.id == _sourceId);
    if (widget.kind == TransferKind.own) {
      final target = data.accounts.firstWhere((a) => a.id == _targetId);
      final draft = TransferDraft(
        kind: TransferKind.own,
        sourceAccountId: source.id,
        targetAccountId: target.id,
        destinationMasked: target.accountNumberMasked,
        beneficiaryAlias:
            'Cuenta CuyCash ${accountTypeLabel(target.type)}',
        currency: source.currency,
        amountMinor: 0,
      );
      context.push(
        '/transfers/amount',
        extra: TransferFlowArgs(draft: draft, source: source, target: target),
      );
      return;
    }
    final beneficiary = data.beneficiaries.firstWhere(
      (b) => b.id == _beneficiaryId,
    );
    // SUPUESTO E2-T11 (riesgo documentado): el backend resuelve la cuenta
    // destino del beneficiario; el `beneficiary_id` viaja ademas en
    // `metadata` para la exencion por tope (E2-T14). Si el backend responde
    // `NOT_FOUND`, la confirmacion muestra "cuenta destino invalida".
    final draft = TransferDraft(
      kind: TransferKind.thirdParty,
      sourceAccountId: source.id,
      targetAccountId: beneficiary.id,
      beneficiaryId: beneficiary.id,
      beneficiaryAlias: beneficiary.alias,
      destinationMasked:
          '${beneficiary.accountMasked} · ${currencyAccountLabel(beneficiary.currency)}',
      currency: beneficiary.currency,
      amountMinor: 0,
    );
    context.push(
      '/transfers/amount',
      extra: TransferFlowArgs(draft: draft, source: source),
    );
  }

  @override
  Widget build(BuildContext context) {
    final isOwn = widget.kind == TransferKind.own;
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        title: Text(isOwn ? 'Entre mis cuentas' : 'Elegir destinatario'),
      ),
      body: FutureBuilder<_DestinationData>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const LoadingView(message: 'Cargando cuentas...');
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            final message =
                error is ApiException
                    ? transferUserMessage(error)
                    : 'Ocurrió un error al consultar el servidor. '
                        'Revisa tu conexión a internet o intenta de nuevo.';
            return Padding(
              padding: const EdgeInsets.all(16),
              child: ErrorView(message: message, onRetry: _retry),
            );
          }
          final data = snapshot.data!;
          if (data.accounts.isEmpty) {
            return Padding(
              padding: const EdgeInsets.all(16),
              child: EmptyView(
                message: 'Aún no tienes cuentas para transferir.\n'
                    'Abre tu primera cuenta para empezar.',
                actionLabel: 'Ir al inicio',
                onAction: () => context.pop(),
              ),
            );
          }
          _sourceId ??= data.accounts.first.id;
          final source = data.accounts.firstWhere(
            (a) => a.id == _sourceId,
            orElse: () => data.accounts.first,
          );
          _sourceId = source.id;
          return Column(
            children: [
              Expanded(
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
                  children: [
                    _SectionLabel(
                      isOwn ? 'DESDE MI CUENTA' : 'CUENTA ORIGEN',
                    ),
                    const SizedBox(height: 8),
                    AccountOptionList(
                      accounts: data.accounts,
                      selectedId: _sourceId,
                      keyPrefix: 'transfer-origin',
                      onSelected:
                          (a) => setState(() {
                            _sourceId = a.id;
                            if (_targetId == a.id) _targetId = null;
                          }),
                    ),
                    const SizedBox(height: 16),
                    _SectionLabel(
                      isOwn ? 'A MI CUENTA' : 'BENEFICIARIO DESTINO',
                    ),
                    const SizedBox(height: 8),
                    if (isOwn)
                      AccountOptionList(
                        accounts: data.accounts,
                        selectedId: _targetId,
                        excludeId: _sourceId,
                        keyPrefix: 'transfer-dest',
                        onSelected:
                            (a) => setState(() => _targetId = a.id),
                      )
                    else
                      _BeneficiaryOptions(
                        beneficiaries: data.beneficiaries,
                        selectedId: _beneficiaryId,
                        onSelected:
                            (b) =>
                                setState(() => _beneficiaryId = b.id),
                        onNew:
                            () => context.push('/beneficiaries/new'),
                      ),
                  ],
                ),
              ),
              SafeArea(
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
                  child: SizedBox(
                    width: double.infinity,
                    child: FilledButton(
                      key: const Key('destination-continue'),
                      onPressed: _canContinue ? () => _continue(data) : null,
                      style: FilledButton.styleFrom(
                        minimumSize: const Size(88, 52),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(AppRadii.md),
                        ),
                      ),
                      child: const Text('Continuar'),
                    ),
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _DestinationData {
  const _DestinationData({required this.accounts, required this.beneficiaries});

  final List<Account> accounts;
  final List<Beneficiary> beneficiaries;
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Text(
      text,
      style: AppTypography.labelMd.copyWith(
        fontSize: 12,
        color: AppColors.secondary,
      ),
    );
  }
}

class _BeneficiaryOptions extends StatelessWidget {
  const _BeneficiaryOptions({
    required this.beneficiaries,
    required this.selectedId,
    required this.onSelected,
    required this.onNew,
  });

  final List<Beneficiary> beneficiaries;
  final String? selectedId;
  final ValueChanged<Beneficiary> onSelected;
  final VoidCallback onNew;

  @override
  Widget build(BuildContext context) {
    if (beneficiaries.isEmpty) {
      return EmptyView(
        message: 'Aún no tienes beneficiarios.\n'
            'Guarda tus cuentas frecuentes para transferir en un toque.',
        actionLabel: 'Agregar beneficiario',
        onAction: onNew,
      );
    }
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        for (final b in beneficiaries) ...[
          AppCard(
            key: Key('transfer-beneficiary-card-${b.id}'),
            padding: const EdgeInsets.all(14),
            onTap: () => onSelected(b),
            child: Row(
              children: [
                Container(
                  width: 44,
                  height: 44,
                  alignment: Alignment.center,
                  decoration: const BoxDecoration(
                    color: AppColors.surfaceContainerHigh,
                    shape: BoxShape.circle,
                  ),
                  child: Text(
                    b.initials,
                    style: AppTypography.labelMd.copyWith(
                      fontWeight: FontWeight.bold,
                      color: AppColors.onSurfaceVariant,
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        b.alias,
                        style: AppTypography.bodyLg.copyWith(
                          fontWeight: FontWeight.w600,
                          color: AppColors.onSurface,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        b.holderName.isEmpty ? '••••' : b.holderName,
                        style: AppTypography.bodyMd.copyWith(
                          fontSize: 13,
                          color: AppColors.onSurfaceVariant,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        '${b.accountMasked} · ${currencyAccountLabel(b.currency)}',
                        style: AppTypography.bodyMd.copyWith(
                          fontSize: 12,
                          color: AppColors.outline,
                        ),
                      ),
                    ],
                  ),
                ),
                Icon(
                  selectedId == b.id
                      ? Icons.radio_button_checked
                      : Icons.radio_button_off,
                  key: Key('transfer-beneficiary-${b.id}'),
                  color: selectedId == b.id
                      ? AppColors.primary
                      : AppColors.outline,
                ),
              ],
            ),
          ),
          const SizedBox(height: 8),
        ],
      ],
    );
  }
}
