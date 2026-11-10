import 'package:flutter/material.dart';

import '../theme/app_colors.dart';
import '../theme/app_spacing.dart';

/// Card estandar (`docs/20` §6): fondo `surface-container-lowest`,
/// radio 16, `shadow.card`. Padding interno 20 (`container-padding`).
///
/// Presentacion pura: no calcula ni decide nada.
class AppCard extends StatelessWidget {
  const AppCard({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(AppSpacing.containerPadding),
    this.margin = EdgeInsets.zero,
    this.onTap,
  });

  final Widget child;
  final EdgeInsetsGeometry padding;
  final EdgeInsetsGeometry margin;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final body = Ink(
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLowest,
        borderRadius: BorderRadius.circular(AppRadii.lg),
        boxShadow: AppShadows.cardList,
      ),
      child: Padding(padding: padding, child: child),
    );
    return Padding(
      padding: margin,
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(AppRadii.lg),
        child: onTap == null
            ? body
            : InkWell(
                borderRadius: BorderRadius.circular(AppRadii.lg),
                onTap: onTap,
                child: body,
              ),
      ),
    );
  }
}
