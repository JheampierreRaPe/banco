import 'package:flutter/material.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../kyc_flow_controller.dart';

/// Overlay de estado sobre el viewfinder durante la captura/evaluacion (F-T25).
///
/// NO reemplaza la pantalla ni el preview ([CameraPreview] / placeholder):
/// se dibuja ENCIMA para que el usuario siga viendose y siga moviendose, y el
/// challenge no quede a medias. Solo presenta la fase que reporta el
/// controlador ([KycTaskPhase]); no decide nada (cliente delgado, docs/19).
///
/// Restyle F-T38: scrim y texto con tokens de F-T34 (sin colores
/// hardcodeados).
class KycCaptureOverlay extends StatelessWidget {
  const KycCaptureOverlay({super.key, required this.phase});

  /// Fase vigente reportada por [KycFlowController.taskPhase].
  final KycTaskPhase phase;

  /// Texto claro y en espanol para cada fase.
  static String messageFor(KycTaskPhase phase) {
    switch (phase) {
      case KycTaskPhase.capturing:
        return 'Capturando...';
      case KycTaskPhase.evaluating:
        return 'Verificando...';
      case KycTaskPhase.idle:
        return 'Procesando...';
    }
  }

  @override
  Widget build(BuildContext context) {
    return ColoredBox(
      color: AppColors.inverseSurface.withValues(alpha: 0.66),
      child: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const SizedBox(
              width: 36,
              height: 36,
              child: CircularProgressIndicator(
                color: AppColors.onPrimary,
                strokeWidth: 3,
              ),
            ),
            const SizedBox(height: AppSpacing.stackSm + AppSpacing.unit),
            Text(
              messageFor(phase),
              key: const Key('kycCaptureOverlay'),
              style: AppTypography.titleMd.copyWith(color: AppColors.onPrimary),
            ),
          ],
        ),
      ),
    );
  }
}
