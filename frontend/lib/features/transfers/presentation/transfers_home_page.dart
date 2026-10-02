import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';

/// Hub de transferencias (mockup `Transferencias`, `pantallas.fig`).
///
/// Puerta del flujo E2-T11: tres tipos (propia / tercero / interbancaria
/// CCI) + franja de seguridad. Presentacion pura: solo navega.
class TransfersHomePage extends StatelessWidget {
  const TransfersHomePage({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Transferir dinero')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          Text(
            'Elige el tipo de transferencia',
            style: AppTypography.titleMd.copyWith(
              fontSize: 18,
              color: AppColors.onSurface,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            'Todas las operaciones viajan encriptadas y piden confirmación.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSurfaceVariant,
            ),
          ),
          const SizedBox(height: 16),
          _TypeCard(
            key: const Key('transfers-type-own'),
            icon: Icons.swap_horiz,
            title: 'Entre mis cuentas',
            subtitle: 'Inmediata · Sin comisión',
            onTap: () =>
                context.push('/transfers/destination?kind=own'),
          ),
          const SizedBox(height: 12),
          _TypeCard(
            key: const Key('transfers-type-third'),
            icon: Icons.group_outlined,
            title: 'A un tercero',
            subtitle: 'Cuentas CuyCash · Elige un beneficiario',
            onTap: () =>
                context.push('/transfers/destination?kind=third'),
          ),
          const SizedBox(height: 12),
          _TypeCard(
            key: const Key('transfers-type-interbank'),
            icon: Icons.account_balance_outlined,
            title: 'Interbancaria CCI',
            subtitle: 'A otro banco · CCE inmediata 24/7',
            badge: '24/7',
            onTap: () => context.push('/transfers/interbank'),
          ),
          const SizedBox(height: 16),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerLow,
              borderRadius: BorderRadius.circular(AppRadii.md),
            ),
            child: Row(
              children: [
                const Icon(
                  Icons.lock_outline,
                  size: 14,
                  color: AppColors.outline,
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    'Operación encriptada TLS 1.3 · Nunca compartas tu PIN',
                    style: AppTypography.bodyMd.copyWith(
                      fontSize: 12,
                      color: AppColors.outline,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _TypeCard extends StatelessWidget {
  const _TypeCard({
    super.key,
    required this.icon,
    required this.title,
    required this.subtitle,
    this.badge,
    required this.onTap,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final String? badge;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return AppCard(
      padding: const EdgeInsets.all(16),
      onTap: onTap,
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
            child: Icon(icon, color: AppColors.primary),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  style: AppTypography.bodyLg.copyWith(
                    fontWeight: FontWeight.w600,
                    color: AppColors.onSurface,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  subtitle,
                  style: AppTypography.bodyMd.copyWith(
                    fontSize: 13,
                    color: AppColors.onSurfaceVariant,
                  ),
                ),
              ],
            ),
          ),
          if (badge != null)
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              decoration: BoxDecoration(
                color: AppColors.successContainer,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text(
                badge!,
                style: AppTypography.labelSm.copyWith(
                  color: AppColors.onSuccessContainer,
                ),
              ),
            ),
          const SizedBox(width: 4),
          const Icon(Icons.chevron_right, color: AppColors.outline),
        ],
      ),
    );
  }
}
