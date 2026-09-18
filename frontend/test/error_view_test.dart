import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/widgets/error_view.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('error de red muestra mensaje en espanol y boton reintentar',
      (tester) async {
    var retried = false;
    final message = ApiException.network().message;

    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ErrorView(
            message: message,
            onRetry: () => retried = true,
          ),
        ),
      ),
    );

    expect(find.text(message), findsOneWidget);
    expect(find.text('Reintentar'), findsOneWidget);

    await tester.tap(find.text('Reintentar'));
    await tester.pump();
    expect(retried, isTrue);
  });
}
