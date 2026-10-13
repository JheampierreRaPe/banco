import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../models/transfer.dart';

/// Monto y motivo (mockup `Monto y Motivo`, `pantallas.fig`).
///
/// Recibe [TransferFlowArgs] por `extra` y navega a `/transfers/confirm`
/// con el borrador actualizado (misma `Idempotency-Key`). Solo captura:
/// no calcula comisiones ni saldos (cliente delgado).
class AmountPage extends StatefulWidget {
  const AmountPage({super.key, required this.args});

  final TransferFlowArgs args;

  @override
  State<AmountPage> createState() => _AmountPageState();
}

class _AmountPageState extends State<AmountPage> {
  final _amountController = TextEditingController();
  final _motiveController = TextEditingController();
  String? _amountError;

  static const _chips = ['20', '50', '100', '200'];

  @override
  void dispose() {
    _amountController.dispose();
    _motiveController.dispose();
    super.dispose();
  }

  int? get _amountMinor => parseAmountToMinor(_amountController.text);

  bool get _valid {
    final minor = _amountMinor;
    return minor != null && minor > 0 && minor <= kMaxAmountMinor;
  }

  void _onAmountChanged(String _) {
    setState(() {
      final minor = _amountMinor;
      if (minor == null) {
        _amountError = 'Ingresa un monto válido (p. ej. 250.00).';
      } else if (minor <= 0) {
        _amountError = 'El monto debe ser mayor a S/ 0.00.';
      } else if (minor > kMaxAmountMinor) {
        _amountError = 'El monto supera el límite de S/ 30,000.';
      } else {
        _amountError = null;
      }
    });
  }

  void _continue() {
    if (!_valid) return;
    final draft = widget.args.draft.copyWith(
      amountMinor: _amountMinor!,
      motive: _motiveController.text.trim(),
    );
    context.push(
      '/transfers/confirm',
      extra: widget.args.withDraft(draft),
    );
  }

  @override
  Widget build(BuildContext context) {
    final args = widget.args;
    final draft = args.draft;
    final destTitle =
        draft.kind == TransferKind.own
            ? draft.beneficiaryAlias
            : draft.beneficiaryAlias;
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Transferir')),
      body: Column(
        children: [
          Expanded(
            child: ListView(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
              children: [
                AppCard(
                  padding: const EdgeInsets.all(16),
                  child: Row(
                    children: [
                      Container(
                        width: 44,
                        height: 44,
                        alignment: Alignment.center,
                        decoration: BoxDecoration(
                          color: AppColors.primaryContainer,
                          borderRadius: BorderRadius.circular(22),
                        ),
                        child: Text(
                          _initials(destTitle),
                          style: AppTypography.labelMd.copyWith(
                            fontSize: 18,
                            color: AppColors.onPrimary,
                          ),
                        ),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              destTitle.isEmpty ? 'Destinatario' : destTitle,
                              style: AppTypography.bodyLg.copyWith(
                                fontWeight: FontWeight.w600,
                                color: AppColors.onSurface,
                              ),
                            ),
                            const SizedBox(height: 2),
                            Text(
                              draft.destinationMasked,
                              style: AppTypography.bodyMd.copyWith(
                                fontSize: 13,
                                color: AppColors.onSurfaceVariant,
                              ),
                            ),
                          ],
                        ),
                      ),
                      TextButton(
                        onPressed: () => context.pop(),
                        child: const Text('Cambiar'),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 24),
                Center(
                  child: Text(
                    '¿Cuánto quieres enviar?',
                    style: AppTypography.bodyMd.copyWith(
                      color: AppColors.onSurfaceVariant,
                    ),
                  ),
                ),
                const SizedBox(height: 8),
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  crossAxisAlignment: CrossAxisAlignment.center,
                  children: [
                    Text(
                      'S/',
                      style: AppTypography.headlineSm.copyWith(
                        color: AppColors.outline,
                      ),
                    ),
                    const SizedBox(width: 8),
                    SizedBox(
                      width: 200,
                      child: TextField(
                        key: const Key('amount-input'),
                        controller: _amountController,
                        keyboardType: const TextInputType.numberWithOptions(
                          decimal: true,
                        ),
                        inputFormatters: [
                          FilteringTextInputFormatter.allow(
                            RegExp(r'[0-9.]'),
                          ),
                        ],
                        onChanged: _onAmountChanged,
                        textAlign: TextAlign.center,
                        style: AppTypography.displayLg.copyWith(
                          fontSize: 44,
                          color: AppColors.onSurface,
                        ),
                        decoration: const InputDecoration(
                          hintText: '0.00',
                          border: InputBorder.none,
                          filled: false,
                        ),
                      ),
                    ),
                  ],
                ),
                const Center(
                  child: SizedBox(
                    width: 180,
                    child: Divider(color: AppColors.divider),
                  ),
                ),
                const SizedBox(height: 8),
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    const Icon(
                      Icons.account_balance_wallet_outlined,
                      size: 16,
                      color: AppColors.outline,
                    ),
                    const SizedBox(width: 6),
                    Text(
                      'Disponible ${formatMoneyMinor(args.source.availableMinor, args.source.currency)}',
                      style: AppTypography.bodyMd.copyWith(
                        fontSize: 13,
                        color: AppColors.onSurfaceVariant,
                      ),
                    ),
                  ],
                ),
                if (_amountError != null) ...[
                  const SizedBox(height: 8),
                  Center(
                    child: Text(
                      _amountError!,
                      key: const Key('amount-error'),
                      style: AppTypography.labelSm.copyWith(
                        color: AppColors.error,
                      ),
                    ),
                  ),
                ],
                const SizedBox(height: 16),
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    for (var i = 0; i < _chips.length; i++) ...[
                      _Chip(
                        key: Key('amount-chip-${_chips[i]}'),
                        label: 'S/ ${_chips[i]}',
                        onTap: () {
                          _amountController.text = '${_chips[i]}.00';
                          _onAmountChanged(_chips[i]);
                        },
                      ),
                      if (i < _chips.length - 1) const SizedBox(width: 8),
                    ],
                  ],
                ),
                const SizedBox(height: 24),
                Text(
                  'Motivo',
                  style: AppTypography.bodyLg.copyWith(
                    fontWeight: FontWeight.w500,
                    color: AppColors.onSurface,
                  ),
                ),
                const SizedBox(height: 8),
                TextField(
                  key: const Key('amount-motive'),
                  controller: _motiveController,
                  maxLength: 40,
                  textInputAction: TextInputAction.done,
                  onChanged: (_) => setState(() {}),
                  decoration: InputDecoration(
                    hintText: 'Ej. Pago del almuerzo',
                    filled: true,
                    fillColor: AppColors.surfaceContainerLowest,
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(AppRadii.def),
                      borderSide: const BorderSide(
                        color: AppColors.divider,
                      ),
                    ),
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
                      key: const Key('amount-continue'),
                      onPressed: _valid ? _continue : null,
                      style: FilledButton.styleFrom(
                        minimumSize: const Size(88, 52),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(AppRadii.def),
                        ),
                      ),
                      child: const Text('Continuar'),
                    ),
                  ),
                  const SizedBox(height: 8),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      const Icon(
                        Icons.lock_outline,
                        size: 14,
                        color: AppColors.outline,
                      ),
                      const SizedBox(width: 4),
                      Text(
                        'Verificarás la operación en el siguiente paso',
                        style: AppTypography.bodyMd.copyWith(
                          fontSize: 12,
                          color: AppColors.outline,
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  String _initials(String value) {
    final words =
        value.split(RegExp(r'\s+')).where((w) => w.isNotEmpty).toList();
    if (words.isEmpty) return '••';
    final first = words.first[0];
    final second = words.length > 1 ? words[1][0] : '';
    return '$first$second'.toUpperCase();
  }
}

class _Chip extends StatelessWidget {
  const _Chip({super.key, required this.label, required this.onTap});

  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(17),
      child: Container(
        height: 34,
        padding: const EdgeInsets.symmetric(horizontal: 16),
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: AppColors.surfaceContainerLowest,
          border: Border.all(color: AppColors.divider),
          borderRadius: BorderRadius.circular(17),
        ),
        child: Text(
          label,
          style: AppTypography.bodyLg.copyWith(
            fontWeight: FontWeight.w500,
            color: AppColors.onSurface,
          ),
        ),
      ),
    );
  }
}
