import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/payment_authorization/data/payment_authorization_service.dart';
import 'package:banca_online/features/payment_authorization/payment_authorization_controller.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/transfers/models/transfer.dart';
import 'package:banca_online/features/wallet/data/services_service.dart';
import 'package:banca_online/features/wallet/data/wallet_service.dart';
import 'package:banca_online/features/wallet/models/services.dart';
import 'package:banca_online/features/wallet/models/wallet.dart';
import 'package:banca_online/features/wallet/presentation/service_pay_page.dart';
import 'package:banca_online/features/wallet/presentation/service_subscriptions_page.dart';
import 'package:banca_online/features/wallet/presentation/services_search_page.dart';
import 'package:banca_online/features/wallet/presentation/topup_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

const kBillerLuz = Biller(
  billerId: 'b-luz',
  category: 'ELECTRICIDAD',
  name: 'Luz del Sur',
  requiredFields: ['supply_ref'],
);
const kBillerClaro = Biller(
  billerId: 'b-claro',
  category: 'TELEFONIA',
  name: 'Claro Perú',
  requiredFields: ['phone'],
);

ServiceOperationResult _settledPay() => ServiceOperationResult(
      operationId: 'pay-1',
      transactionId: 'txn-pay-1',
      status: 'SETTLED',
      amountMinor: 2550,
      currency: 'PEN',
      receipt: const ServiceReceipt(
        receiptNo: 'OP-ABCDEF123456',
        transactionId: 'txn-pay-1',
        concept: 'Luz del Sur',
        maskedRef: '****5678',
        amountMinor: 2550,
        currency: 'PEN',
        paidAt: '2026-10-13T01:00:00Z',
        status: 'SETTLED',
      ),
    );

ServiceOperationResult _settledTopup() => ServiceOperationResult(
      operationId: 'top-1',
      transactionId: 'txn-top-1',
      status: 'SETTLED',
      amountMinor: 1000,
      currency: 'PEN',
      receipt: const ServiceReceipt(
        receiptNo: 'OP-123456ABCDEF',
        transactionId: 'txn-top-1',
        concept: 'Claro Perú',
        maskedRef: '****4321',
        amountMinor: 1000,
        currency: 'PEN',
        paidAt: '2026-10-13T01:00:00Z',
        status: 'SETTLED',
      ),
    );

ServiceOperationResult _pendingPay() => const ServiceOperationResult(
      operationId: 'pay-2',
      transactionId: 'txn-pay-2',
      status: 'PENDING_AUTHORIZATION',
      amountMinor: 99900,
      currency: 'PEN',
      authMethod: 'BIOMETRIC',
      authNonce: 'nonce-9',
      authExpiresAt: '2026-10-13T02:00:00Z',
    );

ServiceOperationResult _settledPending() => ServiceOperationResult(
      operationId: 'pay-2',
      transactionId: 'txn-pay-2',
      status: 'SETTLED',
      amountMinor: 99900,
      currency: 'PEN',
      receipt: const ServiceReceipt(
        receiptNo: 'OP-99990000AAAA',
        transactionId: 'txn-pay-2',
        concept: 'Luz del Sur',
        maskedRef: '****5678',
        amountMinor: 99900,
        currency: 'PEN',
        status: 'SETTLED',
      ),
    );

/// Fake del servicio (seam `ServicesServiceBase`): sin red, con shapes
/// E4-T16/E4-T17 (`GET /services/catalog`, `POST /services/payments`,
/// `POST /topups`, `POST /services/subscriptions`).
class FakeServicesService implements ServicesServiceBase {
  List<Biller> billers = [kBillerLuz, kBillerClaro];
  ApiException? catalogError;
  bool hangCatalog = false;

  final List<Map<String, dynamic>> payBodies = [];
  final List<String?> payKeys = [];
  ServiceOperationResult nextPay = _settledPay();
  final List<ServiceOperationResult> payQueue = [];
  ApiException? payError;
  bool failNextPay = false;

  final List<Map<String, dynamic>> topupBodies = [];
  final List<String?> topupKeys = [];
  ServiceOperationResult nextTopup = _settledTopup();
  ApiException? topupError;

  final List<Map<String, dynamic>> subBodies = [];
  ServiceSubscription nextSub = const ServiceSubscription(
    subscriptionId: 'sub-1',
    billerId: 'b-luz',
    supplyRef: '12345678',
    notifyDaysBefore: 3,
    status: 'ACTIVE',
  );
  ApiException? subError;

  @override
  Future<List<Biller>> catalog({String? category, String? query}) async {
    if (catalogError != null) throw catalogError!;
    if (hangCatalog) await Completer<void>().future;
    return billers;
  }

  @override
  Future<ServiceOperationResult> payService({
    required String billerId,
    required String supplyRef,
    required int amountMinor,
    required String linkId,
    String? currency,
    String? idempotencyKey,
  }) async {
    payBodies.add({
      'biller_id': billerId,
      'supply_ref': supplyRef,
      'amount_minor': amountMinor,
      'currency': currency ?? 'PEN',
      'link_id': linkId,
    });
    payKeys.add(idempotencyKey);
    if (payError != null) throw payError!;
    if (failNextPay) {
      failNextPay = false;
      throw ApiException.network();
    }
    if (payQueue.isNotEmpty) return payQueue.removeAt(0);
    return nextPay;
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
    topupBodies.add({
      'operator': operatorRef,
      'phone': phone,
      'amount_minor': amountMinor,
      'currency': currency ?? 'PEN',
      'link_id': linkId,
    });
    topupKeys.add(idempotencyKey);
    if (topupError != null) throw topupError!;
    return nextTopup;
  }

  @override
  Future<ServiceSubscription> subscribe({
    required String billerId,
    required String supplyRef,
    int? notifyDaysBefore,
  }) async {
    subBodies.add({
      'biller_id': billerId,
      'supply_ref': supplyRef,
      'notify_days_before': notifyDaysBefore,
    });
    if (subError != null) throw subError!;
    return nextSub;
  }
}

class FakeSvcWalletService implements WalletServiceBase {
  FakeSvcWalletService({this.links = const [], this.hang = false});

  static const defaultLinks = [
    WalletLink(
      id: 'l-1',
      type: 'ACCOUNT',
      accountMasked: '****1234',
      status: 'ACTIVE',
    ),
  ];

  final List<WalletLink> links;
  final bool hang;

  @override
  Future<Wallet> getWallet() async {
    if (hang) await Completer<void>().future;
    return Wallet(walletId: 'w-1', status: 'ACTIVE', links: links);
  }

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

class FakeSvcAuthService implements PaymentAuthorizationServiceBase {
  int authorizeCalls = 0;
  ApiException? authorizeError;

  @override
  Future<AuthorizationResult> authorize({
    required String transactionId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  }) async {
    authorizeCalls++;
    if (authorizeError != null) throw authorizeError!;
    return AuthorizationResult(transactionId: transactionId, status: 'SETTLED');
  }

  @override
  Future<AuthorizationResult> authorizeWithPin({
    required String transactionId,
    required String pin,
    String? idempotencyKey,
  }) async {
    if (authorizeError != null) throw authorizeError!;
    return AuthorizationResult(transactionId: transactionId, status: 'SETTLED');
  }

  @override
  Future<TransactionDetail> refreshChallenge(String transactionId) =>
      throw UnimplementedError();
}

PaymentAuthorizationController _bioController(FakeSvcAuthService service) {
  return PaymentAuthorizationController(
    service: service,
    reader: FakeBiometricReader(),
    signNonce: (nonce) async => 'sig-of-$nonce',
    deviceId: 'dev-1',
  );
}

void main() {
  group('ServicesSearchPage (buscar -> listar)', () {
    testWidgets('carga y lista billers del servidor', (tester) async {
      Biller? selected;
      await tester.pumpWidget(
        MaterialApp(
          home: ServicesSearchPage(
            service: FakeServicesService(),
            onSelect: (b) => selected = b,
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('services-search-loading')), findsNothing);
      expect(find.text('Luz del Sur'), findsOneWidget);
      expect(find.text('Claro Perú'), findsOneWidget);
      expect(find.byKey(const Key('services-search-count')), findsOneWidget);

      await tester.tap(find.text('Luz del Sur'));
      await tester.pump();
      expect(selected?.billerId, 'b-luz');
    });

    testWidgets('muestra carga mientras busca', (tester) async {
      final service = FakeServicesService()..hangCatalog = true;
      await tester.pumpWidget(
        MaterialApp(home: ServicesSearchPage(service: service)),
      );
      await tester.pump();

      expect(find.byKey(const Key('services-search-loading')), findsOneWidget);
    });

    testWidgets('vacio accionable cuando no hay resultados', (tester) async {
      final service = FakeServicesService()..billers = [];
      await tester.pumpWidget(
        MaterialApp(home: ServicesSearchPage(service: service)),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('services-search-empty')), findsOneWidget);
      expect(
        find.textContaining('No encontramos servicios'),
        findsOneWidget,
      );
    });

    testWidgets('error 404 mapea a mensaje neutro', (tester) async {
      final service = FakeServicesService()
        ..catalogError = ApiException(
          code: 'NOT_FOUND',
          message: 'No encontrado.',
          statusCode: 404,
        );
      await tester.pumpWidget(
        MaterialApp(home: ServicesSearchPage(service: service)),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('services-error')), findsOneWidget);
      expect(find.textContaining('No encontramos ese servicio'), findsOneWidget);
    });

    testWidgets('red muestra Reintentar y recupera', (tester) async {
      final service = FakeServicesService()
        ..catalogError = ApiException.network();
      await tester.pumpWidget(
        MaterialApp(home: ServicesSearchPage(service: service)),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('Sin conexión'), findsOneWidget);
      expect(find.byKey(const Key('services-retry')), findsOneWidget);

      service.catalogError = null;
      await tester.tap(find.byKey(const Key('services-retry')));
      await tester.pumpAndSettle();
      expect(find.text('Luz del Sur'), findsOneWidget);
    });
  });

  group('ServicePayPage (pagar -> comprobante)', () {
    Future<void> toForm(WidgetTester tester, FakeServicesService svc) async {
      await tester.pumpWidget(
        MaterialApp(
          home: ServicePayPage(
            biller: kBillerLuz,
            servicesService: svc,
            walletService:
                FakeSvcWalletService(links: FakeSvcWalletService.defaultLinks),
            onDone: () {},
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('service-pay-form')), findsOneWidget);
    }

    testWidgets('pagar servicio -> comprobante del servidor', (tester) async {
      final svc = FakeServicesService();
      await toForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('service-pay-supply-field')),
        '12345678',
      );
      await tester.enterText(
        find.byKey(const Key('service-pay-amount-field')),
        '25.50',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('service-pay-confirm')));
      await tester.pumpAndSettle();

      // Datos crudos enviados; montos en centimos exactos, sin recalculo.
      expect(svc.payBodies, hasLength(1));
      expect(svc.payBodies.single['biller_id'], 'b-luz');
      expect(svc.payBodies.single['supply_ref'], '12345678');
      expect(svc.payBodies.single['amount_minor'], 2550);
      expect(svc.payBodies.single['link_id'], 'l-1');
      expect(svc.payKeys.single, isNotNull);

      // Comprobante estructurado con numero oficial del backend.
      expect(find.byKey(const Key('services-receipt')), findsOneWidget);
      expect(find.byKey(const Key('services-receipt-no')), findsOneWidget);
      expect(find.text('OP-ABCDEF123456'), findsOneWidget);
      expect(find.text('S/ 25.50'), findsOneWidget);
      expect(find.text('****5678'), findsOneWidget);
    });

    testWidgets('reintento reusa la misma Idempotency-Key', (tester) async {
      final svc = FakeServicesService()..failNextPay = true;
      await toForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('service-pay-supply-field')),
        '12345678',
      );
      await tester.enterText(
        find.byKey(const Key('service-pay-amount-field')),
        '25.50',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('service-pay-confirm')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('services-error')), findsOneWidget);

      await tester.tap(find.byKey(const Key('services-retry')));
      await tester.pumpAndSettle();

      expect(svc.payBodies, hasLength(2));
      expect(svc.payKeys[0], isNotNull);
      expect(svc.payKeys[1], svc.payKeys[0]);
      expect(find.byKey(const Key('services-receipt')), findsOneWidget);
    });

    testWidgets('sobre el limite -> biometria -> replay misma clave',
        (tester) async {
      final svc = FakeServicesService()
        ..payQueue.addAll([_pendingPay(), _settledPending()]);
      final auth = FakeSvcAuthService();
      await tester.pumpWidget(
        MaterialApp(
          home: ServicePayPage(
            biller: kBillerLuz,
            servicesService: svc,
            walletService:
                FakeSvcWalletService(links: FakeSvcWalletService.defaultLinks),
            authController: _bioController(auth),
            authService: auth,
            onDone: () {},
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('service-pay-supply-field')),
        '12345678',
      );
      await tester.enterText(
        find.byKey(const Key('service-pay-amount-field')),
        '999.00',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('service-pay-confirm')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('service-pay-pending-card')), findsOneWidget);
      await tester.tap(
        find.byKey(const Key('service-pay-authorize-biometric')),
      );
      await tester.pumpAndSettle();

      expect(auth.authorizeCalls, 1);
      expect(svc.payBodies, hasLength(2));
      expect(svc.payKeys[1], svc.payKeys[0]);
      expect(find.byKey(const Key('services-receipt')), findsOneWidget);
      expect(find.text('OP-99990000AAAA'), findsOneWidget);
    });

    testWidgets('REJECTED muestra failure_code sin marcar exito',
        (tester) async {
      final svc = FakeServicesService()
        ..nextPay = const ServiceOperationResult(
          operationId: 'pay-3',
          transactionId: 'txn-3',
          status: 'REJECTED',
          amountMinor: 50000,
          currency: 'PEN',
          failureCode: 'INSUFFICIENT_FUNDS',
        );
      await toForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('service-pay-supply-field')),
        '12345678',
      );
      await tester.enterText(
        find.byKey(const Key('service-pay-amount-field')),
        '500.00',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('service-pay-confirm')));
      await tester.pumpAndSettle();

      expect(find.textContaining('saldo suficiente'), findsOneWidget);
      expect(find.byKey(const Key('services-receipt')), findsNothing);
    });

    testWidgets('sin biller pide ir al catalogo', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: ServicePayPage(
            servicesService: FakeServicesService(),
            walletService: FakeSvcWalletService(links: const []),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('service-pay-empty-biller')), findsOneWidget);
    });
  });

  group('TopupPage (recargar -> comprobante)', () {
    Future<void> toForm(WidgetTester tester, FakeServicesService svc) async {
      await tester.pumpWidget(
        MaterialApp(
          home: TopupPage(
            servicesService: svc,
            walletService:
                FakeSvcWalletService(links: FakeSvcWalletService.defaultLinks),
            onDone: () {},
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('topup-form')), findsOneWidget);
    }

    testWidgets('recargar -> comprobante del servidor', (tester) async {
      final svc = FakeServicesService();
      await toForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('topup-phone-field')),
        '987654321',
      );
      await tester.enterText(
        find.byKey(const Key('topup-amount-field')),
        '10.00',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('topup-confirm')));
      await tester.pumpAndSettle();

      expect(svc.topupBodies, hasLength(1));
      expect(svc.topupBodies.single['phone'], '987654321');
      expect(svc.topupBodies.single['amount_minor'], 1000);
      expect(svc.topupKeys.single, isNotNull);

      expect(find.byKey(const Key('services-receipt')), findsOneWidget);
      expect(find.text('OP-123456ABCDEF'), findsOneWidget);
      expect(find.text('S/ 10.00'), findsOneWidget);
    });

    testWidgets('telefono invalido no llama al servidor', (tester) async {
      final svc = FakeServicesService();
      await toForm(tester, svc);

      await tester.enterText(find.byKey(const Key('topup-phone-field')), '12');
      await tester.enterText(
        find.byKey(const Key('topup-amount-field')),
        '10.00',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('topup-confirm')));
      await tester.pump();

      expect(find.textContaining('6 a 15 dígitos'), findsOneWidget);
      expect(svc.topupBodies, isEmpty);
    });

    testWidgets('409 mapea a mensaje sin duplicar', (tester) async {
      final svc = FakeServicesService()
        ..topupError = ApiException(
          code: 'CONFLICT',
          message: 'Duplicado.',
          statusCode: 409,
        );
      await toForm(tester, svc);

      await tester.enterText(
        find.byKey(const Key('topup-phone-field')),
        '987654321',
      );
      await tester.enterText(
        find.byKey(const Key('topup-amount-field')),
        '10.00',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('topup-confirm')));
      await tester.pumpAndSettle();

      expect(find.textContaining('ya fue registrada'), findsOneWidget);
      expect(svc.topupBodies, hasLength(1));
    });
  });

  group('ServiceSubscriptionsPage (registro)', () {
    testWidgets('registrar servicio -> aparece con estado', (tester) async {
      final svc = FakeServicesService();
      await tester.pumpWidget(
        MaterialApp(home: ServiceSubscriptionsPage(service: svc)),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('subscriptions-empty')), findsOneWidget);

      await tester.enterText(
        find.byKey(const Key('subscriptions-supply-field')),
        '12345678',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('subscriptions-register')));
      await tester.pumpAndSettle();

      expect(svc.subBodies, hasLength(1));
      expect(svc.subBodies.single['biller_id'], 'b-luz');
      expect(svc.subBodies.single['supply_ref'], '12345678');
      expect(find.byKey(const Key('subscriptions-item-sub-1')), findsOneWidget);
      expect(find.text('Recordatorio activo'), findsOneWidget);
      expect(find.byKey(const Key('subscriptions-empty')), findsNothing);
    });

    testWidgets('422 muestra error de validacion en el formulario',
        (tester) async {
      final svc = FakeServicesService()
        ..subError = ApiException(
          code: 'VALIDATION_ERROR',
          message: 'Suministro inválido.',
          statusCode: 422,
        );
      await tester.pumpWidget(
        MaterialApp(home: ServiceSubscriptionsPage(service: svc)),
      );
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('subscriptions-supply-field')),
        'xxx',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('subscriptions-register')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('subscriptions-form-error')), findsOneWidget);
      expect(find.textContaining('datos ingresados'), findsOneWidget);
    });
  });

  group('ServicesService (HTTP)', () {
    ApiClient stubClient(
      Response<dynamic> Function(RequestOptions options) handler, {
      Map<String, dynamic>? Function(RequestOptions options)? onRequest,
    }) {
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
      final api = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 'token'),
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, h) {
            onRequest?.call(options);
            h.resolve(handler(options));
          },
        ),
      );
      return api;
    }

    test('catalog envia category/q y parsea billers', () async {
      Map<String, dynamic>? params;
      String? path;
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': [
              {
                'biller_id': 'b-luz',
                'category': 'ELECTRICIDAD',
                'name': 'Luz del Sur',
                'required_fields': ['supply_ref'],
              },
            ],
            'meta': {'request_id': 'r-1'},
          },
        ),
        onRequest: (o) {
          path = o.path;
          params = Map<String, dynamic>.from(o.queryParameters);
          return null;
        },
      );

      final billers = await ServicesService(api: api).catalog(
        category: 'ELECTRICIDAD',
        query: 'luz',
      );

      expect(path, endsWith('/services/catalog'));
      expect(params!['category'], 'ELECTRICIDAD');
      expect(params!['q'], 'luz');
      expect(billers.single.name, 'Luz del Sur');
      expect(billers.single.requiredFields, ['supply_ref']);
    });

    test('payService usa Idempotency-Key y parsea receipt', () async {
      String? key;
      Map<String, dynamic>? body;
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: {
            'data': {
              'payment_id': 'pay-1',
              'transaction_id': 'txn-1',
              'status': 'SETTLED',
              'amount_minor': 2550,
              'currency': 'PEN',
              'receipt': {
                'receipt_no': 'OP-ABCDEF123456',
                'transaction_id': 'txn-1',
                'concept': 'Luz del Sur',
                'supply_masked': '****5678',
                'amount_minor': 2550,
                'currency': 'PEN',
                'paid_at': '2026-10-13T01:00:00Z',
                'status': 'SETTLED',
              },
            },
            'meta': {'request_id': 'r-1', 'replayed': false},
          },
        ),
        onRequest: (o) {
          key = o.headers['Idempotency-Key']?.toString();
          body = Map<String, dynamic>.from(o.data as Map);
          return null;
        },
      );

      final result = await ServicesService(api: api).payService(
        billerId: 'b-luz',
        supplyRef: '12345678',
        amountMinor: 2550,
        linkId: 'l-1',
        idempotencyKey: 'key-svc-1',
      );

      expect(key, 'key-svc-1');
      expect(body!['amount_minor'], 2550);
      expect(result.isSettled, isTrue);
      expect(result.receipt?.receiptNo, 'OP-ABCDEF123456');
      expect(result.amountMinor, 2550);
    });

    test('topup envia operator/phone con Idempotency-Key', () async {
      String? path;
      Map<String, dynamic>? body;
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: {
            'data': {
              'topup_id': 'top-1',
              'transaction_id': 'txn-top-1',
              'status': 'SETTLED',
              'amount_minor': 1000,
              'currency': 'PEN',
              'receipt': {
                'receipt_no': 'OP-123456ABCDEF',
                'transaction_id': 'txn-top-1',
                'concept': 'Claro Perú',
                'phone_masked': '****4321',
                'amount_minor': 1000,
                'currency': 'PEN',
                'status': 'SETTLED',
              },
            },
            'meta': {'request_id': 'r-2', 'replayed': false},
          },
        ),
        onRequest: (o) {
          path = o.path;
          body = Map<String, dynamic>.from(o.data as Map);
          return null;
        },
      );

      final result = await ServicesService(api: api).topup(
        operatorRef: 'b-claro',
        phone: '987654321',
        amountMinor: 1000,
        linkId: 'l-1',
        idempotencyKey: 'key-top-1',
      );

      expect(path, endsWith('/topups'));
      expect(body!['operator'], 'b-claro');
      expect(body!['phone'], '987654321');
      expect(result.receipt?.maskedRef, '****4321');
    });

    test('subscribe no usa Idempotency-Key (no mueve dinero)', () async {
      String? path;
      String? key;
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: {
            'data': {
              'subscription_id': 'sub-1',
              'biller_id': 'b-luz',
              'supply_ref': '12345678',
              'notify_days_before': 3,
              'status': 'ACTIVE',
            },
            'meta': {'request_id': 'r-3', 'replayed': false},
          },
        ),
        onRequest: (o) {
          path = o.path;
          key = o.headers['Idempotency-Key']?.toString();
          return null;
        },
      );

      final sub = await ServicesService(api: api).subscribe(
        billerId: 'b-luz',
        supplyRef: '12345678',
      );

      expect(path, endsWith('/services/subscriptions'));
      expect(key, isNull);
      expect(sub.statusLabel, 'Recordatorio activo');
    });
  });

  group('serviceFailureFor + formato (sin recalculo)', () {
    test('mapea cada codigo a mensaje accionable', () {
      expect(
        serviceFailureFor(code: 'NOT_FOUND').message,
        contains('No encontramos ese servicio'),
      );
      expect(serviceFailureFor(code: 'NOT_FOUND').retry, isFalse);
      expect(
        serviceFailureFor(code: 'CONFLICT').message,
        contains('ya fue registrada'),
      );
      expect(serviceFailureFor(code: 'NETWORK_ERROR').retry, isTrue);
      expect(
        serviceFailureFor(code: 'UNKNOWN', failureCode: 'INSUFFICIENT_FUNDS')
            .message,
        contains('saldo suficiente'),
      );
      expect(
        serviceFailureFor(
          code: 'UNKNOWN',
          failureCode: 'DAILY_LIMIT_EXCEEDED',
        ).message,
        contains('límite diario'),
      );
    });

    test('formatea centimos del servidor sin recalculo', () {
      expect(formatServiceMinor(2550, 'PEN'), 'S/ 25.50');
      expect(formatServiceMinor(1000, 'PEN'), 'S/ 10.00');
      expect(formatServiceMinor(100, 'USD'), 'US\$ 1.00');
    });
  });
}
