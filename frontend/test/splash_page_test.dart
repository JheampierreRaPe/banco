import 'dart:async';

import 'package:banca_online/core/theme/app_colors.dart';
import 'package:banca_online/core/theme/app_theme.dart';
import 'package:banca_online/features/splash/presentation/splash_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

Widget _harness({SplashWait? wait, String nextLocation = '/home'}) {
  return MaterialApp(
    theme: AppTheme.light,
    home: SplashPage(wait: wait, nextLocation: nextLocation),
  );
}

void main() {
  testWidgets('renderiza la marca con el estilo Eucalipto y Ocre',
      (tester) async {
    // Espera pendiente: la splash se queda pintada (contenido + cargando).
    final pending = Completer<void>();
    addTearDown(() {
      if (!pending.isCompleted) pending.complete();
    });
    await tester.pumpWidget(_harness(wait: () => pending.future));
    await tester.pump();

    expect(find.byKey(const Key('splash-brand')), findsOneWidget);
    expect(find.text('CC'), findsOneWidget);
    expect(find.text('CuyCash'), findsOneWidget);
    expect(find.text('Banca digital · Perú'), findsOneWidget);
    expect(find.byKey(const Key('splash-progress')), findsOneWidget);
    expect(find.byType(LinearProgressIndicator), findsOneWidget);

    final scaffold = tester.widget<Scaffold>(find.byType(Scaffold));
    expect(scaffold.backgroundColor, AppColors.primaryContainer);
  });

  test('la espera por defecto es acotada (no bloquea el arranque)', () {
    expect(kSplashDuration.inMilliseconds, greaterThan(0));
    expect(kSplashDuration, lessThanOrEqualTo(const Duration(seconds: 3)));
  });

  testWidgets('al terminar delega navegando a nextLocation sin decidir',
      (tester) async {
    // La splash no calcula destino: va al neutro y el router resuelve.
    final router = GoRouter(
      initialLocation: '/splash',
      routes: [
        GoRoute(
          path: '/splash',
          builder: (context, state) => const SplashPage(
            wait: _immediate,
            nextLocation: '/destino',
          ),
        ),
        GoRoute(
          path: '/destino',
          builder: (context, state) => const Text('destino resuelto'),
        ),
      ],
    );
    addTearDown(router.dispose);

    await tester.pumpWidget(MaterialApp.router(routerConfig: router));
    await tester.pumpAndSettle();

    expect(find.text('destino resuelto'), findsOneWidget);
    expect(find.text('CuyCash'), findsNothing);
  });
}

Future<void> _immediate() async {}
