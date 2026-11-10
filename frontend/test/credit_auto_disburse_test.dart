import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/credits/data/credit_contract_service.dart';
import 'package:banca_online/features/credits/models/credit_contract.dart';
import 'package:banca_online/features/credits/presentation/credit_sign_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Desembolso automático al firmar (E3-T24, HU12; decisión del dueño:
/// inline en la firma, sin CTA manual).
///
/// - Tras firmar, la página muestra la confirmación
///   (`Tu crédito de S/ X fue desembolsado en tu cuenta ****dddd`, datos
///   del servidor en el bloque aditivo `disbursement`) y la CTA primaria
///   `Ver cronograma` lleva a `/credits/loans`.
/// - La CTA manual solo reaparece como reintento si el automático no
///   liquidó (`disbursement` ausente o `FAILED`).
/// - Cliente delgado: montos y cuenta se muestran TAL CUAL los devuelve
///   el servidor; sin PII en logs.

/// Respuesta firmada con desembolso automático liquidado (shape `05` §6.4
/// + bloque aditivo E3-T24).
const _signedAutoSettled = ContractSignatureResult(
  applicationId: 'app-1',
  contractId: 'c-1',
  status: 'SIGNED',
  signedAt: '2026-10-16T12:00:00Z',
  via: 'device',
  disbursement: ContractDisbursement(
    status: 'SETTLED',
    loanId: 'loan-1',
    transactionId: 'tx-1',
    amountMinor: 1000000,
    accountMasked: '****2424',
    disbursedAt: '2026-10-16T12:00:01Z',
  ),
);

/// Respuesta firmada con automático fallido (queda el reintento manual).
const _signedAutoFailed = ContractSignatureResult(
  applicationId: 'app-1',
  contractId: 'c-1',
  status: 'SIGNED',
  signedAt: '2026-10-16T12:00:00Z',
  via: 'device',
  disbursement: ContractDisbursement(
    status: 'FAILED',
    code: 'VALIDATION_ERROR',
    message: 'Reintenta el desembolso desde tu expediente.',
  ),
);

class _FakeSignService implements CreditContractServiceBase {
  _FakeSignService(this.signedResult);

  final ContractSignatureResult signedResult;

  @override
  Future<ContractBundle> getContract(String applicationId) =>
      throw UnimplementedError();

  @override
  Future<ContractBundle> generateContract(
    String applicationId, {
    String? idempotencyKey,
  }) =>
      throw UnimplementedError();

  @override
  Future<ContractPdf> downloadDocument(String applicationId, String doc) =>
      throw UnimplementedError();

  @override
  Future<SignChallenge> requestChallenge(
    String applicationId, {
    String? deviceId,
  }) async =>
      const SignChallenge(
        applicationId: 'app-1',
        nonce: 'nonce-1',
        expiresIn: 120,
        status: 'GENERATED',
      );

  @override
  Future<ContractSignatureResult> signWithBiometrics({
    required String applicationId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  }) async =>
      signedResult;

  @override
  Future<ContractSignatureResult> signWithPin({
    required String applicationId,
    required String pin,
    String? idempotencyKey,
  }) async =>
      signedResult;
}

Future<String> _fakeSign(String nonce) async => 'a' * 64;

/// Bomba la página de firma hasta el éxito biométrico (consentimiento +
/// CTA), con las rutas necesarias para asertar la navegación.
Future<void> _pumpSigned(
  WidgetTester tester, {
  required ContractSignatureResult result,
  required GoRouter router,
}) async {
  await tester.pumpWidget(
    MaterialApp.router(
      routerConfig: router,
    ),
  );
  await tester.pumpAndSettle();

  await tester.tap(find.byKey(const Key('sign-consent')));
  await tester.pumpAndSettle();
  await tester.scrollUntilVisible(
    find.byKey(const Key('sign-biometric-cta')),
    400,
    scrollable: find.byType(Scrollable).first,
  );
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const Key('sign-biometric-cta')));
  await tester.pumpAndSettle();
  await tester.drag(
    find.byKey(const Key('sign-content')),
    const Offset(0, -5000),
  );
  await tester.pumpAndSettle();
}

GoRouter _routerFor(ContractSignatureResult result) {
  return GoRouter(
    initialLocation: '/',
    routes: [
      GoRoute(
        path: '/',
        builder: (context, state) => CreditSignPage(
          service: _FakeSignService(result),
          reader: FakeBiometricReader(),
          signNonce: _fakeSign,
          deviceId: 'dev-1',
          applicationId: 'app-1',
          summary: 'Personal - S/ 10,000 - 12 m',
          initialChallenge: const SignChallenge(
            applicationId: 'app-1',
            nonce: 'nonce-1',
            expiresIn: 120,
            status: 'GENERATED',
          ),
        ),
      ),
      GoRoute(
        path: '/credits/loans',
        builder: (context, state) => const Scaffold(
          body: Text('cronograma Loans'),
        ),
      ),
    ],
  );
}

void main() {
  group('ContractSignatureResult (bloque aditivo E3-T24)', () {
    test('parsea disbursement SETTLED con monto y cuenta enmascarada', () {
      final result = ContractSignatureResult.fromJson({
        'application_id': 'app-1',
        'contract_id': 'c-1',
        'status': 'SIGNED',
        'signed_at': '2026-10-16T12:00:00Z',
        'via': 'device',
        'documents': [],
        'disbursement': {
          'status': 'SETTLED',
          'loan_id': 'loan-1',
          'transaction_id': 'tx-1',
          'amount_minor': 1000000,
          'account_masked': '****2424',
          'disbursed_at': '2026-10-16T12:00:01Z',
        },
      });
      expect(result.isSigned, isTrue);
      expect(result.isAutoDisbursed, isTrue);
      expect(result.disbursement?.amountMinor, 1000000);
      expect(result.disbursement?.accountMasked, '****2424');
      expect(result.disbursement?.loanId, 'loan-1');
    });

    test('sin bloque (respuesta vieja): no promete desembolso', () {
      final result = ContractSignatureResult.fromJson({
        'application_id': 'app-1',
        'contract_id': 'c-1',
        'status': 'SIGNED',
        'documents': [],
      });
      expect(result.disbursement, isNull);
      expect(result.isAutoDisbursed, isFalse);
    });

    test('FAILED no se presenta como liquidado', () {
      const failed = ContractDisbursement(
        status: 'FAILED',
        code: 'VALIDATION_ERROR',
      );
      expect(failed.isSettled, isFalse);
    });
  });

  group('CreditSignPage con desembolso automático (E3-T24)', () {
    testWidgets('confirmación + Ver cronograma; sin CTA manual',
        (tester) async {
      await _pumpSigned(
        tester,
        result: _signedAutoSettled,
        router: _routerFor(_signedAutoSettled),
      );

      expect(find.byKey(const Key('sign-success')), findsOneWidget);
      expect(
        find.byKey(const Key('sign-disburse-confirm')),
        findsOneWidget,
      );
      expect(
        find.text(
          'Tu crédito de S/ 10,000.00 fue desembolsado en tu cuenta ****2424.',
        ),
        findsOneWidget,
      );
      // La CTA manual desaparece; solo queda el foco al cronograma.
      expect(find.byKey(const Key('disburse-cta')), findsNothing);
      expect(find.byKey(const Key('sign-schedule-cta')), findsOneWidget);

      await tester.scrollUntilVisible(
        find.byKey(const Key('sign-schedule-cta')),
        400,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('sign-schedule-cta')));
      await tester.pumpAndSettle();

      expect(find.text('cronograma Loans'), findsOneWidget);
    });

    testWidgets('automático fallido: reintento visible, sin confirmación',
        (tester) async {
      await _pumpSigned(
        tester,
        result: _signedAutoFailed,
        router: _routerFor(_signedAutoFailed),
      );

      expect(find.byKey(const Key('sign-success')), findsOneWidget);
      expect(find.byKey(const Key('sign-disburse-confirm')), findsNothing);
      expect(find.byKey(const Key('sign-schedule-cta')), findsNothing);
      // Solo reintento (sección sin servicios: aviso honesto, sin CTA rota).
      expect(find.byKey(const Key('disburse-unavailable')), findsOneWidget);
    });
  });
}
