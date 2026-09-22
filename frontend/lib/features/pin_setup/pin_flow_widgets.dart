// Widgets compartidos del cierre del registro (F-T39, fig `0:497`/`0:604`).
//
// Presentacion pura (cliente delgado, docs/19): solo componen textos y
// estilos con los tokens/componentes de F-T34 ([AppColors], [AppSpacing],
// [AppRadii], [AppTypography]). Las medidas propias del fig viven en
// constantes nombradas de esta frontera (precedente F-T38:
// `kKycFaceViewportDiameter`).
library;

import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/app_spacing.dart';
import '../../core/theme/app_typography.dart';
import 'pin_setup_validators.dart';

/// Ancho de cada casilla del PIN/OTP (fig `0:497`: 46x56).
const double kPinBoxWidth = 46;

/// Alto de cada casilla del PIN/OTP (fig `0:497`: 46x56).
const double kPinBoxHeight = 56;

/// Separacion entre casillas (fig `0:497`: 10px).
const double kPinBoxGap = 10;

/// Alto de cada tecla del teclado numerico propio (fig: 66px, >= 44px
/// segun docs/20 §8).
const double kPinKeyHeight = 66;

/// Radio de cada tecla (fig: 16px).
const double kPinKeyRadius = 16;

/// Diametro del punto que enmascara un digito PIN ya ingresado.
const double kPinDotDiameter = 10;

/// Cabecera comun del flujo de seguridad (fig `0:497`/`0:604`/`0:704`):
/// barra superior con volver (>= 44px), titulo centrado y stepper de 4
/// segmentos + leyenda `Paso X de 4 · <etapa>`.
class PinFlowHeader extends StatelessWidget {
  const PinFlowHeader({
    super.key,
    required this.title,
    required this.step,
    required this.stageLabel,
    this.onBack,
  });

  final String title;
  final int step;
  final String stageLabel;
  final VoidCallback? onBack;

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          children: [
            SizedBox(
              width: AppSpacing.stackXl,
              height: AppSpacing.stackXl,
              child: onBack == null
                  ? const SizedBox.shrink()
                  : IconButton(
                      key: const Key('pin-flow-back'),
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
        const SizedBox(height: AppSpacing.stackSm),
        Row(
          children: [
            for (var i = 1; i <= 4; i++)
              Expanded(
                child: Padding(
                  padding: EdgeInsets.only(
                    right: i == 4 ? 0 : AppSpacing.stackSm,
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
        Align(
          alignment: Alignment.centerLeft,
          child: Text(
            'Paso $step de 4 · $stageLabel',
            style: AppTypography.labelSm.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
        ),
      ],
    );
  }
}

/// Titulo + descripcion de cada paso (titulo en `headline-sm`/`primary`,
/// descripcion en `body-md`/`secondary-text`, docs/20 §4).
class PinStepHeading extends StatelessWidget {
  const PinStepHeading({
    super.key,
    required this.title,
    required this.subtitle,
  });

  final String title;
  final String subtitle;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          title,
          style: AppTypography.headlineSm.copyWith(
            color: AppColors.primary,
          ),
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

/// Fila de [kPinLength] casillas (fig `0:497`: 46x56, radio 10, borde
/// `outline-variant`, foco 2px `primary`).
///
/// Con [obscure] muestra un punto por digito (PIN sensible, enmascarado por
/// defecto, docs/20 §8); sin el, muestra el digito (OTP efimero).
class PinCodeBoxes extends StatelessWidget {
  const PinCodeBoxes({
    super.key,
    required this.value,
    this.obscure = true,
    this.length = kPinLength,
  });

  final String value;
  final bool obscure;
  final int length;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        for (var i = 0; i < length; i++)
          Padding(
            padding: EdgeInsets.only(
              right: i == length - 1 ? 0 : kPinBoxGap,
            ),
            child: _PinBox(
              filled: i < value.length,
              focused: i == value.length,
              digit: i < value.length ? value[i] : '',
              obscure: obscure,
            ),
          ),
      ],
    );
  }
}

class _PinBox extends StatelessWidget {
  const _PinBox({
    required this.filled,
    required this.focused,
    required this.digit,
    required this.obscure,
  });

  final bool filled;
  final bool focused;
  final String digit;
  final bool obscure;

  @override
  Widget build(BuildContext context) {
    final child = !filled
        ? const SizedBox.shrink()
        : obscure
            ? Container(
                width: kPinDotDiameter,
                height: kPinDotDiameter,
                decoration: const BoxDecoration(
                  color: AppColors.primary,
                  shape: BoxShape.circle,
                ),
              )
            : Text(
                digit,
                style: AppTypography.headlineSm.copyWith(
                  color: AppColors.primary,
                ),
              );
    return Container(
      width: kPinBoxWidth,
      height: kPinBoxHeight,
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLowest,
        borderRadius: BorderRadius.circular(AppRadii.md - AppSpacing.unit),
        border: Border.all(
          color: focused ? AppColors.primary : AppColors.outlineVariant,
          width: focused ? 2 : 1,
        ),
        boxShadow: AppShadows.cardList,
      ),
      child: Center(child: child),
    );
  }
}

/// Teclado numerico propio anclado abajo (fig `0:497`/`0:604`: teclas de
/// 66px de alto, radio 16).
///
/// Evita el teclado del SO: el PIN/OTP nunca pasa por autofill ni
/// sugerencias (docs/16 reglas 7 y 10). Objetivos tactiles >= 44px
/// (docs/20 §8).
class PinKeypad extends StatelessWidget {
  const PinKeypad({
    super.key,
    required this.onDigit,
    required this.onBackspace,
    this.enabled = true,
  });

  final ValueChanged<String> onDigit;
  final VoidCallback onBackspace;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        for (final row in const [
          ['1', '2', '3'],
          ['4', '5', '6'],
          ['7', '8', '9'],
        ])
          Padding(
            padding: const EdgeInsets.only(bottom: AppSpacing.stackSm),
            child: Row(
              children: [
                for (final digit in row)
                  Expanded(
                    child: Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: AppSpacing.stackXs,
                      ),
                      child: _PinKey(
                        key: Key('pin-key-$digit'),
                        label: digit,
                        enabled: enabled,
                        onTap: () => onDigit(digit),
                      ),
                    ),
                  ),
              ],
            ),
          ),
        Row(
          children: [
            const Expanded(child: SizedBox.shrink()),
            Expanded(
              child: Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: AppSpacing.stackXs,
                ),
                child: _PinKey(
                  key: const Key('pin-key-0'),
                  label: '0',
                  enabled: enabled,
                  onTap: () => onDigit('0'),
                ),
              ),
            ),
            Expanded(
              child: Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: AppSpacing.stackXs,
                ),
                child: SizedBox(
                  height: kPinKeyHeight,
                  child: IconButton(
                    key: const Key('pin-key-back'),
                    onPressed: enabled ? onBackspace : null,
                    icon: const Icon(Icons.backspace_outlined),
                    color: AppColors.primary,
                    iconSize: AppSpacing.stackLg,
                    tooltip: 'Borrar',
                  ),
                ),
              ),
            ),
          ],
        ),
      ],
    );
  }
}

class _PinKey extends StatelessWidget {
  const _PinKey({
    super.key,
    required this.label,
    required this.enabled,
    required this.onTap,
  });

  final String label;
  final bool enabled;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: kPinKeyHeight,
      child: FilledButton.tonal(
        onPressed: enabled ? onTap : null,
        style: FilledButton.styleFrom(
          backgroundColor: AppColors.surfaceContainerLow,
          foregroundColor: AppColors.primary,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(kPinKeyRadius),
          ),
          textStyle: AppTypography.headlineSm,
        ),
        child: Text(label),
      ),
    );
  }
}
