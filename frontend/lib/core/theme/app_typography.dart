import 'package:flutter/material.dart';

/// Escala tipografica **Inter** del design system (`docs/20` §4).
///
/// Fuente canonica: el YAML de `docs/design/design.md` (manda sobre la prosa).
/// La familia `Inter` se empaqueta desde `frontend/assets/fonts/`
/// (400/500/600/700, declarada en `frontend/pubspec.yaml`).
///
/// Presentacion pura: estilos en memoria, sin logica ni persistencia.
class AppTypography {
  AppTypography._();

  static const String fontFamily = 'Inter';

  /// `display-lg`: 36/700, lh 44, ls -0.02em. Montos grandes.
  static const TextStyle displayLg = TextStyle(
    fontFamily: fontFamily,
    fontSize: 36,
    fontWeight: FontWeight.w700,
    height: 44 / 36,
    letterSpacing: 36 * -0.02,
  );

  /// `headline-lg-mobile`: 30/700, lh 38. Display en movil.
  static const TextStyle headlineLgMobile = TextStyle(
    fontFamily: fontFamily,
    fontSize: 30,
    fontWeight: FontWeight.w700,
    height: 38 / 30,
  );

  /// `headline-md`: 28/700, lh 36, ls -0.01em.
  static const TextStyle headlineMd = TextStyle(
    fontFamily: fontFamily,
    fontSize: 28,
    fontWeight: FontWeight.w700,
    height: 36 / 28,
    letterSpacing: 28 * -0.01,
  );

  /// `headline-sm`: 24/600, lh 32.
  static const TextStyle headlineSm = TextStyle(
    fontFamily: fontFamily,
    fontSize: 24,
    fontWeight: FontWeight.w600,
    height: 32 / 24,
  );

  /// `title-md`: 20/600, lh 28.
  static const TextStyle titleMd = TextStyle(
    fontFamily: fontFamily,
    fontSize: 20,
    fontWeight: FontWeight.w600,
    height: 28 / 20,
  );

  /// `body-lg`: 16/400, lh 24.
  static const TextStyle bodyLg = TextStyle(
    fontFamily: fontFamily,
    fontSize: 16,
    fontWeight: FontWeight.w400,
    height: 24 / 16,
  );

  /// `body-md`: 14/400, lh 20.
  static const TextStyle bodyMd = TextStyle(
    fontFamily: fontFamily,
    fontSize: 14,
    fontWeight: FontWeight.w400,
    height: 20 / 14,
  );

  /// `label-md`: 14/600, lh 20, ls 0.01em. Labels persistentes y botones.
  static const TextStyle labelMd = TextStyle(
    fontFamily: fontFamily,
    fontSize: 14,
    fontWeight: FontWeight.w600,
    height: 20 / 14,
    letterSpacing: 14 * 0.01,
  );

  /// `label-sm`: 12/500, lh 16. Chips y metadatos.
  static const TextStyle labelSm = TextStyle(
    fontFamily: fontFamily,
    fontSize: 12,
    fontWeight: FontWeight.w500,
    height: 16 / 12,
  );

  /// Mapeo de los 9 tokens canonicos a [TextTheme] Material 3.
  ///
  /// Los slots sin token propio reutilizan el token canonico mas cercano
  /// (documentado abajo); ningun tamano/peso se inventa fuera de la escala.
  static TextTheme buildTextTheme() {
    return const TextTheme(
      displayLarge: displayLg,
      // Sin token propio: el display movil mas cercano.
      displayMedium: headlineLgMobile,
      displaySmall: headlineMd,
      headlineLarge: headlineLgMobile,
      headlineMedium: headlineMd,
      headlineSmall: headlineSm,
      // Sin token de 22px: se reutiliza title-md.
      titleLarge: titleMd,
      titleMedium: titleMd,
      titleSmall: labelMd,
      bodyLarge: bodyLg,
      bodyMedium: bodyMd,
      // Sin token propio: se reutiliza body-md.
      bodySmall: bodyMd,
      labelLarge: labelMd,
      // Sin token propio: se reutiliza label-md.
      labelMedium: labelMd,
      labelSmall: labelSm,
    );
  }
}
