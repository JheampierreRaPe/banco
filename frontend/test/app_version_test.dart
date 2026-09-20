// Pruebas de la version visible de la app (F-T30).
//
// - Fuente unica: `kAppVersion` por defecto es `1.1.010` (texto visible).
// - Widget compartido: compone `Version <kAppVersion>` con `Key('app-version')`.
// - Montaje en `/entry`: la pantalla muestra el mismo widget/constante.
// - Fuente unica a nivel de codigo: las tres pantallas montan `AppVersionLabel`
//   y no contienen una version hardcodeada.
import 'dart:io';

import 'package:banca_online/core/app_version.dart';
import 'package:banca_online/core/widgets/app_version_label.dart';
import 'package:banca_online/features/welcome/entry_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('fuente unica de version', () {
    test('kAppVersion usa el default 1.1.010 (texto visible)', () {
      expect(kAppVersion, '1.1.010');
    });

    testWidgets('AppVersionLabel compone "Version <kAppVersion>"',
        (tester) async {
      await tester.pumpWidget(
        const MaterialApp(home: Scaffold(body: AppVersionLabel())),
      );

      expect(find.byType(AppVersionLabel), findsOneWidget);
      expect(find.byKey(const Key('app-version')), findsOneWidget);
      expect(find.text('Version $kAppVersion'), findsOneWidget);
    });

    test('las tres pantallas montan AppVersionLabel sin version hardcodeada',
        () {
      const pages = [
        'lib/features/welcome/entry_page.dart',
        'lib/features/login/login_page.dart',
        'lib/features/kyc/presentation/kyc_start_page.dart',
      ];

      for (final path in pages) {
        final file = File(path);
        expect(file.existsSync(), isTrue, reason: 'Falta $path');
        final source = file.readAsStringSync();
        expect(
          source.contains('AppVersionLabel'),
          isTrue,
          reason: '$path debe montar AppVersionLabel',
        );
        // Ninguna pantalla define la version: solo la fuente unica la conoce.
        expect(
          source.contains('1.1.010') || source.contains('1.1.10'),
          isFalse,
          reason: '$path no debe hardcodear la version',
        );
      }
    });

    test('pubspec.yaml usa semver valido 1.1.10+1', () {
      final pubspec = File('pubspec.yaml').readAsStringSync();
      expect(pubspec.contains('version: 1.1.10+1'), isTrue);
    });
  });

  group('pantalla /entry', () {
    testWidgets('muestra la version visible del build', (tester) async {
      await tester.pumpWidget(const MaterialApp(home: EntryPage()));

      expect(find.byType(AppVersionLabel), findsOneWidget);
      expect(find.byKey(const Key('app-version')), findsOneWidget);
      expect(find.text('Version $kAppVersion'), findsOneWidget);
      // La etiqueta no tapa las acciones de la pantalla.
      expect(find.text('Iniciar sesión'), findsOneWidget);
      expect(find.text('Crear cuenta'), findsOneWidget);
    });
  });
}
