import 'package:flutter/material.dart';

import '../theme/app_colors.dart';
import '../theme/app_spacing.dart';
import '../theme/app_typography.dart';

/// Barra de navegacion inferior del `dash` (F-T55).
///
/// Componente **presentacional**: muestra los 4 destinos fijos
/// (Inicio / Operar / Tarjetas / Perfil), resalta el activo segun
/// [activeIndex] y notifica la seleccion por [onSelect].
///
/// No navega por si misma (no usa `go_router`): quien la integra
/// (`F-T54`) mapea `Perfil (3) -> /profile` y deja el resto sin accion
/// ("Proximamente"). Sin logica de negocio, sin red, sin logs.
class AppBottomNav extends StatelessWidget {
  const AppBottomNav({
    super.key,
    this.activeIndex = 0,
    required this.onSelect,
  }) : assert(activeIndex >= 0 && activeIndex < labels.length);

  /// Indice del destino activo (`0` = Inicio por defecto).
  final int activeIndex;

  /// Seam de seleccion: emite el indice tocado (`0..3`).
  final ValueChanged<int> onSelect;

  /// Labels fijos del mockup `dash` (nodo bottom-nav `0:3168`).
  static const List<String> labels = [
    'Inicio',
    'Operar',
    'Tarjetas',
    'Perfil',
  ];

  /// Iconos Material 24px (docs/20 §6): home / plus-circle /
  /// credit-card / user.
  static const List<IconData> icons = [
    Icons.home_outlined,
    Icons.add_circle_outline,
    Icons.credit_card_outlined,
    Icons.person_outline,
  ];

  /// Sufijos estables de `Key` por destino (para test/watch).
  static const List<String> keySuffixes = [
    'inicio',
    'operar',
    'tarjetas',
    'perfil',
  ];

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      top: false,
      child: Container(
        key: const Key('bottomNav'),
        decoration: const BoxDecoration(
          color: AppColors.surfaceContainerLowest,
          border: Border(
            top: BorderSide(color: AppColors.divider),
          ),
        ),
        padding: const EdgeInsets.symmetric(
          horizontal: AppSpacing.stackSm,
          vertical: AppSpacing.stackSm,
        ),
        child: Row(
          children: List.generate(labels.length, (index) {
            final selected = index == activeIndex;
            final color =
                selected ? AppColors.primary : AppColors.secondaryText;
            return Expanded(
              child: InkWell(
                key: Key('bottomNav-${keySuffixes[index]}'),
                onTap: () => onSelect(index),
                borderRadius: BorderRadius.circular(AppRadii.md),
                child: ConstrainedBox(
                  constraints: const BoxConstraints(minHeight: 48),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(
                      horizontal: AppSpacing.unit,
                      vertical: AppSpacing.unit,
                    ),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Icon(icons[index], size: 24, color: color),
                        const SizedBox(height: AppSpacing.unit),
                        Text(
                          labels[index],
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: AppTypography.labelSm.copyWith(color: color),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            );
          }),
        ),
      ),
    );
  }
}
