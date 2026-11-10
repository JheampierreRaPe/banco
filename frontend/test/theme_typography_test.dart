import 'package:banca_online/core/theme/app_typography.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('escala tipografica Inter (docs/20 §4)', () {
    test('toda la escala usa fontFamily Inter', () {
      const styles = [
        AppTypography.displayLg,
        AppTypography.headlineLgMobile,
        AppTypography.headlineMd,
        AppTypography.headlineSm,
        AppTypography.titleMd,
        AppTypography.bodyLg,
        AppTypography.bodyMd,
        AppTypography.labelMd,
        AppTypography.labelSm,
      ];
      for (final style in styles) {
        expect(style.fontFamily, 'Inter', reason: '$style');
      }
      final textTheme = AppTypography.buildTextTheme();
      final slots = [
        textTheme.displayLarge,
        textTheme.displayMedium,
        textTheme.displaySmall,
        textTheme.headlineLarge,
        textTheme.headlineMedium,
        textTheme.headlineSmall,
        textTheme.titleLarge,
        textTheme.titleMedium,
        textTheme.titleSmall,
        textTheme.bodyLarge,
        textTheme.bodyMedium,
        textTheme.bodySmall,
        textTheme.labelLarge,
        textTheme.labelMedium,
        textTheme.labelSmall,
      ];
      for (final slot in slots) {
        expect(slot!.fontFamily, 'Inter');
      }
    });

    test('tamanos canonicos de la escala', () {
      expect(AppTypography.displayLg.fontSize, 36);
      expect(AppTypography.headlineLgMobile.fontSize, 30);
      expect(AppTypography.headlineMd.fontSize, 28);
      expect(AppTypography.headlineSm.fontSize, 24);
      expect(AppTypography.titleMd.fontSize, 20);
      expect(AppTypography.bodyLg.fontSize, 16);
      expect(AppTypography.bodyMd.fontSize, 14);
      expect(AppTypography.labelMd.fontSize, 14);
      expect(AppTypography.labelSm.fontSize, 12);
    });

    test('pesos canonicos de la escala', () {
      expect(AppTypography.displayLg.fontWeight, FontWeight.w700);
      expect(AppTypography.headlineLgMobile.fontWeight, FontWeight.w700);
      expect(AppTypography.headlineMd.fontWeight, FontWeight.w700);
      expect(AppTypography.headlineSm.fontWeight, FontWeight.w600);
      expect(AppTypography.titleMd.fontWeight, FontWeight.w600);
      expect(AppTypography.bodyLg.fontWeight, FontWeight.w400);
      expect(AppTypography.bodyMd.fontWeight, FontWeight.w400);
      expect(AppTypography.labelMd.fontWeight, FontWeight.w600);
      expect(AppTypography.labelSm.fontWeight, FontWeight.w500);
    });

    test('line-height canonicos (height = lh / size)', () {
      expect(AppTypography.displayLg.height, closeTo(44 / 36, 0.0001));
      expect(AppTypography.headlineLgMobile.height, closeTo(38 / 30, 0.0001));
      expect(AppTypography.headlineMd.height, closeTo(36 / 28, 0.0001));
      expect(AppTypography.headlineSm.height, closeTo(32 / 24, 0.0001));
      expect(AppTypography.titleMd.height, closeTo(28 / 20, 0.0001));
      expect(AppTypography.bodyLg.height, closeTo(24 / 16, 0.0001));
      expect(AppTypography.bodyMd.height, closeTo(20 / 14, 0.0001));
      expect(AppTypography.labelMd.height, closeTo(20 / 14, 0.0001));
      expect(AppTypography.labelSm.height, closeTo(16 / 12, 0.0001));
    });

    test('mapeo de tokens al TextTheme Material 3', () {
      final textTheme = AppTypography.buildTextTheme();
      expect(textTheme.displayLarge!.fontSize, 36);
      expect(textTheme.headlineLarge!.fontSize, 30);
      expect(textTheme.headlineMedium!.fontSize, 28);
      expect(textTheme.headlineSmall!.fontSize, 24);
      expect(textTheme.titleMedium!.fontSize, 20);
      expect(textTheme.bodyLarge!.fontSize, 16);
      expect(textTheme.bodyMedium!.fontSize, 14);
      expect(textTheme.labelLarge!.fontSize, 14);
      expect(textTheme.labelSmall!.fontSize, 12);
    });
  });
}
