// Bottom sheet del reto biometrico (E2-T21, HU08).
//
// Replica el mockup `Autorización Biométrica` (`0:3964`, fuente visual
// "Eucalipto y Ocre", Inter):
// - Agarradera + circulo de huella (`#EAF0EA`, icono `fingerprint`).
// - Titulo `Autoriza la operación` (20/600, `#1B1C19`).
// - Subtitulo `Usa tu huella para enviar <monto> a <destinatario>.`
//   (15, `#6B7268`).
// - Divisor `o`, boton `Usar mi PIN de seguridad` (borde `#2B4034`) y
//   accion `Cancelar`.
//
// 4 estados de UI: reposo (pide huella) / cargando (autorizando) /
// error (mensaje + reintento) / exito (lo cierra el llamante).
library;

import 'package:flutter/material.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';

/// Desenlace del sheet para el llamante (flujo de pago).
enum BiometricSheetOutcome {
  /// El usuario pidio el desafio biometrico (el llamante corre el
  /// [PaymentAuthorizationController] y actualiza el sheet).
  authorize,

  /// Fallback a PIN (borde del brief E2-T21).
  usePin,

  /// El usuario cancelo.
  cancelled,
}

/// Hoja del reto biometrico (contenido del `showModalBottomSheet`).
class BiometricChallengeSheet extends StatelessWidget {
  const BiometricChallengeSheet({
    super.key,
    required this.amountLabel,
    required this.recipientLabel,
    this.isAuthorizing = false,
    this.errorMessage = '',
    this.onAuthorize,
    this.onUsePin,
    this.onCancel,
    this.onRetry,
  });

  /// Monto exhibido (`S/ 250.00`; viene del servidor, solo display).
  final String amountLabel;

  /// Destinatario enmascarado (nunca PII en claro).
  final String recipientLabel;

  /// `true` mientras se firma/autoriza (spinner, sin bloquear el hilo).
  final bool isAuthorizing;

  /// Mensaje de fallo claro (vacio = sin error).
  final String errorMessage;

  final VoidCallback? onAuthorize;
  final VoidCallback? onUsePin;
  final VoidCallback? onCancel;
  final VoidCallback? onRetry;

  bool get _hasError => errorMessage.isNotEmpty;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      top: false,
      child: SingleChildScrollView(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 36,
                height: 4,
                decoration: BoxDecoration(
                  color: AppColors.outlineVariant,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(height: 16),
              Container(
                width: 72,
                height: 72,
                alignment: Alignment.center,
                decoration: const BoxDecoration(
                  color: Color(0xFFEAF0EA),
                  shape: BoxShape.circle,
                ),
                child: isAuthorizing
                    ? const SizedBox.square(
                        key: Key('biometric-loading'),
                        dimension: 32,
                        child: CircularProgressIndicator(strokeWidth: 3),
                      )
                    : const Icon(
                        Icons.fingerprint,
                        key: Key('biometric-icon'),
                        size: 34,
                        color: AppColors.primaryContainer,
                      ),
              ),
              const SizedBox(height: 16),
              Text(
                'Autoriza la operación',
                key: const Key('biometric-title'),
                style: AppTypography.titleMd.copyWith(
                  fontSize: 20,
                  color: AppColors.onSurface,
                ),
              ),
              const SizedBox(height: 8),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Text(
                  'Usa tu huella para enviar $amountLabel a $recipientLabel.',
                  key: const Key('biometric-subtitle'),
                  textAlign: TextAlign.center,
                  style: AppTypography.bodyMd.copyWith(
                    fontSize: 15,
                    color: AppColors.secondaryText,
                  ),
                ),
              ),
              if (_hasError) ...[
                const SizedBox(height: 12),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: AppColors.errorContainer,
                    borderRadius: BorderRadius.circular(AppRadii.def),
                  ),
                  child: Text(
                    errorMessage,
                    key: const Key('biometric-error'),
                    textAlign: TextAlign.center,
                    style: AppTypography.bodyMd.copyWith(
                      fontSize: 13,
                      color: AppColors.onErrorContainer,
                    ),
                  ),
                ),
              ],
              const SizedBox(height: 16),
              SizedBox(
                width: double.infinity,
                child: FilledButton.icon(
                  key: const Key('biometric-authorize'),
                  onPressed: isAuthorizing
                      ? null
                      : (onRetry ?? onAuthorize) ??
                            () =>
                                Navigator.of(context)
                                    .pop(BiometricSheetOutcome.authorize),
                  style: FilledButton.styleFrom(
                    minimumSize: const Size(88, 52),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(AppRadii.md),
                    ),
                  ),
                  icon: const Icon(Icons.fingerprint, size: 20),
                  label: Text(
                    _hasError
                        ? 'Reintentar con huella'
                        : 'Autorizar con huella',
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  const Expanded(child: Divider(color: AppColors.divider)),
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    child: Text(
                      'o',
                      style: AppTypography.bodyMd.copyWith(
                        fontSize: 13,
                        color: AppColors.secondaryText,
                      ),
                    ),
                  ),
                  const Expanded(child: Divider(color: AppColors.divider)),
                ],
              ),
              const SizedBox(height: 12),
              SizedBox(
                width: double.infinity,
                child: OutlinedButton.icon(
                  key: const Key('biometric-pin-fallback'),
                  onPressed: isAuthorizing
                      ? null
                      : onUsePin ??
                            () =>
                                Navigator.of(context)
                                    .pop(BiometricSheetOutcome.usePin),
                  style: OutlinedButton.styleFrom(
                    minimumSize: const Size(88, 48),
                    side: const BorderSide(
                      color: AppColors.primaryContainer,
                      width: 1.5,
                    ),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(AppRadii.def),
                    ),
                    foregroundColor: AppColors.primaryContainer,
                  ),
                  icon: const Icon(Icons.keyboard_outlined, size: 20),
                  label: const Text(
                    'Usar mi PIN de seguridad',
                    style: TextStyle(fontWeight: FontWeight.w600),
                  ),
                ),
              ),
              TextButton(
                key: const Key('biometric-cancel'),
                onPressed: isAuthorizing
                    ? null
                    : onCancel ??
                          () =>
                              Navigator.of(context)
                                  .pop(BiometricSheetOutcome.cancelled),
                child: Text(
                  'Cancelar',
                  style: AppTypography.bodyMd.copyWith(
                    fontSize: 15,
                    fontWeight: FontWeight.w500,
                    color: AppColors.secondaryText,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Muestra el reto como `BottomSheet` modal y devuelve su desenlace.
///
/// El llamante (flujo de pago) corre el [PaymentAuthorizationController]
/// cuando el desenlace es [BiometricSheetOutcome.authorize]. `isDismissible`
/// es `false` durante la autorizacion para no perder el intento.
Future<BiometricSheetOutcome?> showBiometricChallengeSheet(
  BuildContext context, {
  required String amountLabel,
  required String recipientLabel,
  bool isAuthorizing = false,
  String errorMessage = '',
  VoidCallback? onAuthorize,
  VoidCallback? onUsePin,
  VoidCallback? onCancel,
  VoidCallback? onRetry,
}) {
  return showModalBottomSheet<BiometricSheetOutcome>(
    context: context,
    isDismissible: !isAuthorizing,
    enableDrag: !isAuthorizing,
    backgroundColor: AppColors.surfaceContainerLowest,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
    ),
    builder: (_) => BiometricChallengeSheet(
      amountLabel: amountLabel,
      recipientLabel: recipientLabel,
      isAuthorizing: isAuthorizing,
      errorMessage: errorMessage,
      onAuthorize: onAuthorize,
      onUsePin: onUsePin,
      onCancel: onCancel,
      onRetry: onRetry,
    ),
  );
}
