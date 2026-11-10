library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../kyc_flow_controller.dart';

/// Widgets compartidos del rediseño KYC alineado al `.fig` (F-T38).
///
/// Presentación pura (cliente delgado, docs/19): solo componen textos y
/// estilos con los tokens/componentes de F-T34. Los colores/espaciados salen
/// de [AppColors], [AppSpacing], [AppRadii] y [AppTypography]; las dos medidas
/// propias del fig viven en constantes nombradas de esta frontera
/// ([kKycFaceViewportDiameter], [kKycFaceViewportRingWidth]).

/// Barra superior transaccional del flujo (fig `0:789`/`0:355`).
///
/// Fondo `surface-container-low`, título centrado en `primary`, botón
/// volver de al menos 44px. El botón es opcional (el facial inmersivo del
/// fig no siempre lo muestra).
class KycTopBar extends StatelessWidget {
  const KycTopBar({super.key, required this.title, this.onBack});

  final String title;
  final VoidCallback? onBack;

  @override
  Widget build(BuildContext context) {
    return Container(
      color: AppColors.surfaceContainerLow,
      padding: const EdgeInsets.symmetric(
        horizontal: AppSpacing.stackMd,
        vertical: AppSpacing.stackSm,
      ),
      child: SafeArea(
        bottom: false,
        child: Row(
          children: [
            SizedBox(
              width: AppSpacing.stackXl,
              height: AppSpacing.stackXl,
              child: onBack == null
                  ? const SizedBox.shrink()
                  : IconButton(
                      key: const Key('kycBackButton'),
                      onPressed: onBack,
                      icon: const Icon(Icons.arrow_back),
                      color: AppColors.primary,
                      iconSize: AppSpacing.stackLg,
                      tooltip: 'Volver',
                    ),
            ),
            Expanded(
              child: Text(
                title,
                textAlign: TextAlign.center,
                style: AppTypography.titleMd.copyWith(
                  color: AppColors.primary,
                ),
              ),
            ),
            const SizedBox(
              width: AppSpacing.stackXl,
              height: AppSpacing.stackXl,
            ),
          ],
        ),
      ),
    );
  }
}

/// Stepper de progreso del flujo (fig `0:789`: 4 segmentos + etiqueta).
///
/// [step] es 1-based, [total] el número de segmentos. Los segmentos
/// completados/actual usan `primary-container`; los pendientes, `divider`.
/// La etiqueta sigue el formato del fig: `Paso X de Y · <etapa>`.
class KycProgressStepper extends StatelessWidget {
  const KycProgressStepper({
    super.key,
    required this.step,
    required this.total,
    required this.stageLabel,
  });

  final int step;
  final int total;
  final String stageLabel;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          children: [
            for (var i = 1; i <= total; i++)
              Expanded(
                child: Padding(
                  padding: EdgeInsets.only(
                    right: i == total ? 0 : AppSpacing.stackSm,
                  ),
                  child: Container(
                    height: AppSpacing.unit,
                    decoration: BoxDecoration(
                      color: i <= step
                          ? AppColors.primaryContainer
                          : AppColors.divider,
                      borderRadius: BorderRadius.circular(AppRadii.full),
                    ),
                  ),
                ),
              ),
          ],
        ),
        const SizedBox(height: AppSpacing.stackSm),
        Text(
          'Paso $step de $total · $stageLabel',
          style: AppTypography.labelSm.copyWith(
            color: AppColors.secondaryText,
          ),
        ),
      ],
    );
  }
}

/// Título + subtítulo de sección (fig `0:789`/`0:355`).
///
/// Título en `headline-sm`/`primary`, descripción en `body-md`/
/// `secondary-text` (docs/20 §4).
class KycSectionHeader extends StatelessWidget {
  const KycSectionHeader({
    super.key,
    required this.title,
    required this.subtitle,
    this.titleStyle,
  });

  final String title;
  final String subtitle;
  final TextStyle? titleStyle;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          title,
          style:
              titleStyle ??
              AppTypography.headlineSm.copyWith(color: AppColors.primary),
        ),
        const SizedBox(height: AppSpacing.stackSm),
        Text(
          subtitle,
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.secondaryText,
          ),
        ),
      ],
    );
  }
}

/// Franja informativa del fig (fig `0:789`: fondo 10% de
/// `primary-container`, radio 12).
class KycInfoStrip extends StatelessWidget {
  const KycInfoStrip({super.key, required this.message, this.icon = Icons.info_outline});

  final String message;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(AppSpacing.stackMd - AppSpacing.unit),
      decoration: BoxDecoration(
        color: AppColors.primaryContainer.withValues(alpha: 0.1),
        borderRadius: BorderRadius.circular(AppRadii.md),
        border: Border.all(
          color: AppColors.primaryContainer.withValues(alpha: 0.2),
        ),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(
            Icons.info_outline,
            size: AppSpacing.stackLg,
            color: AppColors.primaryContainer,
          ),
          const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
          Expanded(
            child: Text(
              message,
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.primaryContainer,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Pie de seguridad del fig (fig `0:355`): candado + texto centrado en
/// `secondary-text`.
class KycSecurityFooter extends StatelessWidget {
  const KycSecurityFooter({super.key, required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Icon(
          Icons.lock_outline,
          size: AppSpacing.stackMd,
          color: AppColors.secondaryText,
        ),
        const SizedBox(width: AppSpacing.stackSm),
        Flexible(
          child: Text(
            message,
            textAlign: TextAlign.center,
            style: AppTypography.labelSm.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
        ),
      ],
    );
  }
}

/// Banner de errores de formulario (fig `0:407`: fondo 8% de
/// `error-carmine`, radio 10, texto `error-carmine`).
class KycFormErrorBanner extends StatelessWidget {
  const KycFormErrorBanner({super.key, required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      key: const Key('kycFormErrorBanner'),
      padding: const EdgeInsets.all(AppSpacing.stackSm + AppSpacing.unit),
      decoration: BoxDecoration(
        color: AppColors.errorCarmine.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(AppRadii.md - AppSpacing.unit),
      ),
      child: Row(
        children: [
          const Icon(
            Icons.error_outline,
            size: AppSpacing.stackMd,
            color: AppColors.errorCarmine,
          ),
          const SizedBox(width: AppSpacing.stackSm),
          Expanded(
            child: Text(
              message,
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.errorCarmine,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Tarjeta de contenido del flujo (fig: card blanca, radio 14/16, padding
/// 20). Envuelve [AppCard] para no duplicar el estilo canónico.
class KycFormCard extends StatelessWidget {
  const KycFormCard({super.key, required this.children, this.spacing = AppSpacing.stackLg});

  final List<Widget> children;
  final double spacing;

  @override
  Widget build(BuildContext context) {
    final separated = <Widget>[];
    for (var i = 0; i < children.length; i++) {
      if (i > 0) separated.add(SizedBox(height: spacing));
      separated.add(children[i]);
    }
    return AppCard(child: Column(children: separated));
  }
}

/// Borde de error para inputs (fig `0:407`: borde `error-carmine`).
InputBorder kycInputBorder(Color color, {double width = 1}) {
  return OutlineInputBorder(
    borderRadius: BorderRadius.circular(AppRadii.md),
    borderSide: BorderSide(color: color, width: width),
  );
}

/// Decoración canónica de los campos del formulario KYC.
///
/// Fondo `surface-container-lowest`, borde 1 `outline-variant`, foco 2
/// `primary`, error `error-carmine`, radio 12, placeholder en
/// `secondary-text` (docs/20 §6 + fig `0:789`).
InputDecoration kycFieldDecoration({
  required String hintText,
  String? errorText,
  Widget? prefixIcon,
  bool hasError = false,
}) {
  final enabled = hasError
      ? kycInputBorder(AppColors.errorCarmine)
      : kycInputBorder(AppColors.outlineVariant);
  final focused = hasError
      ? kycInputBorder(AppColors.errorCarmine, width: 2)
      : kycInputBorder(AppColors.primary, width: 2);
  return InputDecoration(
    hintText: hintText,
    hintStyle: AppTypography.bodyLg.copyWith(color: AppColors.secondaryText),
    errorText: errorText,
    errorStyle: AppTypography.labelSm.copyWith(color: AppColors.errorCarmine),
    filled: true,
    fillColor: AppColors.surfaceContainerLowest,
    contentPadding: const EdgeInsets.symmetric(
      horizontal: AppSpacing.stackMd,
      vertical: AppSpacing.stackSm + AppSpacing.unit,
    ),
    prefixIcon: prefixIcon,
    enabledBorder: enabled,
    focusedBorder: focused,
    errorBorder: kycInputBorder(AppColors.errorCarmine),
    focusedErrorBorder: kycInputBorder(AppColors.errorCarmine, width: 2),
    disabledBorder: kycInputBorder(AppColors.outlineVariant),
  );
}

/// Etiqueta persistente sobre el campo (fig: `label-md`, color `primary`).
class KycFieldLabel extends StatelessWidget {
  const KycFieldLabel({super.key, required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Text(
      text,
      style: AppTypography.labelMd.copyWith(color: AppColors.primary),
    );
  }
}

/// Texto de ayuda bajo el campo (fig: `label-sm`, `secondary-text`).
class KycFieldHelper extends StatelessWidget {
  const KycFieldHelper({super.key, required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: AppSpacing.unit),
      child: Text(
        text,
        style: AppTypography.labelSm.copyWith(
          color: AppColors.secondaryText,
        ),
      ),
    );
  }
}

/// Diálogo de confirmación de retroceso voluntario (F-T47, decisión del
/// dueño): avisa que al retroceder se reiniciará TODO el proceso de creación
/// de cuenta.
///
/// Solo presentación con tokens (docs/20 §3/§6): sin PII (no muestra número
/// de documento, nombres ni correo), sin logs, objetivos táctiles >= 44px.
/// Devuelve `true` solo si el usuario confirma; cualquier otra salida
/// (cancelar, fuera del diálogo, botón del sistema) es `false`.
Future<bool> showKycRestartConfirmDialog(BuildContext context) async {
  final confirmed = await showDialog<bool>(
    context: context,
    barrierDismissible: true,
    builder: (dialogContext) => AlertDialog(
      key: const Key('kycBackConfirmDialog'),
      backgroundColor: AppColors.surfaceContainerLowest,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      title: Text(
        '¿Volver al inicio?',
        style: AppTypography.titleMd.copyWith(color: AppColors.primary),
      ),
      content: Text(
        'Al retroceder se reiniciará todo el proceso de creación de cuenta '
        'y deberás empezar de nuevo.',
        style: AppTypography.bodyMd.copyWith(
          color: AppColors.onSurfaceVariant,
        ),
      ),
      actions: [
        TextButton(
          key: const Key('kycBackCancelButton'),
          style: TextButton.styleFrom(
            minimumSize: const Size(48, 48),
            foregroundColor: AppColors.primary,
          ),
          onPressed: () => Navigator.of(dialogContext).pop(false),
          child: const Text('Cancelar'),
        ),
        FilledButton(
          key: const Key('kycBackConfirmButton'),
          style: FilledButton.styleFrom(
            minimumSize: const Size(48, 48),
            backgroundColor: AppColors.primary,
            foregroundColor: AppColors.onPrimary,
          ),
          onPressed: () => Navigator.of(dialogContext).pop(true),
          child: const Text('Sí, empezar de nuevo'),
        ),
      ],
    ),
  );
  return confirmed ?? false;
}

/// Retroceso voluntario del KYC (F-T47): pide confirmación y, al confirmar,
/// reinicia TODO ([KycFlowController.resetFull]) y vuelve a `/kyc` con `go`
/// (el `go` no dispara los `PopScope`, así que no hay doble diálogo).
///
/// Al cancelar (o cerrar el diálogo) no navega ni cambia el estado.
/// No toca la sesión: solo el estado en memoria del flujo KYC.
Future<void> requestKycBackRestart(
  BuildContext context,
  KycFlowController controller,
) async {
  final confirmed = await showKycRestartConfirmDialog(context);
  if (!confirmed || !context.mounted) return;
  controller.resetFull();
  if (context.mounted) context.go('/kyc');
}

/// Diámetro del viewport facial del fig `0:198` (constante nombrada de la
/// frontera KYC; no es token de `core/theme`).
const double kKycFaceViewportDiameter = 260;

/// Grosor del anillo del viewport facial (fig `0:198`).
const double kKycFaceViewportRingWidth = 3;

/// Marco circular claro para el avatar facial sin cámara (fig `0:198`
/// adaptado a modo claro según docs/20: anillo `secondary-container` sobre
/// `surface-container-low`, sin tema oscuro).
///
/// Solo envuelve contenido compacto (iconos): nunca recorta al
/// [KycCameraPreview], cuyos estados internos (inicializando/denegado/no
/// disponible) necesitan su altura natural.
class KycFaceViewportFrame extends StatelessWidget {
  const KycFaceViewportFrame({
    super.key,
    required this.child,
    this.hasError = false,
    this.diameter = kKycFaceViewportDiameter,
  });

  final Widget child;
  final bool hasError;
  final double diameter;

  @override
  Widget build(BuildContext context) {
    final ring = hasError ? AppColors.errorCarmine : AppColors.secondaryContainer;
    return Center(
      child: Container(
        width: diameter,
        height: diameter,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: AppColors.surfaceContainerLow,
          border: Border.all(color: ring, width: kKycFaceViewportRingWidth),
        ),
        child: ClipOval(child: child),
      ),
    );
  }
}
