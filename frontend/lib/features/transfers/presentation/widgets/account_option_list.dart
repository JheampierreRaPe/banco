import 'package:flutter/material.dart';

import '../../../../core/theme/app_colors.dart';
import '../../../../core/theme/app_spacing.dart';
import '../../../../core/theme/app_typography.dart';
import '../../../../core/widgets/app_card.dart';
import '../../../accounts/models/account.dart';
import '../../models/transfer.dart';

/// Etiqueta corta del tipo de cuenta para la UI.
String accountTypeLabel(String type) {
  switch (type.toUpperCase()) {
    case 'SAVINGS':
      return 'Ahorros';
    case 'CHECKING':
      return 'Corriente';
    default:
      return type.isEmpty ? 'Cuenta' : type;
  }
}

/// Selector de cuenta propia (origen o destino).
///
/// Lista las cuentas con numero enmascarado + disponible (mockups
/// `Transferencia Entre Mis Cuentas` y `Formulario Interbancario CCI`).
/// Presentacion pura: no carga ni decide nada.
class AccountOptionList extends StatelessWidget {
  const AccountOptionList({
    super.key,
    required this.accounts,
    required this.selectedId,
    required this.onSelected,
    this.excludeId,
    this.keyPrefix = 'transfer-account',
  });

  final List<Account> accounts;
  final String? selectedId;
  final ValueChanged<Account> onSelected;

  /// Cuenta a ocultar (p. ej. el origen no puede ser destino propio).
  final String? excludeId;
  final String keyPrefix;

  @override
  Widget build(BuildContext context) {
    final visible = accounts.where((a) => a.id != excludeId).toList();
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        for (final account in visible) ...[
          AppCard(
            key: Key('$keyPrefix-${account.id}'),
            padding: const EdgeInsets.all(14),
            onTap: () => onSelected(account),
            child: Row(
              children: [
                Container(
                  width: 44,
                  height: 44,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: AppColors.surfaceContainer,
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                  child: const Icon(
                    Icons.account_balance_wallet_outlined,
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Flexible(
                            child: Text(
                              'Cuenta CuyCash ${accountTypeLabel(account.type)}',
                              style: AppTypography.bodyLg.copyWith(
                                fontWeight: FontWeight.w600,
                                color: AppColors.onSurface,
                              ),
                            ),
                          ),
                          Text(
                            account.accountNumberMasked,
                            style: AppTypography.bodyMd.copyWith(
                              fontSize: 12,
                              color: AppColors.outline,
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 2),
                      Text(
                        'Disponible: ${formatMoneyMinor(account.availableMinor, account.currency)}',
                        style: AppTypography.bodyMd.copyWith(
                          fontSize: 12,
                          color: AppColors.onSurfaceVariant,
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: AppSpacing.stackSm),
                Icon(
                  selectedId == account.id
                      ? Icons.radio_button_checked
                      : Icons.radio_button_off,
                  color: selectedId == account.id
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
