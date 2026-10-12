import 'package:banca_online/features/credits/models/loan_application.dart';
import 'package:banca_online/features/credits/presentation/credit_status_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'credits_application_test.dart' show FakeCreditApplicationsService;

/// Regresión: la pantalla de Estado no debe re-ofrecer la firma de un
/// expediente ya firmado (defecto: el detalle `GET .../{id}` no traía
/// `contract_signed` y el CTA "Continuar a la firma" seguía visible).
///
/// - `APPROVED` + `contractSigned == true`: SIN `credit-status-sign-cta`;
///   muestra `Firmado` (`credit-status-signed-badge`) + `Ver contrato`
///   (`credit-status-contract-cta`) + `Volver al inicio`
///   (`credit-status-home-cta`).
/// - `APPROVED` sin firmar: SÍ muestra `credit-status-sign-cta`.
/// Sin el fix, el primer test falla (el CTA de firma sigue visible).
Future<void> _reveal(WidgetTester tester, Finder finder) async {
  await tester.scrollUntilVisible(
    finder,
    400,
    scrollable: find.byType(Scrollable).first,
  );
  await tester.pumpAndSettle();
}

void main() {
  group('CreditStatusPage firmado (regresión re-firma)', () {
    testWidgets('aprobado y firmado: sin CTA de firma, con Firmado',
        (tester) async {
      final fake = FakeCreditApplicationsService();
      await tester.pumpWidget(
        MaterialApp(
          home: CreditStatusPage(
            service: fake,
            initialResult: const LoanApplicationResult(
              id: 'app-firmado',
              status: LoanApplicationStatus.approved,
              decisionReason: 'Aprobado.',
              requestedAmountMinor: 1000000,
              termMonths: 12,
              contractSigned: true,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Aprobado'), findsOneWidget);
      expect(find.byKey(const Key('credit-status-signed-badge')), findsOneWidget);
      expect(find.text('Firmado'), findsOneWidget);
      expect(find.byKey(const Key('credit-status-sign-cta')), findsNothing);
      expect(find.text('Continuar a la firma'), findsNothing);
      await _reveal(tester, find.byKey(const Key('credit-status-home-cta')));
      expect(find.byKey(const Key('credit-status-contract-cta')), findsOneWidget);
      expect(find.text('Ver contrato'), findsOneWidget);
      expect(find.text('Volver al inicio'), findsOneWidget);
    });

    testWidgets('aprobado sin firmar: mantiene Continuar a la firma',
        (tester) async {
      final fake = FakeCreditApplicationsService();
      await tester.pumpWidget(
        MaterialApp(
          home: CreditStatusPage(
            service: fake,
            initialResult: const LoanApplicationResult(
              id: 'app-por-firmar',
              status: LoanApplicationStatus.approved,
              decisionReason: 'Aprobado.',
              requestedAmountMinor: 1000000,
              termMonths: 12,
              contractSigned: false,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      await _reveal(tester, find.byKey(const Key('credit-status-sign-cta')));
      expect(find.text('Continuar a la firma'), findsOneWidget);
      expect(find.byKey(const Key('credit-status-signed-badge')), findsNothing);
      expect(find.byKey(const Key('credit-status-contract-cta')), findsNothing);
    });

    test('LoanApplicationResult paresa contract_signed (defecto false)', () {
      expect(
        LoanApplicationResult.fromJson(const {
          'id': 'a',
          'status': 'APPROVED',
        }).contractSigned,
        isFalse,
      );
      expect(
        LoanApplicationResult.fromJson(const {
          'id': 'a',
          'status': 'APPROVED',
          'contract_signed': true,
        }).contractSigned,
        isTrue,
      );
      expect(
        LoanApplicationResult.fromJson(const {
          'id': 'a',
          'status': 'APPROVED',
          'contract_signed': false,
        }).contractSigned,
        isFalse,
      );
    });
  });
}
