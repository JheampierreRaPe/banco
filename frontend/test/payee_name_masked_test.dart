import 'package:banca_online/features/wallet/data/personal_qr_service.dart';
import 'package:banca_online/features/wallet/data/qr_payment_service.dart';
import 'package:banca_online/features/wallet/data/wallet_service.dart';
import 'package:banca_online/features/wallet/models/personal_qr.dart';
import 'package:banca_online/features/wallet/models/qr_payment.dart';
import 'package:banca_online/features/wallet/models/wallet.dart';
import 'package:banca_online/features/wallet/presentation/personal_qr_page.dart';
import 'package:banca_online/features/wallet/presentation/qr_pay_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Mascara del servidor usada en estos tests (valor opaco del backend;
/// el cliente la muestra tal cual, sin calcularla; sin PII en claro).
const kMaskedName = 'DIEG••• GONZ•••';

/// Fake del QR personal con mascara configurable en el `GET`
/// (`payee_name_masked` aditivo de E4-T24).
class FakeMaskedQrService implements PersonalQrServiceBase {
  FakeMaskedQrService({this.statusMasked = kMaskedName});

  /// Mascara que devuelve `getPersonalQrStatus` (`null` = ausente).
  final String? statusMasked;

  static const created = PersonalQr(
    chargeId: 'chg-mask-1',
    qrContent: 'BO-QR1.mask',
    payloadHash: 'h',
    currency: 'PEN',
    mode: 'STATIC',
    status: 'ACTIVE',
    ownerUserId: 'owner-1',
    reusable: true,
  );

  @override
  Future<PersonalQr> createPersonalQr({
    String currency = 'PEN',
    String? concept,
    String? idempotencyKey,
  }) async =>
      created;

  @override
  Future<PersonalQrStatus> getPersonalQrStatus(String chargeId) async =>
      PersonalQrStatus(
        chargeId: 'chg-mask-1',
        qrContent: 'BO-QR1.mask',
        status: 'ACTIVE',
        currency: 'PEN',
        payeeNameMasked: statusMasked,
      );
}

/// Fake de pago QR con mascara configurable en preview y resultado.
class FakeMaskedPayService implements QrPaymentServiceBase {
  FakeMaskedPayService({
    this.previewMasked = kMaskedName,
    this.resultMasked,
    this.previewMerchantName = '',
    this.previewPayeeOwnerId = 'owner-1',
  });

  final String? previewMasked;
  final String? resultMasked;
  final String previewMerchantName;
  final String? previewPayeeOwnerId;

  @override
  Future<QrPaymentResult> payQr({
    required String qrContent,
    required String linkId,
    int? amountMinor,
    String? idempotencyKey,
  }) async =>
      QrPaymentResult(
        transactionId: 'txn-mask-1',
        qrPaymentId: 'qrp-mask-1',
        chargeId: 'chg-mask-1',
        merchantId: '',
        merchantName: '',
        payeeOwnerId: 'owner-1',
        payeeNameMasked: resultMasked,
        amountMinor: amountMinor ?? 1000,
        currency: 'PEN',
        status: 'SETTLED',
      );

  @override
  Future<QrChargePreview> previewQr({required String qrContent}) async =>
      QrChargePreview(
        chargeId: 'chg-mask-1',
        merchantId: '',
        merchantName: previewMerchantName,
        payeeOwnerId: previewPayeeOwnerId,
        payeeNameMasked: previewMasked,
        amountMinor: 0,
        currency: 'PEN',
        status: 'ACTIVE',
      );
}

/// Fake de billetera con una fuente activa.
class FakeMaskedWalletService implements WalletServiceBase {
  @override
  Future<Wallet> getWallet() async => const Wallet(
        walletId: 'w-1',
        status: 'ACTIVE',
        links: [
          WalletLink(
            id: 'l-1',
            type: 'ACCOUNT',
            accountMasked: '****1234',
            status: 'ACTIVE',
          ),
        ],
      );

  @override
  Future<WalletLink> linkAccount(AccountLinkInput input,
          {String? idempotencyKey}) =>
      throw UnimplementedError();

  @override
  Future<WalletLink> linkCard(CardLinkInput input, {String? idempotencyKey}) =>
      throw UnimplementedError();

  @override
  Future<WalletLink> unlink(String id) => throw UnimplementedError();
}

void main() {
  group('modelos con payee_name_masked (E4-T24)', () {
    test('PersonalQrStatus parsea la mascara del servidor', () {
      final status = PersonalQrStatus.fromJson({
        'charge_id': 'chg-1',
        'qr_content': 'BO-QR1.x',
        'status': 'ACTIVE',
        'currency': 'PEN',
        'payee_name_masked': kMaskedName,
      });
      expect(status.payeeNameMasked, kMaskedName);
    });

    test('PersonalQrStatus tolera ausencia/null/vacio (null)', () {
      for (final body in [
        {'charge_id': 'chg-1', 'status': 'ACTIVE', 'currency': 'PEN'},
        {
          'charge_id': 'chg-1',
          'status': 'ACTIVE',
          'currency': 'PEN',
          'payee_name_masked': null,
        },
        {
          'charge_id': 'chg-1',
          'status': 'ACTIVE',
          'currency': 'PEN',
          'payee_name_masked': '  ',
        },
      ]) {
        expect(
          PersonalQrStatus.fromJson(body).payeeNameMasked,
          isNull,
          reason: 'body: $body',
        );
      }
    });

    test('QrChargePreview parsea la mascara y conserva business_name',
        () {
      final preview = QrChargePreview.fromJson({
        'charge_id': 'chg-1',
        'merchant': {'merchant_id': 'm-1', 'business_name': 'Café Central'},
        'payee_name_masked': kMaskedName,
        'amount_minor': 2550,
        'currency': 'PEN',
        'status': 'ACTIVE',
      });
      expect(preview.payeeNameMasked, kMaskedName);
      // Comercio: el nombre publico queda intacto (aditivo).
      expect(preview.merchantName, 'Café Central');
    });

    test('QrChargePreview sin mascara -> null (fila oculta)', () {
      final preview = QrChargePreview.fromJson({
        'charge_id': 'chg-1',
        'payee': {'owner_user_id': 'owner-1'},
        'amount_minor': null,
        'currency': 'PEN',
        'status': 'ACTIVE',
      });
      expect(preview.isPersonal, isTrue);
      expect(preview.payeeNameMasked, isNull);
    });

    test('QrPaymentResult tolera la mascara presente/ausente', () {
      final withMask = QrPaymentResult.fromJson({
        'transaction_id': 'txn-1',
        'qr_payment_id': 'qrp-1',
        'charge_id': 'chg-1',
        'merchant': null,
        'payee': {'owner_user_id': 'owner-1'},
        'payee_name_masked': kMaskedName,
        'amount_minor': 1000,
        'currency': 'PEN',
        'status': 'SETTLED',
      });
      expect(withMask.payeeNameMasked, kMaskedName);

      final withoutMask = QrPaymentResult.fromJson({
        'transaction_id': 'txn-1',
        'qr_payment_id': 'qrp-1',
        'charge_id': 'chg-1',
        'merchant': null,
        'payee': {'owner_user_id': 'owner-1'},
        'amount_minor': 1000,
        'currency': 'PEN',
        'status': 'SETTLED',
      });
      expect(withoutMask.payeeNameMasked, isNull);
    });
  });

  group('PersonalQrPage muestra la mascara del titular (E4-T24)', () {
    testWidgets('tras actualizar estado se ve Titular: <mascara>',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(home: PersonalQrPage(service: FakeMaskedQrService())),
      );
      await tester.tap(find.byKey(const Key('personal-qr-generate')));
      await tester.pumpAndSettle();
      // Antes del refresh (solo POST, sin el campo) no hay fila.
      expect(
        find.byKey(const Key('personal-qr-payee-name')),
        findsNothing,
      );

      await tester.tap(find.byKey(const Key('personal-qr-refresh')));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('personal-qr-payee-name')),
        findsOneWidget,
      );
      expect(find.text('Titular: $kMaskedName'), findsOneWidget);
    });

    testWidgets('sin mascara del servidor se oculta la fila', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: PersonalQrPage(service: FakeMaskedQrService(statusMasked: null)),
        ),
      );
      await tester.tap(find.byKey(const Key('personal-qr-generate')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('personal-qr-refresh')));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('personal-qr-payee-name')),
        findsNothing,
      );
    });
  });

  group('QrPayPage muestra Pagas a: <mascara> (E4-T24)', () {
    Widget harness(FakeMaskedPayService pay) => MaterialApp(
          home: QrPayPage(
            qrContent: 'BO-QR1.personal-mask',
            paymentService: pay,
            walletService: FakeMaskedWalletService(),
            onRescan: () {},
            onDone: () {},
          ),
        );

    testWidgets('resumen con mascara muestra a quien pagas antes de confirmar',
        (tester) async {
      await tester.pumpWidget(harness(FakeMaskedPayService()));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('qr-pay-summary')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-payee-name')), findsOneWidget);
      expect(find.text('Pagas a: $kMaskedName'), findsOneWidget);
      // Sin PII en claro: solo viaja la mascara.
      expect(find.textContaining('Diego'), findsNothing);
    });

    testWidgets('resumen sin mascara oculta la fila', (tester) async {
      await tester.pumpWidget(
        harness(FakeMaskedPayService(previewMasked: null)),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('qr-pay-summary')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-payee-name')), findsNothing);
    });

    testWidgets('comercio conserva business_name y suma la mascara',
        (tester) async {
      await tester.pumpWidget(
        harness(
          FakeMaskedPayService(
            previewMerchantName: 'Café Central',
            previewPayeeOwnerId: null,
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Café Central'), findsOneWidget);
      expect(find.text('Pagas a: $kMaskedName'), findsOneWidget);
    });

    testWidgets('recibo muestra la mascara del preview', (tester) async {
      await tester.pumpWidget(harness(FakeMaskedPayService()));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('qr-pay-amount-field')),
        '10.00',
      );
      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
      expect(find.text('Pagas a: $kMaskedName'), findsOneWidget);
    });

    testWidgets('recibo oculta la fila si nadie trae mascara',
        (tester) async {
      await tester.pumpWidget(
        harness(FakeMaskedPayService(previewMasked: null)),
      );
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('qr-pay-amount-field')),
        '10.00',
      );
      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-payee-name')), findsNothing);
    });
  });
}
