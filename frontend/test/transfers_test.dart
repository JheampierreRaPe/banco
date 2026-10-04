import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/transfers/data/transfers_service.dart';
import 'package:banca_online/features/transfers/models/resolve_target.dart';
import 'package:banca_online/features/transfers/models/transfer.dart';
import 'package:banca_online/features/transfers/presentation/amount_page.dart';
import 'package:banca_online/features/transfers/presentation/confirm_page.dart';
import 'package:banca_online/features/transfers/presentation/destination_page.dart';
import 'package:banca_online/features/transfers/presentation/interbank_form_page.dart';
import 'package:banca_online/features/transfers/presentation/receipt_page.dart';
import 'package:banca_online/features/transfers/presentation/tracking_page.dart';
import 'package:banca_online/features/transfers/presentation/transfers_home_page.dart';
import 'package:banca_online/features/transfers/presentation/widgets/cci_status_banner.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

const _a1 = Account(
  id: 'a1',
  accountNumberMasked: '•••• 8921',
  type: 'SAVINGS',
  currency: 'PEN',
  availableMinor: 485020,
  heldMinor: 0,
  balanceMinor: 485020,
);

const _a2 = Account(
  id: 'a2',
  accountNumberMasked: '•••• 4521',
  type: 'CHECKING',
  currency: 'PEN',
  availableMinor: 425080,
  heldMinor: 0,
  balanceMinor: 425080,
);

const _b1 = Beneficiary(
  id: 'b1',
  alias: 'Mamá Lima',
  accountMasked: '•••• 7744',
  bankCode: 'BCP',
  holderName: 'M. Rojas ••••',
  currency: 'PEN',
);

/// Fake del servicio (seam `TransfersServiceBase`): sin red, con shapes
/// docs/05 §6.3 (`{data, meta}` + `202` interbancario con `tracking`).
class FakeTransfersService implements TransfersServiceBase {
  FakeTransfersService({
    this.accountsSeed = const [_a1, _a2],
    this.beneficiariesSeed = const [_b1],
  });

  List<Account> accountsSeed;
  List<Beneficiary> beneficiariesSeed;

  bool hangAccounts = false;
  ApiException? accountsError;
  ApiException? beneficiariesError;

  TransferResult? ownResult;
  ApiException? ownError;
  TransferResult? thirdResult;
  ApiException? thirdError;
  TransferResult? interbankResult;
  ApiException? interbankError;

  /// Guion del polling: cada llamada a `getTransaction` consume un item
  /// ([TransactionDetail] o [ApiException]); agotado, repite el ultimo
  /// detalle (o un `SETTLED` por defecto).
  List<Object> pollScript = const [];
  int pollCalls = 0;

  ReceiptResult? receiptResult;
  ApiException? receiptError;
  ApiException? detailError;

  List<String?> keysOwn = [];
  List<String?> keysThird = [];
  List<String?> keysInterbank = [];
  String? lastBeneficiaryId;

  static TransferResult _own({
    required String source,
    required String target,
    required int amount,
  }) => TransferResult(
    transactionId: 'tx-own-1',
    type: 'OWN_TRANSFER',
    status: 'SETTLED',
    amountMinor: amount,
    currency: 'PEN',
    sourceAccountId: source,
    targetAccountId: target,
  );

  static TransferResult _interbankAccepted({required int amount}) =>
      TransferResult(
        transactionId: 'tx-ib-1',
        type: 'INTERBANK_TRANSFER',
        status: 'FUNDS_HELD',
        amountMinor: amount,
        currency: 'PEN',
        feeMinor: 350,
        externalRef: 'ext-1',
        tracking: {
          'poll_url': '/transactions/tx-ib-1',
          'status': 'FUNDS_HELD',
          'holder_name_masked': 'M*** R***',
        },
      );

  @override
  Future<List<Account>> accounts() async {
    if (accountsError != null) throw accountsError!;
    if (hangAccounts) await Completer<void>().future;
    return List<Account>.from(accountsSeed);
  }

  @override
  Future<List<Beneficiary>> beneficiaries({String? query}) async {
    if (beneficiariesError != null) throw beneficiariesError!;
    return List<Beneficiary>.from(beneficiariesSeed);
  }

  @override
  Future<TransferResult> createOwnTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) async {
    keysOwn.add(idempotencyKey);
    if (ownError != null) throw ownError!;
    return ownResult ??
        _own(
          source: sourceAccountId,
          target: targetAccountId,
          amount: amountMinor,
        );
  }

  @override
  Future<TransferResult> createThirdPartyTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    int? feeMinor,
    String? beneficiaryId,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) async {
    keysThird.add(idempotencyKey);
    lastBeneficiaryId = beneficiaryId;
    if (thirdError != null) throw thirdError!;
    return thirdResult ??
        TransferResult(
          transactionId: 'tx-third-1',
          type: 'THIRD_PARTY_TRANSFER',
          status: 'SETTLED',
          amountMinor: amountMinor,
          currency: currency,
          sourceAccountId: sourceAccountId,
          targetAccountId: targetAccountId,
          feeMinor: feeMinor ?? 0,
        );
  }

  @override
  Future<TransferResult> createInterbankTransfer({
    required String sourceAccountId,
    required int amountMinor,
    required String cci,
    String currency = 'PEN',
    String? holderName,
    String? bankCode,
    int? feeMinor,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) async {
    keysInterbank.add(idempotencyKey);
    if (interbankError != null) throw interbankError!;
    return interbankResult ?? _interbankAccepted(amount: amountMinor);
  }

  @override
  Future<ResolvedTarget> resolveTarget({
    String? documentType,
    String? documentNumber,
    String? phone,
  }) => throw UnimplementedError();

  @override
  Future<TransactionDetail> getTransaction(String transactionId) async {
    pollCalls++;
    if (pollScript.isNotEmpty) {
      final item =
          pollScript[pollCalls <= pollScript.length
              ? pollCalls - 1
              : pollScript.length - 1];
      if (item is ApiException) throw item;
      return item as TransactionDetail;
    }
    if (detailError != null) throw detailError!;
    return const TransactionDetail(
      transactionId: 'tx-ib-1',
      type: 'INTERBANK_TRANSFER',
      status: 'SETTLED',
      amountMinor: 35000,
      currency: 'PEN',
      feeMinor: 350,
      tracking: {'holder_name_masked': 'M*** R***'},
    );
  }

  @override
  Future<ReceiptResult> getReceipt(String transactionId) async {
    if (receiptError != null) throw receiptError!;
    return receiptResult ??
        const ReceiptResult(
          bytes: [1, 2, 3],
          suggestedFilename: 'comprobante-OP-ABCDEF123456.pdf',
          contentType: 'application/pdf',
        );
  }
}

const _settledOwn = TransactionDetail(
  transactionId: 'tx-own-1',
  type: 'OWN_TRANSFER',
  status: 'SETTLED',
  amountMinor: 25000,
  currency: 'PEN',
);

const _settledInterbank = TransactionDetail(
  transactionId: 'tx-ib-1',
  type: 'INTERBANK_TRANSFER',
  status: 'SETTLED',
  amountMinor: 35000,
  currency: 'PEN',
  feeMinor: 350,
  externalRef: 'ext-1',
  tracking: {'holder_name_masked': 'M*** R***'},
);

const _heldInterbank = TransactionDetail(
  transactionId: 'tx-ib-1',
  type: 'INTERBANK_TRANSFER',
  status: 'FUNDS_HELD',
  amountMinor: 35000,
  currency: 'PEN',
  feeMinor: 350,
  tracking: {'holder_name_masked': 'M*** R***'},
);

/// Harness con `GoRouter` real (las paginas usan `push`/`pop`/`extra`).
Widget _harness(
  FakeTransfersService fake, {
  String initial = '/transfers',
  TransferFlowArgs? confirmArgs,
}) {
  final router = GoRouter(
    initialLocation: initial,
    routes: [
      GoRoute(
        path: '/transfers',
        builder: (context, state) => const TransfersHomePage(),
      ),
      GoRoute(
        path: '/transfers/destination',
        builder: (context, state) {
          final kind = TransferKind.fromQuery(
            state.uri.queryParameters['kind'],
          );
          return DestinationPage(kind: kind, service: fake);
        },
      ),
      GoRoute(
        path: '/transfers/amount',
        builder: (context, state) =>
            AmountPage(args: state.extra as TransferFlowArgs),
      ),
      GoRoute(
        path: '/transfers/confirm',
        builder: (context, state) =>
            ConfirmPage(args: state.extra as TransferFlowArgs, service: fake),
      ),
      GoRoute(
        path: '/transfers/interbank',
        builder: (context, state) => InterbankFormPage(service: fake),
      ),
      GoRoute(
        path: '/transfers/tracking/:id',
        builder: (context, state) {
          final id = state.pathParameters['id'] ?? '';
          final extra = state.extra;
          return TrackingPage(
            args: extra is TrackingArgs
                ? extra
                : TrackingArgs(transactionId: id),
            service: fake,
          );
        },
      ),
      GoRoute(
        path: '/transfers/receipt/:id',
        builder: (context, state) => ReceiptPage(
          transactionId: state.pathParameters['id'] ?? '',
          service: fake,
        ),
      ),
      // Ruta solo-tests: confirmacion directa con borrador fijo.
      GoRoute(
        path: '/test-confirm',
        builder: (context, state) =>
            ConfirmPage(args: confirmArgs ?? _ownArgs(), service: fake),
      ),
    ],
  );
  return MaterialApp.router(routerConfig: router);
}

TransferFlowArgs _ownArgs({String key = 'k-test-1', int amount = 25000}) {
  final draft = TransferDraft(
    kind: TransferKind.own,
    sourceAccountId: 'a1',
    targetAccountId: 'a2',
    beneficiaryAlias: 'Cuenta CuyCash Corriente',
    destinationMasked: '•••• 4521',
    amountMinor: amount,
    idempotencyKey: key,
  );
  return TransferFlowArgs(draft: draft, source: _a1, target: _a2);
}

void main() {
  group('TransfersHomePage', () {
    testWidgets('hub muestra los 3 tipos y abre la interbancaria', (
      tester,
    ) async {
      await tester.pumpWidget(_harness(FakeTransfersService()));
      await tester.pumpAndSettle();

      expect(find.text('Entre mis cuentas'), findsOneWidget);
      expect(find.text('A un tercero'), findsOneWidget);
      expect(find.text('Interbancaria CCI'), findsOneWidget);

      await tester.tap(find.byKey(const Key('transfers-type-interbank')));
      await tester.pumpAndSettle();

      expect(find.text('Transferencia Interbancaria'), findsOneWidget);
      expect(find.text('DESTINO INTERBANCARIO CCE'), findsOneWidget);
    });
  });

  group('Flujo destino -> monto -> confirmacion -> comprobante', () {
    testWidgets('propia feliz con total del backend', (tester) async {
      final fake = FakeTransfersService();
      await tester.pumpWidget(
        _harness(fake, initial: '/transfers/destination?kind=own'),
      );
      await tester.pumpAndSettle();

      // Destino: elige la segunda cuenta (el origen a1 se excluye).
      await tester.tap(find.byKey(const Key('transfer-dest-a2')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('destination-continue')));
      await tester.pumpAndSettle();

      // Monto: chips + escritura manual.
      await tester.tap(find.byKey(const Key('amount-chip-200')));
      await tester.pump();
      await tester.enterText(find.byKey(const Key('amount-input')), '250.00');
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('amount-motive')),
        'Cena compartida',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('amount-continue')));
      await tester.pumpAndSettle();

      // Confirmacion: resumen con datos enmascarados + sin comision.
      expect(find.text('S/ 250.00'), findsWidgets);
      expect(find.text('SIN COMISIÓN'), findsOneWidget);
      expect(find.text('•••• 4521'), findsOneWidget);
      expect(find.text('Cena compartida'), findsOneWidget);

      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();

      // Comprobante: exito + numero de operacion del backend.
      expect(find.text('Transferencia exitosa'), findsOneWidget);
      expect(find.text('Nº OP-ABCDEF123456'), findsOneWidget);
      expect(fake.keysOwn, hasLength(1));
      expect(fake.keysOwn.single, isNotNull);
    });

    testWidgets('tercero envia beneficiary_id en metadata', (tester) async {
      final fake = FakeTransfersService();
      await tester.pumpWidget(
        _harness(fake, initial: '/transfers/destination?kind=third'),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('transfer-beneficiary-card-b1')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('destination-continue')));
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('amount-input')), '100.00');
      await tester.pump();
      await tester.tap(find.byKey(const Key('amount-continue')));
      await tester.pumpAndSettle();

      expect(find.text('Mamá Lima'), findsWidgets);
      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();

      expect(find.text('Transferencia exitosa'), findsOneWidget);
      expect(fake.lastBeneficiaryId, 'b1');
    });
  });

  group('ConfirmPage errores', () {
    Future<void> pumpConfirm(
      WidgetTester tester,
      FakeTransfersService fake,
      TransferFlowArgs args,
    ) async {
      await tester.pumpWidget(
        _harness(fake, initial: '/test-confirm', confirmArgs: args),
      );
      await tester.pumpAndSettle();
    }

    testWidgets('saldo insuficiente muestra tarjeta sin debitar', (
      tester,
    ) async {
      final fake = FakeTransfersService()
        ..ownResult = const TransferResult(
          transactionId: 'tx-own-2',
          type: 'OWN_TRANSFER',
          status: 'REJECTED',
          amountMinor: 35000,
          currency: 'PEN',
          feeMinor: 350,
          failureCode: 'INSUFFICIENT_FUNDS',
        );
      await pumpConfirm(tester, fake, _ownArgs(amount: 35000));

      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('insufficient-banner')), findsOneWidget);
      expect(find.text('No tienes saldo suficiente'), findsOneWidget);
      expect(
        find.textContaining('No se debitó nada', findRichText: true),
        findsWidgets,
      );
      // No navega al comprobante.
      expect(find.text('Transferencia exitosa'), findsNothing);
    });

    testWidgets('error de red muestra ErrorView y reintenta misma clave', (
      tester,
    ) async {
      final fake = FakeTransfersService()..ownError = ApiException.network();
      await pumpConfirm(tester, fake, _ownArgs(key: 'k-retry-1'));

      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('confirm-error')), findsOneWidget);

      fake.ownError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();

      expect(find.text('Transferencia exitosa'), findsOneWidget);
      expect(fake.keysOwn, ['k-retry-1', 'k-retry-1']);
    });

    testWidgets('409 idempotencia avisa operacion ya registrada', (
      tester,
    ) async {
      final fake = FakeTransfersService()
        ..ownError = ApiException(
          code: 'IDEMPOTENCY_CONFLICT',
          message: 'Conflicto.',
        );
      await pumpConfirm(tester, fake, _ownArgs());

      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();

      expect(find.textContaining('ya fue registrada'), findsOneWidget);
    });
  });

  group('Formulario interbancario CCI', () {
    Future<void> pumpForm(
      WidgetTester tester,
      FakeTransfersService fake,
    ) async {
      await tester.pumpWidget(_harness(fake, initial: '/transfers/interbank'));
      await tester.pumpAndSettle();
    }

    Future<void> fillValidCci(WidgetTester tester) async {
      await tester.enterText(
        find.byKey(const Key('cci-field')),
        '00219100284729104458',
      );
      await tester.pump();
      // La tarjeta de monto nace fuera del viewport (ListView perezoso):
      // se arrastra hasta construirla.
      for (
        var i = 0;
        i < 4 && find.byKey(const Key('interbank-amount')).evaluate().isEmpty;
        i++
      ) {
        await tester.drag(
          find.byKey(const Key('interbank-form-list')),
          const Offset(0, -400),
        );
        await tester.pumpAndSettle();
      }
      await tester.enterText(
        find.byKey(const Key('interbank-amount')),
        '350.00',
      );
      await tester.pump();
    }

    testWidgets('CCI incompleto bloquea continuar; 20 digitos lo habilita', (
      tester,
    ) async {
      await pumpForm(tester, FakeTransfersService());

      expect(find.text('0 / 20'), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('interbank-continue')))
            .enabled,
        isFalse,
      );

      await tester.enterText(
        find.byKey(const Key('cci-field')),
        '0021910028472910445',
      );
      await tester.pump();
      expect(find.text('19 / 20'), findsOneWidget);

      await fillValidCci(tester);
      expect(find.text('20 / 20'), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('interbank-continue')))
            .enabled,
        isTrue,
      );
    });

    testWidgets('flujo feliz va a seguimiento y al comprobante', (
      tester,
    ) async {
      final fake = FakeTransfersService();
      await pumpForm(tester, fake);
      await fillValidCci(tester);

      await tester.tap(find.byKey(const Key('interbank-continue')));
      await tester.pumpAndSettle();
      expect(find.text('Confirma tu transferencia'), findsOneWidget);

      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();

      // 202 aceptado -> seguimiento con titular verificado enmascarado.
      expect(find.text('Transferencia registrada'), findsOneWidget);
      expect(find.byKey(const Key('cci-banner-verified')), findsOneWidget);
      expect(find.text('M*** R***'), findsOneWidget);

      await tester.tap(find.byKey(const Key('tracking-receipt-cta')));
      await tester.pumpAndSettle();

      expect(find.text('Transferencia exitosa'), findsOneWidget);
      expect(find.text('Nº OP-ABCDEF123456'), findsOneWidget);
      expect(fake.keysInterbank, hasLength(1));
    });

    testWidgets('HOLDER_NOT_FOUND muestra no-hallado y vuelve al formulario', (
      tester,
    ) async {
      final fake = FakeTransfersService()
        ..interbankError = ApiException(
          code: 'HOLDER_NOT_FOUND',
          message: 'Neutro.',
        );
      await pumpForm(tester, fake);
      await fillValidCci(tester);

      await tester.tap(find.byKey(const Key('interbank-continue')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('cci-banner-not-found')), findsOneWidget);
      expect(
        find.text('No pudimos validar el titular con los datos ingresados'),
        findsOneWidget,
      );
      // El CCI en claro nunca se exhibe en el banner.
      expect(find.textContaining('00219100284729104458'), findsNothing);

      await tester.tap(find.text('Corregir datos'));
      await tester.pumpAndSettle();

      // De vuelta en el formulario con el banner neutro.
      expect(find.byKey(const Key('cci-banner-not-found')), findsOneWidget);
      expect(find.byKey(const Key('cci-field')), findsOneWidget);
    });

    testWidgets('error CCE ofrece reintento con la misma clave', (
      tester,
    ) async {
      final fake = FakeTransfersService()
        ..interbankError = ApiException(
          code: 'HOLDER_LOOKUP_UNAVAILABLE',
          message: 'Caída.',
        );
      await pumpForm(tester, fake);
      await fillValidCci(tester);

      await tester.tap(find.byKey(const Key('interbank-continue')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('cci-banner-unavailable')), findsOneWidget);
      expect(
        find.text('Servicio interbancario no disponible temporalmente'),
        findsOneWidget,
      );

      fake.interbankError = null;
      await tester.tap(find.byKey(const Key('confirm-retry')));
      await tester.pumpAndSettle();

      expect(find.text('Transferencia registrada'), findsOneWidget);
      expect(fake.keysInterbank, hasLength(2));
      expect(fake.keysInterbank[0], equals(fake.keysInterbank[1]));
    });
  });

  group('CciStatusBanner (4 estados)', () {
    Future<void> pumpBanner(WidgetTester tester, CciState state) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: CciStatusBanner(
              state: state,
              holderMasked: 'M*** R***',
              accountLabel: 'Cuenta Corriente Soles · BCP',
              onRetry: state == CciState.unavailable ? () {} : null,
            ),
          ),
        ),
      );
    }

    testWidgets('verificando muestra consulta CCE', (tester) async {
      await pumpBanner(tester, CciState.verifying);
      expect(find.byKey(const Key('cci-banner-verifying')), findsOneWidget);
      expect(
        find.textContaining('Consultando Cámara de Compensación'),
        findsOneWidget,
      );
    });

    testWidgets('ok muestra titular enmascarado', (tester) async {
      await pumpBanner(tester, CciState.verified);
      expect(find.byKey(const Key('cci-banner-verified')), findsOneWidget);
      expect(find.text('TITULAR CCE VERIFICADO'), findsOneWidget);
      expect(find.text('M*** R***'), findsOneWidget);
    });

    testWidgets('no hallado es neutro y accionable', (tester) async {
      await pumpBanner(tester, CciState.notFound);
      expect(find.byKey(const Key('cci-banner-not-found')), findsOneWidget);
      expect(find.textContaining('verifica el número de CCI'), findsOneWidget);
    });

    testWidgets('error CCE muestra reintento', (tester) async {
      var retried = false;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: CciStatusBanner(
              state: CciState.unavailable,
              onRetry: () => retried = true,
            ),
          ),
        ),
      );
      expect(find.byKey(const Key('cci-banner-unavailable')), findsOneWidget);
      await tester.tap(find.byKey(const Key('cci-retry')));
      expect(retried, isTrue);
    });

    testWidgets('idle no dibuja nada', (tester) async {
      await pumpBanner(tester, CciState.idle);
      expect(find.byType(CciStatusBanner), findsOneWidget);
      expect(find.textContaining('CCE'), findsNothing);
    });
  });

  group('TrackingPage (seguimiento 202)', () {
    testWidgets('polling FUNDS_HELD -> SETTLED con avance de tiempo', (
      tester,
    ) async {
      final fake = FakeTransfersService()
        ..pollScript = [_heldInterbank, _settledInterbank];
      await tester.pumpWidget(
        _harness(fake, initial: '/transfers/tracking/tx-ib-1'),
      );
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Procesando tu transferencia'), findsOneWidget);
      expect(find.byKey(const Key('tracking-loading')), findsOneWidget);

      await tester.pump(const Duration(seconds: 3));
      await tester.pump();
      await tester.pump(const Duration(seconds: 3));
      await tester.pumpAndSettle();

      expect(fake.pollCalls, greaterThanOrEqualTo(2));
      expect(find.text('Transferencia registrada'), findsOneWidget);
      expect(find.byKey(const Key('tracking-receipt-cta')), findsOneWidget);
    });

    testWidgets('timeout muestra seguimiento con reintento que no duplica', (
      tester,
    ) async {
      final fake = FakeTransfersService()
        ..pollScript = [
          const TransactionDetail(
            transactionId: 'tx-ib-1',
            type: 'INTERBANK_TRANSFER',
            status: 'FAILED',
            amountMinor: 35000,
            currency: 'PEN',
            feeMinor: 350,
            failureCode: 'TRANSFER_TIMEOUT',
          ),
        ];
      await tester.pumpWidget(
        _harness(fake, initial: '/transfers/tracking/tx-ib-1'),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('tracking-failure')), findsOneWidget);
      expect(find.text('Sigue en seguimiento'), findsOneWidget);
      expect(
        find.textContaining('no duplica el débito', findRichText: true),
        findsWidgets,
      );
      // El seguimiento es GET: ningun POST nuevo.
      expect(fake.keysInterbank, isEmpty);

      fake.pollScript = [_settledInterbank];
      await tester.tap(find.byKey(const Key('tracking-retry')));
      await tester.pumpAndSettle();

      expect(find.text('Transferencia registrada'), findsOneWidget);
      expect(fake.keysInterbank, isEmpty);
    });

    testWidgets('error de red en el poll ofrece reintentar', (tester) async {
      final fake = FakeTransfersService()
        ..pollScript = [ApiException.network(), _settledInterbank];
      await tester.pumpWidget(
        _harness(fake, initial: '/transfers/tracking/tx-ib-1'),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('Sin conexion'), findsOneWidget);

      await tester.tap(find.text('Reintentar seguimiento'));
      await tester.pumpAndSettle();

      expect(find.text('Transferencia registrada'), findsOneWidget);
    });
  });

  group('ReceiptPage', () {
    testWidgets('descarga informa nombre y pendiente de guardado', (
      tester,
    ) async {
      final fake = FakeTransfersService()..pollScript = [_settledOwn];
      await tester.pumpWidget(
        _harness(fake, initial: '/transfers/receipt/tx-own-1'),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('receipt-success')), findsOneWidget);
      expect(find.text('S/ 250.00'), findsWidgets);
      expect(find.text('Nº OP-ABCDEF123456'), findsOneWidget);

      for (
        var i = 0;
        i < 4 && find.byKey(const Key('receipt-download')).evaluate().isEmpty;
        i++
      ) {
        await tester.drag(find.byType(ListView), const Offset(0, -400));
        await tester.pumpAndSettle();
      }
      await tester.tap(find.byKey(const Key('receipt-download')));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('comprobante-OP-ABCDEF123456.pdf'),
        findsOneWidget,
      );
    });

    testWidgets('REJECTED muestra el motivo en vez del comprobante', (
      tester,
    ) async {
      final fake = FakeTransfersService()
        ..pollScript = [
          const TransactionDetail(
            transactionId: 'tx-own-9',
            type: 'OWN_TRANSFER',
            status: 'REJECTED',
            amountMinor: 35000,
            currency: 'PEN',
            failureCode: 'INSUFFICIENT_FUNDS',
          ),
        ];
      await tester.pumpWidget(
        _harness(fake, initial: '/transfers/receipt/tx-own-9'),
      );
      await tester.pumpAndSettle();

      expect(find.text('La transferencia no prosperó'), findsOneWidget);
      expect(find.textContaining('saldo suficiente'), findsOneWidget);
      expect(find.byKey(const Key('receipt-download')), findsNothing);
    });
  });

  group('DestinationPage estados', () {
    testWidgets('carga muestra skeleton', (tester) async {
      final fake = FakeTransfersService()..hangAccounts = true;
      await tester.pumpWidget(
        _harness(fake, initial: '/transfers/destination?kind=own'),
      );
      await tester.pump();

      expect(find.text('Cargando cuentas...'), findsOneWidget);
    });

    testWidgets('sin cuentas muestra vacio', (tester) async {
      final fake = FakeTransfersService()..accountsSeed = [];
      await tester.pumpWidget(
        _harness(fake, initial: '/transfers/destination?kind=own'),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('Aún no tienes cuentas'), findsOneWidget);
    });

    testWidgets('error muestra reintento y reintenta', (tester) async {
      final fake = FakeTransfersService()
        ..accountsError = ApiException.network();
      await tester.pumpWidget(
        _harness(fake, initial: '/transfers/destination?kind=own'),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('Sin conexion'), findsOneWidget);

      fake.accountsError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();

      expect(find.text('DESDE MI CUENTA'), findsOneWidget);
    });
  });

  group('TransfersService (HTTP)', () {
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

    test('createOwn envia Idempotency-Key y total viene del backend', () async {
      String? key;
      Map<String, dynamic>? body;
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: {
            'data': {
              'transaction_id': 'tx-1',
              'type': 'OWN_TRANSFER',
              'status': 'SETTLED',
              'source_account_id': 'a1',
              'target_account_id': 'a2',
              'amount_minor': 25000,
              'currency': 'PEN',
              'fee_minor': 0,
            },
            'meta': {'request_id': 'r1', 'replayed': false},
          },
        ),
        onRequest: (o) {
          key = o.headers['Idempotency-Key']?.toString();
          if (o.data is Map) {
            body = Map<String, dynamic>.from(o.data as Map);
          }
          return null;
        },
      );

      final result = await TransfersService(api: api).createOwnTransfer(
        sourceAccountId: 'a1',
        targetAccountId: 'a2',
        amountMinor: 25000,
      );

      expect(key, isNotNull);
      expect(key, isNotEmpty);
      expect(body!['amount_minor'], 25000);
      expect(result.status, 'SETTLED');
      expect(result.totalMinor, 25000);
      expect(result.replayed, isFalse);
    });

    test('reintento reutiliza la misma clave', () async {
      final keys = <String?>[];
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: {
            'data': {
              'transaction_id': 'tx-1',
              'type': 'OWN_TRANSFER',
              'status': 'SETTLED',
              'amount_minor': 25000,
              'currency': 'PEN',
              'fee_minor': 0,
            },
            'meta': {'replayed': true},
          },
        ),
        onRequest: (o) {
          keys.add(o.headers['Idempotency-Key']?.toString());
          return null;
        },
      );
      final service = TransfersService(api: api);
      await service.createOwnTransfer(
        sourceAccountId: 'a1',
        targetAccountId: 'a2',
        amountMinor: 25000,
        idempotencyKey: 'k-same',
      );
      await service.createOwnTransfer(
        sourceAccountId: 'a1',
        targetAccountId: 'a2',
        amountMinor: 25000,
        idempotencyKey: 'k-same',
      );

      expect(keys, ['k-same', 'k-same']);
    });

    test('interbank 202 parsea tracking con titular enmascarado', () async {
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 202,
          data: {
            'data': {
              'transaction_id': 'tx-ib-9',
              'type': 'INTERBANK_TRANSFER',
              'status': 'FUNDS_HELD',
              'source_account_id': 'a1',
              'amount_minor': 35000,
              'currency': 'PEN',
              'fee_minor': 350,
              'external_ref': 'ext-9',
              'tracking': {
                'poll_url': '/transactions/tx-ib-9',
                'status': 'FUNDS_HELD',
                'holder_name_masked': 'M*** R***',
              },
            },
            'meta': {'request_id': 'r9', 'replayed': false},
          },
        ),
      );

      final result = await TransfersService(api: api).createInterbankTransfer(
        sourceAccountId: 'a1',
        amountMinor: 35000,
        cci: '002-191-002847291044-58',
      );

      expect(result.status, 'FUNDS_HELD');
      expect(result.holderMasked, 'M*** R***');
      expect(result.totalMinor, 35350);
    });

    test('getReceipt parsea filename, operacion y bytes', () async {
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
      final api = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 'token'),
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) => handler.resolve(
            Response<List<int>>(
              requestOptions: options,
              statusCode: 200,
              data: [37, 80, 68, 70],
              headers: Headers.fromMap({
                'content-type': ['application/pdf'],
                'content-disposition': [
                  'attachment; filename="comprobante-OP-ABCDEF123456.pdf"',
                ],
              }),
            ),
          ),
        ),
      );

      final receipt = await TransfersService(api: api).getReceipt('tx-1');

      expect(receipt.suggestedFilename, 'comprobante-OP-ABCDEF123456.pdf');
      expect(receipt.operationNumber, 'OP-ABCDEF123456');
      expect(receipt.bytes, hasLength(4));
    });

    test('HOLDER_NOT_FOUND se propaga con codigo neutro', () async {
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
      final api = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 'token'),
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) => handler.reject(
            DioException(
              requestOptions: options,
              response: Response(
                requestOptions: options,
                statusCode: 404,
                data: {
                  'error': {
                    'code': 'HOLDER_NOT_FOUND',
                    'message': 'sin datos para la cuenta indicada',
                  },
                },
              ),
              type: DioExceptionType.badResponse,
            ),
          ),
        ),
      );

      try {
        await TransfersService(api: api).createInterbankTransfer(
          sourceAccountId: 'a1',
          amountMinor: 35000,
          cci: '00219100284729104458',
        );
        fail('debió lanzar ApiException');
      } on ApiException catch (e) {
        expect(e.code, 'HOLDER_NOT_FOUND');
      }
    });
  });

  group('Modelos y mensajes', () {
    test('dinero y CCI: formato, parseo y mascaras', () {
      expect(formatMoneyMinor(485020, 'PEN'), 'S/ 4,850.20');
      expect(formatMoneyMinor(25000, 'PEN'), 'S/ 250.00');
      expect(formatMoneyMinor(350, 'PEN'), 'S/ 3.50');
      expect(formatMoneyMinor(100000, 'USD'), r'$ 1,000.00');

      expect(parseAmountToMinor('350.00'), 35000);
      expect(parseAmountToMinor('350'), 35000);
      expect(parseAmountToMinor('0'), 0);
      expect(parseAmountToMinor('abc'), isNull);
      expect(parseAmountToMinor('10.123'), isNull);

      expect(normalizeCci('002-191-002847291044-58'), '00219100284729104458');
      expect(isValidCciFormat('002-191-002847291044-58'), isTrue);
      expect(isValidCciFormat('0021910028472910445'), isFalse);
      expect(isValidCciFormat('0021910028472910445A'), isFalse);
      expect(maskCciShort('00219100284729104458'), '•••• 4458');
    });

    test('transferUserMessage mapea sin PII', () {
      String msg(String code) =>
          transferUserMessage(ApiException(code: code, message: 'srv'));

      expect(msg('INSUFFICIENT_FUNDS'), contains('saldo suficiente'));
      expect(msg('HOLDER_NOT_FOUND'), contains('titular'));
      expect(msg('HOLDER_NOT_FOUND'), isNot(contains('002191')));
      expect(
        msg('HOLDER_LOOKUP_UNAVAILABLE'),
        contains('no disponible temporalmente'),
      );
      expect(msg('IDEMPOTENCY_CONFLICT'), contains('ya fue registrada'));
      expect(msg('TRANSFER_TIMEOUT'), contains('seguimiento'));
      expect(msg('NOT_FOUND'), contains('cuenta destino'));
    });
  });
}
