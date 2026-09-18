import 'package:flutter/material.dart';

/// Tema banca (Material 3) claro/oscuro.
///
/// Semilla azul institucional; superficies claras y contraste alto.
/// Sin dependencias externas: solo [ColorScheme.fromSeed].
class AppTheme {
  AppTheme._();

  static const _seed = Color(0xFF0B3D91);

  static ThemeData get light => ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: _seed,
          brightness: Brightness.light,
        ),
        appBarTheme: const AppBarTheme(centerTitle: true),
        cardTheme: const CardThemeData(
          margin: EdgeInsets.all(12),
        ),
      );

  static ThemeData get dark => ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: _seed,
          brightness: Brightness.dark,
        ),
        appBarTheme: const AppBarTheme(centerTitle: true),
        cardTheme: const CardThemeData(
          margin: EdgeInsets.all(12),
        ),
      );
}
