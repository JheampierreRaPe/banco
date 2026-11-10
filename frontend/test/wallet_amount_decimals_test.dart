import 'package:banca_online/features/wallet/data/services_service.dart';
import 'package:banca_online/features/wallet/data/wallet_service.dart';
import 'package:banca_online/features/wallet/models/services.dart';
import 'package:banca_online/features/wallet/models/wallet.dart';
import 'package:banca_online/features/wallet/presentation/service_pay_page.dart';
import 'package:banca_online/features/wallet/presentation/topup_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

const _kBiller = Biller(
  billerId: 'b-luz',
  category: 'ELECTRICIDAD',
  name: 'Luz del Sur',
  requiredFields: ['supply_ref'],
);

const _kOperator = Biller(
  billerId: 'b-claro',
  category: 'TELEFONIA',
  name: 'Claro Perú',
  requiredFields: ['phone'],
);

ServiceOperationResult _settled(int minor) => ServiceOperationResult(
      operationId: 'op-1',
      transactionId: 'txn-1',
      status: 'SETTLED',
      amountMinor: minor,
      currency: 'PEN',
      receipt: ServiceReceipt(
        receiptNo: 'OP-TEST12345678',
        transactionId: 'txn-1',
        concept: 'Test',
        maskedRef: '****5678',
        amountMinor: minor,
        currency: 'PEN',
        status: 'SETTLED',
      ),
    );

class _FakeServices implements ServicesServiceBase {
  final List<int> payMinors = [];
  final List<int> topupMinors = [];

  @override
  Future<List<Biller>> catalog({String? category, String? query}) async =>
      [_kOperator];

  @override
  Future<ServiceOperationResult> payService({
    required String billerId,
    required String supplyRef,
    required int amountMinor,
    required String linkId,
    String? currency,
    String? idempotencyKey,
  }) async {
    payMinors.add(amountMinor);
    return _settled(amountMinor);
  }

  @override
  Future<ServiceOperationResult> topup({
    required String operatorRef,
    required String phone,
    required int amountMinor,
    required String linkId,
    String? currency,
    String? idempotencyKey,
  }) async {
    topupMinors.add(amountMinor);
    return _settled(amountMinor);
  }

  @override
  Future<ServiceSubscription> subscribe({
    required String billerId,
    required String supplyRef,
    int? notifyDaysBefore,
  }) =>
      throw UnimplementedError();
}

class _FakeWallet implements WalletServiceBase {
  static const links = [
    WalletLink(
      id: 'l-1',
      type: 'ACCOUNT',
      accountMasked: '****1234',
      status: 'ACTIVE',
    ),
  ];

  @override
  Future<Wallet> getWallet() async =>
      const Wallet(walletId: 'w-1', status: 'ACTIVE', links: links);

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

Future<void> _toServicePayForm(WidgetTester tester, _FakeServices svc) async {
  await tester.pumpWidget(
    MaterialApp(
      home: ServicePayPage(
        biller: _kBiller,
        servicesService: svc,
        walletService: _FakeWallet(),
        onDone: () {},
      ),
    ),
  );
  await tester.pumpAndSettle();
  expect(find.byKey(const Key('service-pay-form')), findsOneWidget);
}

Future<void> _toTopupForm(WidgetTester tester, _FakeServices svc) async {
  await tester.pumpWidget(
    MaterialApp(
      home: TopupPage(
        servicesService: svc,
        walletService: _FakeWallet(),
        onDone: () {},
      ),
    ),
  );
  await tester.pumpAndSettle();
  expect(find.byKey(const Key('topup-form')), findsOneWidget);
}

void main() {
  group('H2: rechazo de mas de 2 decimales (sin truncar)', () {
    testWidgets('ServicePayPage rechaza "12.567" sin enviar', (tester) async {
      final svc = _FakeServices();
      await _toServicePayForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('service-pay-supply-field')),
        '12345678',
      );
      await tester.enterText(
        find.byKey(const Key('service-pay-amount-field')),
        '12.567',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('service-pay-confirm')));
      await tester.pump();

      expect(
        find.text('Usa máximo 2 decimales (céntimos).'),
        findsOneWidget,
      );
      expect(svc.payMinors, isEmpty);
      expect(find.byKey(const Key('services-receipt')), findsNothing);
    });

    testWidgets('TopupPage rechaza "12.5678" sin enviar', (tester) async {
      final svc = _FakeServices();
      await _toTopupForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('topup-phone-field')),
        '987654321',
      );
      await tester.enterText(
        find.byKey(const Key('topup-amount-field')),
        '12.5678',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('topup-confirm')));
      await tester.pump();

      expect(
        find.text('Usa máximo 2 decimales (céntimos).'),
        findsOneWidget,
      );
      expect(svc.topupMinors, isEmpty);
      expect(find.byKey(const Key('services-receipt')), findsNothing);
    });

    testWidgets('ServicePayPage acepta "12.56" como 1256', (tester) async {
      final svc = _FakeServices();
      await _toServicePayForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('service-pay-supply-field')),
        '12345678',
      );
      await tester.enterText(
        find.byKey(const Key('service-pay-amount-field')),
        '12.56',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('service-pay-confirm')));
      await tester.pumpAndSettle();

      expect(svc.payMinors, [1256]);
      expect(find.byKey(const Key('services-receipt')), findsOneWidget);
    });

    testWidgets('ServicePayPage acepta "12.5" como 1250', (tester) async {
      final svc = _FakeServices();
      await _toServicePayForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('service-pay-supply-field')),
        '12345678',
      );
      await tester.enterText(
        find.byKey(const Key('service-pay-amount-field')),
        '12.5',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('service-pay-confirm')));
      await tester.pumpAndSettle();

      expect(svc.payMinors, [1250]);
      expect(find.byKey(const Key('services-receipt')), findsOneWidget);
    });

    testWidgets('ServicePayPage acepta "12" como 1200', (tester) async {
      final svc = _FakeServices();
      await _toServicePayForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('service-pay-supply-field')),
        '12345678',
      );
      await tester.enterText(
        find.byKey(const Key('service-pay-amount-field')),
        '12',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('service-pay-confirm')));
      await tester.pumpAndSettle();

      expect(svc.payMinors, [1200]);
      expect(find.byKey(const Key('services-receipt')), findsOneWidget);
    });

    testWidgets('TopupPage acepta "12.56" como 1256', (tester) async {
      final svc = _FakeServices();
      await _toTopupForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('topup-phone-field')),
        '987654321',
      );
      await tester.enterText(
        find.byKey(const Key('topup-amount-field')),
        '12.56',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('topup-confirm')));
      await tester.pumpAndSettle();

      expect(svc.topupMinors, [1256]);
      expect(find.byKey(const Key('services-receipt')), findsOneWidget);
    });

    testWidgets('TopupPage acepta "12.5" como 1250', (tester) async {
      final svc = _FakeServices();
      await _toTopupForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('topup-phone-field')),
        '987654321',
      );
      await tester.enterText(
        find.byKey(const Key('topup-amount-field')),
        '12.5',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('topup-confirm')));
      await tester.pumpAndSettle();

      expect(svc.topupMinors, [1250]);
      expect(find.byKey(const Key('services-receipt')), findsOneWidget);
    });

    testWidgets('TopupPage acepta "12" como 1200', (tester) async {
      final svc = _FakeServices();
      await _toTopupForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('topup-phone-field')),
        '987654321',
      );
      await tester.enterText(
        find.byKey(const Key('topup-amount-field')),
        '12',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('topup-confirm')));
      await tester.pumpAndSettle();

      expect(svc.topupMinors, [1200]);
      expect(find.byKey(const Key('services-receipt')), findsOneWidget);
    });
  });
}
