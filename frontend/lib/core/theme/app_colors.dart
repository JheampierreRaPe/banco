import 'package:flutter/material.dart';

/// Tokens de color del design system "Eucalipto y Ocre" (`docs/20` §3).
///
/// Fuente canonica: el YAML de `docs/design/design.md`. Donde la prosa y el
/// YAML difieren, manda el YAML (`docs/20` §2).
///
/// Presentacion pura: constantes en memoria, sin logica ni persistencia.
class AppColors {
  AppColors._();

  // ---------------------------------------------------------------------------
  // Base y superficies (docs/20 §3.1).
  // ---------------------------------------------------------------------------
  static const Color surface = Color(0xFFFBF9F4);
  static const Color surfaceDim = Color(0xFFDCDAD5);
  static const Color surfaceBright = Color(0xFFFBF9F4);
  static const Color surfaceContainerLowest = Color(0xFFFFFFFF);
  static const Color surfaceContainerLow = Color(0xFFF5F3EE);
  static const Color surfaceContainer = Color(0xFFF0EEE9);
  static const Color surfaceContainerHigh = Color(0xFFEAE8E3);
  static const Color surfaceContainerHighest = Color(0xFFE4E2DD);
  static const Color onSurface = Color(0xFF1B1C19);
  static const Color onSurfaceVariant = Color(0xFF424844);
  static const Color outline = Color(0xFF737873);
  static const Color outlineVariant = Color(0xFFC2C8C2);
  static const Color surfaceTint = Color(0xFF4D6356);
  static const Color inverseSurface = Color(0xFF30312E);
  static const Color inverseOnSurface = Color(0xFFF2F1EC);

  // ---------------------------------------------------------------------------
  // Primario, secundario y terciario (docs/20 §3.2).
  // ---------------------------------------------------------------------------
  static const Color primary = Color(0xFF152A1F);
  static const Color onPrimary = Color(0xFFFFFFFF);
  static const Color primaryContainer = Color(0xFF2B4034);
  static const Color onPrimaryContainer = Color(0xFF94AB9C);
  static const Color inversePrimary = Color(0xFFB4CCBC);

  static const Color secondary = Color(0xFF895200);
  static const Color onSecondary = Color(0xFFFFFFFF);
  static const Color secondaryContainer = Color(0xFFFDAA47);
  static const Color onSecondaryContainer = Color(0xFF6E4100);

  static const Color tertiary = Color(0xFF381F20);
  static const Color onTertiary = Color(0xFFFFFFFF);
  static const Color tertiaryContainer = Color(0xFF503435);
  static const Color onTertiaryContainer = Color(0xFFC39C9D);

  // ---------------------------------------------------------------------------
  // Fijos (docs/20 §3.3).
  // ---------------------------------------------------------------------------
  static const Color primaryFixed = Color(0xFFD0E8D7);
  static const Color primaryFixedDim = Color(0xFFB4CCBC);
  static const Color onPrimaryFixed = Color(0xFF0B1F15);
  static const Color onPrimaryFixedVariant = Color(0xFF364B3F);

  static const Color secondaryFixed = Color(0xFFFFDCBC);
  static const Color secondaryFixedDim = Color(0xFFFFB86A);
  static const Color onSecondaryFixed = Color(0xFF2C1700);
  static const Color onSecondaryFixedVariant = Color(0xFF683D00);

  static const Color tertiaryFixed = Color(0xFFFFDADA);
  static const Color tertiaryFixedDim = Color(0xFFE6BDBD);
  static const Color onTertiaryFixed = Color(0xFF2C1516);
  static const Color onTertiaryFixedVariant = Color(0xFF5D3F40);

  // ---------------------------------------------------------------------------
  // Estados y extras (docs/20 §3.4).
  // ---------------------------------------------------------------------------
  static const Color error = Color(0xFFBA1A1A);
  static const Color onError = Color(0xFFFFFFFF);
  static const Color errorContainer = Color(0xFFFFDAD6);
  static const Color onErrorContainer = Color(0xFF93000A);

  /// Error critico (token extra; en el `ColorScheme` vive `error`).
  static const Color errorCarmine = Color(0xFFB31D3F);

  /// Alias de [surface] (docs/20 §9.1).
  static const Color background = surface;

  /// Alias de [onSurface] (docs/20 §9.1).
  static const Color onBackground = onSurface;

  /// Alias de [surfaceContainerHighest] (docs/20 §2: `divider = #E4E2DD`).
  static const Color surfaceVariant = surfaceContainerHighest;

  /// Texto de acento (ocre oscuro).
  static const Color accentText = Color(0xFF9B5F12);

  /// Metadatos / texto atenuado.
  static const Color secondaryText = Color(0xFF6B7268);

  /// Divisores (`surface-container-highest`).
  static const Color divider = Color(0xFFE4E2DD);

  static const Color success = Color(0xFF2F6B4A);
  static const Color onSuccess = Color(0xFFFFFFFF);
  static const Color successContainer = Color(0xFFD0E8D7);
  static const Color onSuccessContainer = Color(0xFF0B1F15);

  static const Color warning = Color(0xFFB26A00);
  static const Color onWarning = Color(0xFFFFFFFF);
  static const Color warningContainer = Color(0xFFFFDCBC);
  static const Color onWarningContainer = Color(0xFF683D00);
}
