import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/wallet/data/personal_qr_service.dart';
import 'package:banca_online/features/wallet/data/qr_payment_service.dart';
import 'package:banca_online/features/wallet/data/wallet_service.dart';
import 'package:banca_online/features/wallet/models/personal_qr.dart';
import 'package:banca_online/features/wallet/models/qr_payment.dart';
import 'package:banca_online/features/wallet/models/wallet.dart';
import 'package:banca_online/features/wallet/presentation/personal_qr_page.dart';
import 'package:banca_online/features/wallet/presentation/qr_pay_page.dart';
import 'package:banca_online/features/wallet/presentation/wallet_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Fake del QR personal (seam `PersonalQrServiceBase`): sin red, con
/// shapes E4-T21 (`POST /wallet/qr` -> `data` sin monto ni caducidad;
/// `GET /wallet/qr/{id}` -> `data` con `status`).
class FakePersonalQrService implements PersonalQrServiceBase {
  int createCalls = 0;
  final List<String?> createKeys = [];
  ApiException? createError;
  bool hangCreate = false;

  int statusCalls = 0;
  ApiException? statusError;

  static const created = PersonalQr(
    chargeId: 'chg-personal-1',
    qrContent: 'BO-QR1.personal-sin-monto',
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
  }) async {
    createCalls++;
    createKeys.add(idempotencyKey);
    if (createError != null) throw createError!;
    if (hangCreate) await Future<void>.delayed(const Duration(seconds: 5));
    return created;
  }

  @override
  Future<PersonalQrStatus> getPersonalQrStatus(String chargeId) async {
    statusCalls++;
    if (statusError != null) throw statusError!;
    return const PersonalQrStatus(
      chargeId: 'chg-personal-1',
      qrContent: 'BO-QR1.personal-sin-monto',
      status: 'ACTIVE',
      currency: 'PEN',
    );
  }
}

/// Fake de pago QR con preview personal (sin monto, E4-T21).
class FakePersonalPayService implements QrPaymentServiceBase {
  final List<Map<String, dynamic>> payBodies = [];

  QrPaymentResult nextResult = const QrPaymentResult(
    transactionId: 'txn-p1',
    qrPaymentId: 'qrp-p1',
    chargeId: 'chg-personal-1',
    merchantId: '',
    merchantName: '',
    payeeOwnerId: 'owner-1',
    amountMinor: 2550,
    currency: 'PEN',
    status: 'SETTLED',
  );

  static const preview = QrChargePreview(
    chargeId: 'chg-personal-1',
    merchantId: '',
    merchantName: '',
    payeeOwnerId: 'owner-1',
    amountMinor: 0,
    currency: 'PEN',
    status: 'ACTIVE',
  );

  @override
  Future<QrPaymentResult> payQr({
    required String qrContent,
    required String linkId,
    int? amountMinor,
    String? idempotencyKey,
  }) async {
    final body = <String, dynamic>{
      'qr_content': qrContent,
      'link_id': linkId,
    };
    final amount = amountMinor;
    if (amount != null) body['amount_minor'] = amount;
    payBodies.add(body);
    return nextResult;
  }

  @override
  Future<QrChargePreview> previewQr({required String qrContent}) async {
    return preview;
  }
}

/// Fake de billetera para elegir fuente y para `WalletPage`.
class FakePersonalWalletService implements WalletServiceBase {
  FakePersonalWalletService({this.links = const [], this.isMerchant = true});

  final List<WalletLink> links;

  /// Rol de comercio (E4-T23): `true` por defecto porque estos tests
  /// verifican la entrada de comercio junto a "Mi QR personal".
  final bool isMerchant;

  @override
  Future<Wallet> getWallet() async => Wallet(
        walletId: 'w-1',
        status: 'ACTIVE',
        links: links,
        isMerchant: isMerchant,
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
  group('modelos de QR personal (E4-T21)', () {
    test('fromJson tolera merchant/amount nulos del preview personal', () {
      final preview = QrChargePreview.fromJson({
        'charge_id': 'chg-1',
        'merchant': null,
        'payee': {'owner_user_id': 'owner-1'},
        'amount_minor': null,
        'currency': 'PEN',
        'expires_at': null,
        'status': 'ACTIVE',
      });
      expect(preview.isPersonal, isTrue);
      expect(preview.amountMinor, 0);
      expect(preview.displayName, 'QR personal');
    });

    test('fromJson del pago personal expone el titular destino', () {
      final result = QrPaymentResult.fromJson({
        'transaction_id': 'txn-1',
        'qr_payment_id': 'qrp-1',
        'charge_id': 'chg-1',
        'merchant': null,
        'payee': {'owner_user_id': 'owner-1'},
        'amount_minor': 2550,
        'currency': 'PEN',
        'status': 'SETTLED',
      });
      expect(result.isPersonal, isTrue);
      expect(result.payeeOwnerId, 'owner-1');
      expect(result.displayName, 'QR personal');
    });

    test('status personal es Activo', () {
      const status = PersonalQrStatus(
        chargeId: 'chg-1',
        qrContent: 'BO-QR1.x',
        status: 'ACTIVE',
        currency: 'PEN',
      );
      expect(status.isActive, isTrue);
      expect(status.statusLabel, 'Activo');
    });
  });

  group('PersonalQrPage (E4-T21)', () {
    testWidgets('genera y muestra el QR sin monto ni vencimiento',
        (tester) async {
      final service = FakePersonalQrService();
      await tester.pumpWidget(
        MaterialApp(home: PersonalQrPage(service: service)),
      );
      expect(find.byKey(const Key('personal-qr-form')), findsOneWidget);

      await tester.tap(find.byKey(const Key('personal-qr-generate')));
      await tester.pumpAndSettle();

      expect(service.createCalls, 1);
      expect(service.createKeys.single, isNotNull);
      expect(find.byKey(const Key('personal-qr-result')), findsOneWidget);
      // Sin monto fijo ni vencimiento: copy de reutilizable.
      expect(find.textContaining('Sin monto fijo'), findsOneWidget);
      expect(find.textContaining('reutilizable'), findsOneWidget);
    });

    testWidgets('reintentar reusa la misma clave (sin duplicar)',
        (tester) async {
      final service = FakePersonalQrService()..createError = ApiException.network();
      await tester.pumpWidget(
        MaterialApp(home: PersonalQrPage(service: service)),
      );
      await tester.tap(find.byKey(const Key('personal-qr-generate')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('personal-qr-form-error')), findsOneWidget);

      service.createError = null;
      await tester.tap(find.byKey(const Key('personal-qr-generate')));
      await tester.pumpAndSettle();
      expect(service.createCalls, 2);
      expect(service.createKeys[0], service.createKeys[1]);
      expect(find.byKey(const Key('personal-qr-result')), findsOneWidget);
    });

    testWidgets('actualizar estado consulta al servidor', (tester) async {
      final service = FakePersonalQrService();
      await tester.pumpWidget(
        MaterialApp(home: PersonalQrPage(service: service)),
      );
      await tester.tap(find.byKey(const Key('personal-qr-generate')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('personal-qr-refresh')));
      await tester.pumpAndSettle();
      expect(service.statusCalls, 1);
      expect(find.text('Activo'), findsOneWidget);
    });
  });

  group('QrPayPage con QR personal (monto del pagador)', () {
    Widget harness(FakePersonalPayService pay) {
      return MaterialApp(
        home: QrPayPage(
          qrContent: 'BO-QR1.personal',
          paymentService: pay,
          walletService: FakePersonalWalletService(links: const [
            WalletLink(
              id: 'l-1',
              type: 'ACCOUNT',
              accountMasked: '****1234',
              status: 'ACTIVE',
            ),
          ]),
          onRescan: () {},
          onDone: () {},
        ),
      );
    }

    testWidgets('muestra campo de monto y lo envia en centimos',
        (tester) async {
      final pay = FakePersonalPayService();
      await tester.pumpWidget(harness(pay));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('qr-pay-amount-field')), findsOneWidget);
      expect(
        find.text('QR personal confirmado por el servidor'),
        findsOneWidget,
      );

      await tester.enterText(
        find.byKey(const Key('qr-pay-amount-field')),
        '25.50',
      );
      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();

      expect(pay.payBodies, hasLength(1));
      expect(pay.payBodies.single['amount_minor'], 2550);
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
    });

    testWidgets('monto invalido bloquea sin llamar al servidor',
        (tester) async {
      final pay = FakePersonalPayService();
      await tester.pumpWidget(harness(pay));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('qr-pay-amount-field')),
        'cero',
      );
      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pump();
      expect(pay.payBodies, isEmpty);
      expect(find.textContaining('mayor a S/ 0.00'), findsOneWidget);
    });
  });

  group('WalletPage (E4-T21)', () {
    testWidgets('expone Mi QR personal en vacio y con vinculos',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: WalletPage(
            service: FakePersonalWalletService(),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('wallet-personal-qr-empty')), findsOneWidget);
      // Reencuadre del copy comercio.
      expect(find.text('Tengo un comercio: generar cobro QR'), findsOneWidget);
      expect(find.text('Soy comercio: generar cobro QR'), findsNothing);
    });

    testWidgets('expone Mi QR personal con vinculos', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: WalletPage(
            service: FakePersonalWalletService(links: const [
              WalletLink(
                id: 'l-1',
                type: 'ACCOUNT',
                accountMasked: '****1234',
                status: 'ACTIVE',
              ),
            ]),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('wallet-personal-qr-action')),
        findsOneWidget,
      );
      expect(find.text('Cobrar como comercio'), findsOneWidget);
    });
  });
}
