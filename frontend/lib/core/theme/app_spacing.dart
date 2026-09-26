import 'package:flutter/material.dart';

/// Espaciado, radios y elevacion del design system (`docs/20` §5).
///
/// Ritmo base de 4px. Presentacion pura: constantes en memoria.
class AppSpacing {
  AppSpacing._();

  /// Unidad base del ritmo (4px). Todo espaciado es multiplo de [unit].
  static const double unit = 4;

  /// Margen lateral en movil.
  static const double marginMobile = 16;

  /// Margen lateral en escritorio.
  static const double marginDesktop = 24;

  /// Gutter de la grilla.
  static const double gutter = 16;

  /// Padding interno de las cards.
  static const double containerPadding = 20;

  /// Ritmo vertical `stack`: xs/sm/md/lg/xl = 4/8/16/24/48.
  static const double stackXs = 4;
  static const double stackSm = 8;
  static const double stackMd = 16;
  static const double stackLg = 24;
  static const double stackXl = 48;
}

/// Radios de borde (`docs/20` §5: `rounded.*`).
///
/// El YAML usa `rem` (base 16px): sm 0.25rem = 4, DEFAULT 0.5rem = 8,
/// md 0.75rem = 12 (boton/input), lg 1rem = 16 (card), xl 1.5rem = 24,
/// full 9999 (pills). `docs/20` §2 confirma boton/input = 12 y card = 16.
class AppRadii {
  AppRadii._();

  static const double sm = 4;
  static const double def = 8;

  /// Radio de botones e inputs.
  static const double md = 12;

  /// Radio de cards.
  static const double lg = 16;

  static const double xl = 24;

  /// Pills (chips, tags de estado).
  static const double full = 9999;
}

/// Sombras del design system (`docs/20` §5).
class AppShadows {
  AppShadows._();

  /// `shadow.card = 0 2px 12px rgba(43,64,52,0.05)` (tinte de primary-container).
  static const BoxShadow card = BoxShadow(
    offset: Offset(0, 2),
    blurRadius: 12,
    color: Color(0x0D2B4034),
  );

  static List<BoxShadow> get cardList => const [card];
}
