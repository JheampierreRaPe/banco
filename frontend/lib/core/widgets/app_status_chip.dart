import 'package:flutter/material.dart';

import '../theme/app_colors.dart';
import '../theme/app_spacing.dart';
import '../theme/app_typography.dart';

/// Estado visual de un [AppStatusChip].
///
/// `info` usa `secondary-container`/`on-secondary-container` y sirve tambien
/// como indicador de saldo (`docs/20` §6).
enum AppStatus { success, warning, error, info }

/// Chip/badge de estado (`docs/20` §6): fondo = contenedor del estado,
/// texto = color del estado. Pill (`rounded.full`).
///
/// Presentacion pura: no decide el estado, solo lo muestra.
class AppStatusChip extends StatelessWidget {
  const AppStatusChip({
    super.key,
    required this.label,
    required this.status,
  });

  const AppStatusChip.success({super.key, required this.label})
      : status = AppStatus.success;

  const AppStatusChip.warning({super.key, required this.label})
      : status = AppStatus.warning;

  const AppStatusChip.error({super.key, required this.label})
      : status = AppStatus.error;

  const AppStatusChip.info({super.key, required this.label})
      : status = AppStatus.info;

  final String label;
  final AppStatus status;

  Color get background {
    switch (status) {
      case AppStatus.success:
        return AppColors.successContainer;
      case AppStatus.warning:
        return AppColors.warningContainer;
      case AppStatus.error:
        return AppColors.errorContainer;
      case AppStatus.info:
        return AppColors.secondaryContainer;
    }
  }

  Color get foreground {
    switch (status) {
      case AppStatus.success:
        return AppColors.onSuccessContainer;
      case AppStatus.warning:
        return AppColors.onWarningContainer;
      case AppStatus.error:
        return AppColors.onErrorContainer;
      case AppStatus.info:
        return AppColors.onSecondaryContainer;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(
        horizontal: AppSpacing.stackSm + AppSpacing.unit,
        vertical: AppSpacing.unit + 2,
      ),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(AppRadii.full),
      ),
      child: Text(
        label,
        style: AppTypography.labelSm.copyWith(color: foreground),
      ),
    );
  }
}
