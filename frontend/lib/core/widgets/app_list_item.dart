import 'package:flutter/material.dart';

import '../theme/app_colors.dart';
import '../theme/app_spacing.dart';
import '../theme/app_typography.dart';

/// List item (`docs/20` §6): alto minimo 64, divisor `divider`, icono en
/// circulo con 5% de `primary`.
///
/// Presentacion pura: no navega ni decide nada.
class AppListItem extends StatelessWidget {
  const AppListItem({
    super.key,
    required this.title,
    this.subtitle,
    this.leadingIcon,
    this.trailing,
    this.onTap,
  });

  /// Diametro del circulo del icono leading (`docs/20` §6, CA-03 de F-T34:
  /// token nombrado en vez de `40` hardcodeado; el icono interior es 24).
  static const double leadingCircleDiameter = 40;

  final String title;
  final String? subtitle;
  final IconData? leadingIcon;
  final Widget? trailing;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final content = Container(
      constraints: const BoxConstraints(minHeight: 64),
      padding: const EdgeInsets.symmetric(
        horizontal: AppSpacing.stackMd,
        vertical: AppSpacing.stackSm + AppSpacing.unit,
      ),
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: AppColors.divider)),
      ),
      child: Row(
        children: [
          if (leadingIcon != null) ...[
            Container(
              width: AppListItem.leadingCircleDiameter,
              height: AppListItem.leadingCircleDiameter,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: AppColors.primary.withValues(alpha: 0.05),
              ),
              child: Icon(
                leadingIcon,
                size: 24,
                color: AppColors.primary,
              ),
            ),
            const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
          ],
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  title,
                  style: AppTypography.bodyLg.copyWith(color: scheme.onSurface),
                ),
                if (subtitle != null) ...[
                  const SizedBox(height: AppSpacing.unit),
                  Text(
                    subtitle!,
                    style: AppTypography.bodyMd
                        .copyWith(color: AppColors.secondaryText),
                  ),
                ],
              ],
            ),
          ),
          if (trailing != null) ...[
            const SizedBox(width: AppSpacing.stackSm),
            trailing!,
          ],
        ],
      ),
    );
    if (onTap == null) return content;
    return InkWell(onTap: onTap, child: content);
  }
}
