import 'package:flutter/material.dart';

import '../../../../core/theme/app_colors.dart';
import '../../../../core/theme/app_spacing.dart';
import '../../../../core/theme/app_typography.dart';
import '../../models/transfer.dart';

/// Banner de estado de la verificacion del CCI (mockups `CCI 2/3/4` +
/// `Formulario Interbancario CCI`, `pantallas.fig`).
///
/// - [CciState.verifying]: `Consultando CCE...` (mockup `CCI 2`).
/// - [CciState.verified]: `TITULAR CCE VERIFICADO` + titular enmascarado
///   (el nombre en claro jamas se exhibe).
/// - [CciState.notFound]: mensaje neutro `404 HOLDER_NOT_FOUND`
///   (mockup `CCI 3`, sin eco del CCI).
/// - [CciState.unavailable]: `503` con boton de reintento (mockup `CCI 4`);
///   el reintento reutiliza la misma `Idempotency-Key` del intento en curso.
/// - [CciState.idle]: no dibuja nada (el formulario muestra su ayuda neutra).
///
/// Presentacion pura: no verifica ni decide nada.
class CciStatusBanner extends StatelessWidget {
  const CciStatusBanner({
    super.key,
    required this.state,
    this.holderMasked = '',
    this.accountLabel = '',
    this.onRetry,
    this.retryKey,
  });

  final CciState state;

  /// Titular YA enmascarado por el backend (`tracking.holder_name_masked`).
  final String holderMasked;

  /// Sub-etiqueta de cuenta/banco (p. ej. `Cuenta Corriente Soles · BCP`).
  final String accountLabel;
  final VoidCallback? onRetry;
  final Key? retryKey;

  @override
  Widget build(BuildContext context) {
    switch (state) {
      case CciState.idle:
        return const SizedBox.shrink();
      case CciState.verifying:
        return _shell(
          key: const Key('cci-banner-verifying'),
          background: AppColors.surfaceContainer,
          icon: const SizedBox.square(
            dimension: 20,
            child: CircularProgressIndicator(strokeWidth: 2.5),
          ),
          title: 'Consultando Cámara de Compensación Electrónica (CCE)...',
          titleColor: AppColors.onSurface,
          subtitle: 'Validando titularidad y tipo de cuenta interbancaria',
        );
      case CciState.verified:
        return _shell(
          key: const Key('cci-banner-verified'),
          background: AppColors.successContainer,
          icon: const Icon(
            Icons.check_circle,
            size: 20,
            color: AppColors.primary,
          ),
          title: 'TITULAR CCE VERIFICADO',
          titleColor: AppColors.primary,
          subtitle: holderMasked.isEmpty ? null : holderMasked,
          extra: accountLabel.isEmpty
              ? null
              : Text(
                  accountLabel,
                  style: AppTypography.bodyMd.copyWith(
                    fontSize: 12,
                    color: AppColors.onSurfaceVariant,
                  ),
                ),
        );
      case CciState.notFound:
        return _shell(
          key: const Key('cci-banner-not-found'),
          background: AppColors.surfaceContainerHigh,
          icon: Container(
            width: 24,
            height: 24,
            alignment: Alignment.center,
            decoration: const BoxDecoration(
              color: AppColors.outlineVariant,
              shape: BoxShape.circle,
            ),
            child: const Icon(
              Icons.info,
              size: 16,
              color: AppColors.surfaceContainerLowest,
            ),
          ),
          title: 'No pudimos validar el titular con los datos ingresados',
          titleColor: AppColors.onSurface,
          subtitle: 'Por medidas de seguridad bancaria, verifica el número '
              'de CCI directamente con el destinatario antes de continuar '
              'con la transacción.',
        );
      case CciState.unavailable:
        return Container(
          key: const Key('cci-banner-unavailable'),
          width: double.infinity,
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            color: AppColors.warningContainer,
            borderRadius: BorderRadius.circular(AppRadii.md),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(
                children: [
                  Container(
                    width: 24,
                    height: 24,
                    alignment: Alignment.center,
                    decoration: const BoxDecoration(
                      color: AppColors.secondary,
                      shape: BoxShape.circle,
                    ),
                    child: const Icon(
                      Icons.warning_amber,
                      size: 16,
                      color: AppColors.onSecondary,
                    ),
                  ),
                  const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Servicio interbancario no disponible temporalmente',
                          style: AppTypography.labelMd.copyWith(
                            fontSize: 12,
                            color: AppColors.onSecondaryFixed,
                          ),
                        ),
                        const SizedBox(height: 2),
                        Text(
                          'La CCE está experimentando una interrupción en su '
                          'nodo de validación en tiempo real.',
                          style: AppTypography.bodyMd.copyWith(
                            fontSize: 11,
                            color: AppColors.onWarningContainer,
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
              if (onRetry != null) ...[
                const SizedBox(height: 10),
                SizedBox(
                  height: 36,
                  child: FilledButton.icon(
                    key: retryKey ?? const Key('cci-retry'),
                    onPressed: onRetry,
                    icon: const Icon(Icons.refresh, size: 16),
                    label: const Text('Reintentar verificación'),
                    style: FilledButton.styleFrom(
                      backgroundColor: AppColors.secondary,
                      foregroundColor: AppColors.onSecondary,
                      textStyle: AppTypography.labelMd.copyWith(fontSize: 12),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(AppRadii.def),
                      ),
                    ),
                  ),
                ),
              ],
            ],
          ),
        );
    }
  }

  Widget _shell({
    required Key key,
    required Color background,
    required Widget icon,
    required String title,
    required Color titleColor,
    String? subtitle,
    Widget? extra,
  }) {
    return Container(
      key: key,
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(AppRadii.md),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          icon,
          const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  title,
                  style: AppTypography.labelMd.copyWith(
                    fontSize: 12,
                    color: titleColor,
                  ),
                ),
                if (subtitle != null && subtitle.isNotEmpty) ...[
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    style: AppTypography.bodyMd.copyWith(
                      fontSize: subtitle.length < 40 ? 14 : 11,
                      fontWeight: subtitle.length < 40
                          ? FontWeight.w600
                          : FontWeight.w400,
                      color: titleColor,
                    ),
                  ),
                ],
                if (extra != null) ...[
                  const SizedBox(height: 2),
                  extra,
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}
