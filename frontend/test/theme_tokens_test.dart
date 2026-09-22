import 'package:banca_online/core/theme/app_colors.dart';
import 'package:banca_online/core/theme/app_theme.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('tokens de color (docs/20 §3, manda el YAML)', () {
    test('primario eucalipto y ocre con los hex canonicos', () {
      expect(AppColors.primary, const Color(0xFF152A1F));
      expect(AppColors.onPrimary, const Color(0xFFFFFFFF));
      expect(AppColors.primaryContainer, const Color(0xFF2B4034));
      expect(AppColors.secondary, const Color(0xFF895200));
      expect(AppColors.secondaryContainer, const Color(0xFFFDAA47));
      expect(AppColors.onSecondaryContainer, const Color(0xFF6E4100));
    });

    test('superficies y texto con los hex canonicos', () {
      expect(AppColors.surface, const Color(0xFFFBF9F4));
      expect(AppColors.surfaceContainerLowest, const Color(0xFFFFFFFF));
      expect(AppColors.surfaceContainerLow, const Color(0xFFF5F3EE));
      expect(AppColors.onSurface, const Color(0xFF1B1C19));
      expect(AppColors.onSurfaceVariant, const Color(0xFF424844));
      expect(AppColors.outline, const Color(0xFF737873));
      expect(AppColors.outlineVariant, const Color(0xFFC2C8C2));
    });

    test('error y extras con los hex canonicos', () {
      expect(AppColors.error, const Color(0xFFBA1A1A));
      expect(AppColors.errorContainer, const Color(0xFFFFDAD6));
      expect(AppColors.success, const Color(0xFF2F6B4A));
      expect(AppColors.successContainer, const Color(0xFFD0E8D7));
      expect(AppColors.warning, const Color(0xFFB26A00));
      expect(AppColors.warningContainer, const Color(0xFFFFDCBC));
      expect(AppColors.accentText, const Color(0xFF9B5F12));
      expect(AppColors.secondaryText, const Color(0xFF6B7268));
      expect(AppColors.errorCarmine, const Color(0xFFB31D3F));
      expect(AppColors.divider, const Color(0xFFE4E2DD));
    });

    test('background/onBackground son alias de surface/onSurface', () {
      expect(AppColors.background, AppColors.surface);
      expect(AppColors.onBackground, AppColors.onSurface);
    });
  });

  group('mapeo token -> ColorScheme (docs/20 §9.1)', () {
    test('roles principales del esquema claro', () {
      final scheme = AppTheme.lightScheme;
      expect(scheme.brightness, Brightness.light);
      expect(scheme.primary, AppColors.primary);
      expect(scheme.onPrimary, AppColors.onPrimary);
      expect(scheme.primaryContainer, AppColors.primaryContainer);
      expect(scheme.secondary, AppColors.secondary);
      expect(scheme.onSecondary, AppColors.onSecondary);
      expect(scheme.secondaryContainer, AppColors.secondaryContainer);
      expect(
        scheme.onSecondaryContainer,
        AppColors.onSecondaryContainer,
      );
      expect(scheme.surface, AppColors.surface);
      expect(scheme.onSurface, AppColors.onSurface);
      expect(scheme.error, AppColors.error);
      expect(scheme.onError, AppColors.onError);
      expect(scheme.errorContainer, AppColors.errorContainer);
      expect(scheme.surfaceContainerLowest, AppColors.surfaceContainerLowest);
      expect(scheme.surfaceContainerLow, AppColors.surfaceContainerLow);
      expect(scheme.outline, AppColors.outline);
      expect(scheme.outlineVariant, AppColors.outlineVariant);
    });

    test('el seed azul 0xFF0B3D91 desaparecio del tema', () {
      const seedAzul = Color(0xFF0B3D91);
      expect(AppTheme.lightScheme.primary, isNot(seedAzul));
      expect(AppTheme.lightScheme.secondary, isNot(seedAzul));
      expect(AppTheme.light.colorScheme.primary, AppColors.primary);
    });

    test('extras por ThemeExtension con los hex canonicos', () {
      final theme = AppTheme.light;
      final extras = theme.extension<AppExtraColors>()!;
      expect(extras.success, const Color(0xFF2F6B4A));
      expect(extras.onSuccessContainer, const Color(0xFF0B1F15));
      expect(extras.warning, const Color(0xFFB26A00));
      expect(extras.onWarningContainer, const Color(0xFF683D00));
      expect(extras.accentText, const Color(0xFF9B5F12));
      expect(extras.secondaryText, const Color(0xFF6B7268));
      expect(extras.errorCarmine, const Color(0xFFB31D3F));
      expect(extras.divider, const Color(0xFFE4E2DD));
    });

    test('ThemeExtension copyWith/lerp no rompen', () {
      const base = AppExtraColors.light;
      expect(base.copyWith().success, base.success);
      expect(
        base.copyWith(success: const Color(0xFF000000)).success,
        const Color(0xFF000000),
      );
      final mid = base.lerp(base, 0.5);
      expect(mid.success, base.success);
    });
  });
}
