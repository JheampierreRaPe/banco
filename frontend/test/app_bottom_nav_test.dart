import 'package:banca_online/core/theme/app_colors.dart';
import 'package:banca_online/core/widgets/app_bottom_nav.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// F-T55: `AppBottomNav` presentacional (sin `GoRouter`).
Widget _harness(AppBottomNav nav) =>
    MaterialApp(home: Scaffold(body: Column(children: [Expanded(child: Container()), nav])));

void main() {
  testWidgets('renderiza los 4 destinos con sus Keys', (tester) async {
    await tester.pumpWidget(_harness(AppBottomNav(onSelect: (_) {})));

    for (final label in AppBottomNav.labels) {
      expect(find.text(label), findsOneWidget);
    }
    expect(find.byKey(const Key('bottomNav')), findsOneWidget);
    expect(find.byKey(const Key('bottomNav-inicio')), findsOneWidget);
    expect(find.byKey(const Key('bottomNav-operar')), findsOneWidget);
    expect(find.byKey(const Key('bottomNav-tarjetas')), findsOneWidget);
    expect(find.byKey(const Key('bottomNav-perfil')), findsOneWidget);
  });

  testWidgets('activeIndex resalta el item activo', (tester) async {
    await tester.pumpWidget(
      _harness(AppBottomNav(activeIndex: 3, onSelect: (_) {})),
    );

    Icon iconFor(String label) {
      final item = tester.widget<InkWell>(
        find.byKey(Key('bottomNav-${label.toLowerCase() == 'tarjetas' ? 'tarjetas' : label.toLowerCase()}')),
      );
      // El icono vive dentro del InkWell del item.
      final icon = find.descendant(
        of: find.byWidget(item),
        matching: find.byType(Icon),
      );
      return tester.widget<Icon>(icon);
    }

    // Perfil activo en eucalipto; Inicio inactivo atenuado.
    expect(iconFor('Perfil').color, AppColors.primary);
    expect(iconFor('Inicio').color, AppColors.secondaryText);

    Text textFor(String label) => tester.widget<Text>(find.text(label));
    expect(textFor('Perfil').style?.color, AppColors.primary);
    expect(textFor('Inicio').style?.color, AppColors.secondaryText);
  });

  testWidgets('tap en cada item emite el index por onSelect', (tester) async {
    final selected = <int>[];
    await tester.pumpWidget(
      _harness(AppBottomNav(onSelect: selected.add)),
    );

    const keys = [
      'bottomNav-inicio',
      'bottomNav-operar',
      'bottomNav-tarjetas',
      'bottomNav-perfil',
    ];
    for (var i = 0; i < keys.length; i++) {
      await tester.tap(find.byKey(Key(keys[i])));
      await tester.pump();
    }
    expect(selected, [0, 1, 2, 3]);
  });

  testWidgets('no requiere GoRouter (MaterialApp simple)', (tester) async {
    var tapped = -1;
    await tester.pumpWidget(
      MaterialApp(home: Scaffold(bottomNavigationBar: AppBottomNav(onSelect: (i) => tapped = i))),
    );

    await tester.tap(find.byKey(const Key('bottomNav-perfil')));
    await tester.pump();
    expect(tapped, 3);
  });
}
