import 'package:banca_online/core/theme/app_spacing.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('espaciado, radios y sombra (docs/20 §5)', () {
    test('ritmo base de 4px y stack xs/sm/md/lg/xl', () {
      expect(AppSpacing.unit, 4);
      expect(AppSpacing.stackXs, 4);
      expect(AppSpacing.stackSm, 8);
      expect(AppSpacing.stackMd, 16);
      expect(AppSpacing.stackLg, 24);
      expect(AppSpacing.stackXl, 48);
      for (final value in [
        AppSpacing.unit,
        AppSpacing.marginMobile,
        AppSpacing.marginDesktop,
        AppSpacing.gutter,
        AppSpacing.containerPadding,
        AppSpacing.stackXs,
        AppSpacing.stackSm,
        AppSpacing.stackMd,
        AppSpacing.stackLg,
        AppSpacing.stackXl,
      ]) {
        expect(value % AppSpacing.unit, 0, reason: '$value');
      }
    });

    test('margenes, gutter y container-padding', () {
      expect(AppSpacing.marginMobile, 16);
      expect(AppSpacing.marginDesktop, 24);
      expect(AppSpacing.gutter, 16);
      expect(AppSpacing.containerPadding, 20);
    });

    test('radios: boton/input 12, card 16', () {
      expect(AppRadii.sm, 4);
      expect(AppRadii.def, 8);
      expect(AppRadii.md, 12);
      expect(AppRadii.lg, 16);
      expect(AppRadii.xl, 24);
      expect(AppRadii.full, 9999);
    });

    test('sombra card: 0 2px 12px rgba(43,64,52,0.05)', () {
      final shadow = AppShadows.card;
      expect(shadow.offset.dx, 0);
      expect(shadow.offset.dy, 2);
      expect(shadow.blurRadius, 12);
      expect(shadow.color.r, closeTo(43 / 255, 0.005));
      expect(shadow.color.g, closeTo(64 / 255, 0.005));
      expect(shadow.color.b, closeTo(52 / 255, 0.005));
      expect(shadow.color.a, closeTo(0.05, 0.005));
    });
  });
}
