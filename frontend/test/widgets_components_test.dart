import 'package:banca_online/core/theme/app_colors.dart';
import 'package:banca_online/core/theme/app_theme.dart';
import 'package:banca_online/core/widgets/app_button.dart';
import 'package:banca_online/core/widgets/app_card.dart';
import 'package:banca_online/core/widgets/app_list_item.dart';
import 'package:banca_online/core/widgets/app_status_chip.dart';
import 'package:banca_online/core/widgets/app_text_field.dart';
import 'package:banca_online/core/widgets/empty_view.dart';
import 'package:banca_online/core/widgets/error_view.dart';
import 'package:banca_online/core/widgets/loading_view.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

Future<void> pumpThemed(WidgetTester tester, Widget child) {
  return tester.pumpWidget(
    MaterialApp(
      theme: AppTheme.light,
      home: Scaffold(body: child),
    ),
  );
}

void main() {
  group('botones base (docs/20 §6)', () {
    testWidgets('primario: alto 52, radio 12, fondo primary', (tester) async {
      var pressed = false;
      await pumpThemed(
        tester,
        SizedBox(
          width: 320,
          child: AppPrimaryButton(
            key: const Key('btnPrimary'),
            label: 'Continuar',
            onPressed: () => pressed = true,
          ),
        ),
      );

      expect(find.byKey(const Key('btnPrimary')), findsOneWidget);
      expect(find.text('Continuar'), findsOneWidget);
      final size = tester.getSize(find.byKey(const Key('btnPrimary')));
      expect(size.height, greaterThanOrEqualTo(52));

      final button =
          tester.widget<ElevatedButton>(find.byType(ElevatedButton));
      final style = button.style!;
      final bg = style.backgroundColor!.resolve({});
      expect(bg, AppColors.primary);

      await tester.tap(find.text('Continuar'));
      await tester.pump();
      expect(pressed, isTrue);
    });

    testWidgets('primario deshabilitado y cargando no disparan', (
      tester,
    ) async {
      await pumpThemed(
        tester,
        Column(
          children: [
            AppPrimaryButton(
              key: const Key('btnDisabled'),
              label: 'Off',
              onPressed: null,
            ),
            AppPrimaryButton(
              key: const Key('btnLoading'),
              label: 'Va',
              onPressed: () {},
              loading: true,
            ),
          ],
        ),
      );
      expect(
        tester
            .widget<ElevatedButton>(
              find.descendant(
                of: find.byKey(const Key('btnDisabled')),
                matching: find.byType(ElevatedButton),
              ),
            )
            .enabled,
        isFalse,
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      await tester.tap(find.byKey(const Key('btnLoading')));
      await tester.pump();
      // Sigue cargando: no navega ni falla.
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
    });

    testWidgets('secundario: borde 1.5 primary, texto primary', (tester) async {
      var pressed = false;
      await pumpThemed(
        tester,
        SizedBox(
          width: 320,
          child: AppSecondaryButton(
            key: const Key('btnSecondary'),
            label: 'Atras',
            onPressed: () => pressed = true,
          ),
        ),
      );
      final size = tester.getSize(find.byKey(const Key('btnSecondary')));
      expect(size.height, greaterThanOrEqualTo(52));
      final button =
          tester.widget<OutlinedButton>(find.byType(OutlinedButton));
      final side = button.style!.side!.resolve({})!;
      expect(side.color, AppColors.primary);
      expect(side.width, 1.5);
      expect(button.style!.foregroundColor!.resolve({}), AppColors.primary);
      await tester.tap(find.text('Atras'));
      await tester.pump();
      expect(pressed, isTrue);
    });

    testWidgets('ghost: texto primary sin fondo', (tester) async {
      var pressed = false;
      await pumpThemed(
        tester,
        AppGhostButton(
          key: const Key('btnGhost'),
          label: 'Omitir',
          onPressed: () => pressed = true,
        ),
      );
      final button = tester.widget<TextButton>(find.byType(TextButton));
      expect(button.style!.foregroundColor!.resolve({}), AppColors.primary);
      expect(button.style!.backgroundColor?.resolve({}), isNull);
      await tester.tap(find.text('Omitir'));
      await tester.pump();
      expect(pressed, isTrue);
    });
  });

  group('input base (docs/20 §6)', () {
    testWidgets('label persistente, fondo y bordes del token', (tester) async {
      final controller = TextEditingController();
      var changed = '';
      await pumpThemed(
        tester,
        AppTextField(
          key: const Key('inputDoc'),
          label: 'Documento',
          hintText: 'DNI',
          controller: controller,
          onChanged: (value) => changed = value,
        ),
      );

      expect(find.text('Documento'), findsOneWidget);
      final label = tester.widget<Text>(find.text('Documento'));
      expect(label.style!.color, AppColors.primary);
      expect(label.style!.fontSize, 14);

      final field = tester.widget<TextField>(find.byType(TextField));
      final decoration = field.decoration!;
      expect(decoration.fillColor, AppColors.surfaceContainerLowest);
      final enabled = decoration.enabledBorder! as OutlineInputBorder;
      expect(enabled.borderSide.color, AppColors.outlineVariant);
      expect(enabled.borderSide.width, 1);
      final focused = decoration.focusedBorder! as OutlineInputBorder;
      expect(focused.borderSide.color, AppColors.primary);
      expect(focused.borderSide.width, 2);

      await tester.enterText(find.byType(TextField), '12345678');
      await tester.pump();
      expect(changed, '12345678');
      controller.dispose();
    });

    testWidgets('muestra error del servidor sin validar nada local', (
      tester,
    ) async {
      await pumpThemed(
        tester,
        const AppTextField(
          key: Key('inputErr'),
          label: 'Monto',
          errorText: 'Fondos insuficientes',
        ),
      );
      expect(find.text('Fondos insuficientes'), findsOneWidget);
    });
  });

  group('card base (docs/20 §6)', () {
    testWidgets('fondo lowest, radio 16 y sombra card', (tester) async {
      await pumpThemed(
        tester,
        const AppCard(
          key: Key('cardMain'),
          child: Text('Saldo'),
        ),
      );
      expect(find.byKey(const Key('cardMain')), findsOneWidget);
      final ink = tester.widget<Ink>(
        find.descendant(
          of: find.byKey(const Key('cardMain')),
          matching: find.byType(Ink),
        ),
      );
      final decoration = ink.decoration! as BoxDecoration;
      expect(decoration.color, AppColors.surfaceContainerLowest);
      final radius = decoration.borderRadius! as BorderRadius;
      expect(radius.topLeft.x, 16);
      expect(decoration.boxShadow, isNotEmpty);
      expect(decoration.boxShadow!.first.blurRadius, 12);
    });

    testWidgets('con onTap responde al toque', (tester) async {
      var tapped = false;
      await pumpThemed(
        tester,
        AppCard(
          key: const Key('cardTap'),
          onTap: () => tapped = true,
          child: const Text('Ver'),
        ),
      );
      await tester.tap(find.text('Ver'));
      await tester.pump();
      expect(tapped, isTrue);
    });
  });

  group('chips de estado (docs/20 §6)', () {
    testWidgets('fondos y textos del estado + pill', (tester) async {
      await pumpThemed(
        tester,
        const Column(
          children: [
            AppStatusChip.success(key: Key('chipOk'), label: 'Aprobado'),
            AppStatusChip.warning(key: Key('chipWarn'), label: 'En revisión'),
            AppStatusChip.error(key: Key('chipErr'), label: 'Rechazado'),
            AppStatusChip.info(key: Key('chipInfo'), label: 'S/ 1,250.00'),
          ],
        ),
      );

      Color bgOf(Key key) {
        final container = tester.widget<Container>(
          find.descendant(of: find.byKey(key), matching: find.byType(Container)),
        );
        return (container.decoration! as BoxDecoration).color!;
      }

      expect(bgOf(const Key('chipOk')), AppColors.successContainer);
      expect(bgOf(const Key('chipWarn')), AppColors.warningContainer);
      expect(bgOf(const Key('chipErr')), AppColors.errorContainer);
      expect(bgOf(const Key('chipInfo')), AppColors.secondaryContainer);

      Text textOf(String label) => tester.widget<Text>(find.text(label));
      expect(textOf('Aprobado').style!.color, AppColors.onSuccessContainer);
      expect(
        textOf('En revisión').style!.color,
        AppColors.onWarningContainer,
      );
      expect(textOf('Rechazado').style!.color, AppColors.onErrorContainer);
      expect(
        textOf('S/ 1,250.00').style!.color,
        AppColors.onSecondaryContainer,
      );
    });
  });

  group('list item (docs/20 §6)', () {
    testWidgets('alto minimo 64, divisor y circulo del icono', (tester) async {
      await pumpThemed(
        tester,
        AppListItem(
          key: const Key('itemCuenta'),
          title: 'Cuenta Sueldo',
          subtitle: '**** 1234',
          leadingIcon: Icons.account_balance_outlined,
          trailing: const Icon(Icons.chevron_right),
          onTap: () {},
        ),
      );

      final size = tester.getSize(find.byKey(const Key('itemCuenta')));
      expect(size.height, greaterThanOrEqualTo(64));
      expect(find.text('Cuenta Sueldo'), findsOneWidget);
      expect(find.text('**** 1234'), findsOneWidget);

      final subtitle = tester.widget<Text>(find.text('**** 1234'));
      expect(subtitle.style!.color, AppColors.secondaryText);
      final circleFinder = find.byWidgetPredicate(
        (widget) =>
            widget is Container &&
            widget.decoration is BoxDecoration &&
            (widget.decoration! as BoxDecoration).shape == BoxShape.circle,
      );
      expect(circleFinder, findsOneWidget);
      // Regresion CA-03 (F-T34): el diametro del circulo sale del token
      // nombrado (sin `40` hardcodeado) y el icono interior sigue en 24.
      expect(AppListItem.leadingCircleDiameter, 40);
      final circleSize = tester.getSize(circleFinder);
      expect(circleSize.width, AppListItem.leadingCircleDiameter);
      expect(circleSize.height, AppListItem.leadingCircleDiameter);
      final icon = tester.widget<Icon>(
        find.byIcon(Icons.account_balance_outlined),
      );
      expect(icon.size, 24);
    });
  });

  group('estados obligatorios con el tema nuevo (docs/20 §7)', () {
    testWidgets('loading, empty y error renderizan bajo AppTheme.light', (
      tester,
    ) async {
      await pumpThemed(
        tester,
        const Column(
          children: [
            LoadingView(message: 'Cargando cuentas...'),
            EmptyView(message: 'Sin movimientos.'),
            ErrorView(message: 'Sin conexión.'),
          ],
        ),
      );
      expect(find.text('Cargando cuentas...'), findsOneWidget);
      expect(find.text('Sin movimientos.'), findsOneWidget);
      expect(find.text('Sin conexión.'), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
    });
  });
}
