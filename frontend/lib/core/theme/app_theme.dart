import 'package:flutter/material.dart';

import 'app_colors.dart';
import 'app_spacing.dart';
import 'app_typography.dart';

/// Tokens extra que no existen como campo de [ColorScheme] (`docs/20` §9.1).
///
/// Se exponen por [ThemeExtension] y se leen con
/// `Theme.of(context).extension<AppExtraColors>()!`.
class AppExtraColors extends ThemeExtension<AppExtraColors> {
  const AppExtraColors({
    required this.accentText,
    required this.secondaryText,
    required this.errorCarmine,
    required this.divider,
    required this.success,
    required this.onSuccess,
    required this.successContainer,
    required this.onSuccessContainer,
    required this.warning,
    required this.onWarning,
    required this.warningContainer,
    required this.onWarningContainer,
  });

  /// Juego canonico en modo claro (`docs/20` §3.4).
  static const AppExtraColors light = AppExtraColors(
    accentText: AppColors.accentText,
    secondaryText: AppColors.secondaryText,
    errorCarmine: AppColors.errorCarmine,
    divider: AppColors.divider,
    success: AppColors.success,
    onSuccess: AppColors.onSuccess,
    successContainer: AppColors.successContainer,
    onSuccessContainer: AppColors.onSuccessContainer,
    warning: AppColors.warning,
    onWarning: AppColors.onWarning,
    warningContainer: AppColors.warningContainer,
    onWarningContainer: AppColors.onWarningContainer,
  );

  final Color accentText;
  final Color secondaryText;
  final Color errorCarmine;
  final Color divider;
  final Color success;
  final Color onSuccess;
  final Color successContainer;
  final Color onSuccessContainer;
  final Color warning;
  final Color onWarning;
  final Color warningContainer;
  final Color onWarningContainer;

  @override
  AppExtraColors copyWith({
    Color? accentText,
    Color? secondaryText,
    Color? errorCarmine,
    Color? divider,
    Color? success,
    Color? onSuccess,
    Color? successContainer,
    Color? onSuccessContainer,
    Color? warning,
    Color? onWarning,
    Color? warningContainer,
    Color? onWarningContainer,
  }) {
    return AppExtraColors(
      accentText: accentText ?? this.accentText,
      secondaryText: secondaryText ?? this.secondaryText,
      errorCarmine: errorCarmine ?? this.errorCarmine,
      divider: divider ?? this.divider,
      success: success ?? this.success,
      onSuccess: onSuccess ?? this.onSuccess,
      successContainer: successContainer ?? this.successContainer,
      onSuccessContainer: onSuccessContainer ?? this.onSuccessContainer,
      warning: warning ?? this.warning,
      onWarning: onWarning ?? this.onWarning,
      warningContainer: warningContainer ?? this.warningContainer,
      onWarningContainer: onWarningContainer ?? this.onWarningContainer,
    );
  }

  @override
  AppExtraColors lerp(ThemeExtension<AppExtraColors>? other, double t) {
    if (other is! AppExtraColors) return this;
    Color lerpColor(Color a, Color b) => Color.lerp(a, b, t)!;
    return AppExtraColors(
      accentText: lerpColor(accentText, other.accentText),
      secondaryText: lerpColor(secondaryText, other.secondaryText),
      errorCarmine: lerpColor(errorCarmine, other.errorCarmine),
      divider: lerpColor(divider, other.divider),
      success: lerpColor(success, other.success),
      onSuccess: lerpColor(onSuccess, other.onSuccess),
      successContainer: lerpColor(successContainer, other.successContainer),
      onSuccessContainer:
          lerpColor(onSuccessContainer, other.onSuccessContainer),
      warning: lerpColor(warning, other.warning),
      onWarning: lerpColor(onWarning, other.onWarning),
      warningContainer: lerpColor(warningContainer, other.warningContainer),
      onWarningContainer:
          lerpColor(onWarningContainer, other.onWarningContainer),
    );
  }
}

/// Tema "Eucalipto y Ocre" (Material 3, **solo modo claro**).
///
/// El seed azul institucional anterior se elimino: el [ColorScheme] se
/// construye directo con los tokens de `docs/20` §3. Presentacion pura.
class AppTheme {
  AppTheme._();

  /// `ColorScheme` canonico con los tokens directos (`docs/20` §9.1).
  static const ColorScheme lightScheme = ColorScheme(
    brightness: Brightness.light,
    primary: AppColors.primary,
    onPrimary: AppColors.onPrimary,
    primaryContainer: AppColors.primaryContainer,
    onPrimaryContainer: AppColors.onPrimaryContainer,
    primaryFixed: AppColors.primaryFixed,
    primaryFixedDim: AppColors.primaryFixedDim,
    onPrimaryFixed: AppColors.onPrimaryFixed,
    onPrimaryFixedVariant: AppColors.onPrimaryFixedVariant,
    secondary: AppColors.secondary,
    onSecondary: AppColors.onSecondary,
    secondaryContainer: AppColors.secondaryContainer,
    onSecondaryContainer: AppColors.onSecondaryContainer,
    secondaryFixed: AppColors.secondaryFixed,
    secondaryFixedDim: AppColors.secondaryFixedDim,
    onSecondaryFixed: AppColors.onSecondaryFixed,
    onSecondaryFixedVariant: AppColors.onSecondaryFixedVariant,
    tertiary: AppColors.tertiary,
    onTertiary: AppColors.onTertiary,
    tertiaryContainer: AppColors.tertiaryContainer,
    onTertiaryContainer: AppColors.onTertiaryContainer,
    tertiaryFixed: AppColors.tertiaryFixed,
    tertiaryFixedDim: AppColors.tertiaryFixedDim,
    onTertiaryFixed: AppColors.onTertiaryFixed,
    onTertiaryFixedVariant: AppColors.onTertiaryFixedVariant,
    error: AppColors.error,
    onError: AppColors.onError,
    errorContainer: AppColors.errorContainer,
    onErrorContainer: AppColors.onErrorContainer,
    surface: AppColors.surface,
    onSurface: AppColors.onSurface,
    surfaceDim: AppColors.surfaceDim,
    surfaceBright: AppColors.surfaceBright,
    surfaceContainerLowest: AppColors.surfaceContainerLowest,
    surfaceContainerLow: AppColors.surfaceContainerLow,
    surfaceContainer: AppColors.surfaceContainer,
    surfaceContainerHigh: AppColors.surfaceContainerHigh,
    surfaceContainerHighest: AppColors.surfaceContainerHighest,
    onSurfaceVariant: AppColors.onSurfaceVariant,
    outline: AppColors.outline,
    outlineVariant: AppColors.outlineVariant,
    inverseSurface: AppColors.inverseSurface,
    onInverseSurface: AppColors.inverseOnSurface,
    inversePrimary: AppColors.inversePrimary,
    surfaceTint: AppColors.surfaceTint,
  );

  /// Esquema oscuro derivado del claro (compatibilidad: la app fija
  /// `ThemeMode.light`; nunca usa el seed azul).
  static final ColorScheme darkScheme =
      lightScheme.copyWith(brightness: Brightness.dark);

  static ThemeData _build(ColorScheme scheme, AppExtraColors extras) {
    final labelMd = AppTypography.labelMd.copyWith(color: scheme.onPrimary);
    return ThemeData(
      useMaterial3: true,
      colorScheme: scheme,
      scaffoldBackgroundColor: scheme.surface,
      fontFamily: AppTypography.fontFamily,
      textTheme: AppTypography.buildTextTheme(),
      extensions: <ThemeExtension<dynamic>>[extras],
      appBarTheme: AppBarTheme(
        centerTitle: true,
        backgroundColor: scheme.surface,
        foregroundColor: scheme.primary,
        titleTextStyle:
            AppTypography.titleMd.copyWith(color: scheme.primary),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: scheme.primary,
          foregroundColor: scheme.onPrimary,
          minimumSize: const Size(88, 52),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(AppRadii.md),
          ),
          textStyle: labelMd,
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: scheme.primary,
          minimumSize: const Size(88, 52),
          side: BorderSide(color: scheme.primary, width: 1.5),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(AppRadii.md),
          ),
          textStyle: AppTypography.labelMd.copyWith(color: scheme.primary),
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: scheme.primary,
          minimumSize: const Size(44, 44),
          textStyle: AppTypography.labelMd.copyWith(color: scheme.primary),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: AppColors.surfaceContainerLowest,
        labelStyle: AppTypography.labelMd.copyWith(color: scheme.primary),
        hintStyle:
            AppTypography.bodyMd.copyWith(color: AppColors.secondaryText),
        errorStyle: AppTypography.labelSm.copyWith(color: scheme.error),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: AppSpacing.stackMd,
          vertical: AppSpacing.stackSm + AppSpacing.unit,
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(AppRadii.md),
          borderSide: const BorderSide(
            color: AppColors.outlineVariant,
          ),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(AppRadii.md),
          borderSide: BorderSide(color: scheme.primary, width: 2),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(AppRadii.md),
          borderSide: BorderSide(color: scheme.error),
        ),
        focusedErrorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(AppRadii.md),
          borderSide: BorderSide(color: scheme.error, width: 2),
        ),
      ),
      cardTheme: CardThemeData(
        color: AppColors.surfaceContainerLowest,
        margin: const EdgeInsets.all(12),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppRadii.lg),
        ),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: scheme.surfaceContainerLow,
        labelStyle: AppTypography.labelSm.copyWith(color: scheme.onSurface),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppRadii.full),
        ),
        side: BorderSide.none,
      ),
      listTileTheme: const ListTileThemeData(
        minTileHeight: 64,
      ),
      dividerTheme: const DividerThemeData(
        color: AppColors.divider,
        thickness: 1,
        space: 1,
      ),
      iconTheme: IconThemeData(color: scheme.primary, size: 24),
    );
  }

  static ThemeData get light => _build(lightScheme, AppExtraColors.light);

  /// Derivado del claro (la app opera en `ThemeMode.light`).
  static ThemeData get dark => _build(darkScheme, AppExtraColors.light);
}
