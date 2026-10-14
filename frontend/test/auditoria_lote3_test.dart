import 'dart:async';

import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/credits/data/credits_service.dart';
import 'package:banca_online/features/credits/data/my_credits_service.dart';
import 'package:banca_online/features/credits/models/credit.dart';
import 'package:banca_online/features/credits/models/credit_contract.dart';
import 'package:banca_online/features/credits/models/loan_application.dart';
import 'package:banca_online/features/credits/models/own_application.dart';
import 'package:banca_online/features/credits/presentation/credit_application_page.dart';
import 'package:banca_online/features/credits/presentation/credit_contract_page.dart';
import 'package:banca_online/features/credits/presentation/credit_sign_page.dart';
import 'package:banca_online/features/credits/presentation/credit_status_page.dart';
import 'package:banca_online/features/credits/presentation/credits_home_page.dart';
import 'package:banca_online/features/payment_authorization/presentation/payment_error_page.dart';
import 'package:banca_online/features/transfers/data/transfers_service.dart';
import 'package:banca_online/features/transfers/models/resolve_target.dart';
import 'package:banca_online/features/transfers/models/transfer.dart';
import 'package:banca_online/features/transfers/presentation/confirm_page.dart';
import 'package:banca_online/features/transfers/presentation/destination_page.dart';
import 'package:banca_online/features/transfers/presentation/tracking_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

// Pruebas de la auditoría Lote 3 (diseñador frontend): M12, m6, m7, M10,
// M11, m9, m10. i3-cronograma cambia en sprint-3 (C3): "Ver cronograma"
// navega a `/credits/loans` (E3-T21) en vez del aviso "Próximamente".

const _a1 = Account(
  id: 'a1',
  accountNumberMasked: '•••• 8921',
  type: 'SAVINGS',
  currency: 'PEN',
  availableMinor: 485020,
  heldMinor: 0,
  balanceMinor: 485020,
);

/// Fake mínimo de transferencias (solo lo que cada harness necesita).
class _FakeTransfers implements TransfersServiceBase {
  _FakeTransfers({this.accountsSeed = const [], this.ownResult});

  List<Account> accountsSeed;
  TransferResult? ownResult;

  @override
  Future<List<Account>> accounts() async => List.of(accountsSeed);

  @override
  Future<TransactionDetail> getTransaction(String transactionId) =>
      Completer<TransactionDetail>().future;

  @override
  Future<TransferResult> createOwnTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) async =>
      ownResult ??
      const TransferResult(
        transactionId: 'tx-1',
        type: 'OWN_TRANSFER',
        status: 'SETTLED',
        amountMinor: 25000,
        currency: 'PEN',
      );

  @override
  Future<List<Beneficiary>> beneficiaries({String? query}) =>
      throw UnimplementedError();

  @override
  Future<TransferResult> createThirdPartyTransfer({
    required String sourceAccountId,
    String? targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    int? feeMinor,
    String? beneficiaryId,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) =>
      throw UnimplementedError();

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
  }) =>
      throw UnimplementedError();

  @override
  Future<ResolvedTarget> resolveTarget({
    String? documentType,
    String? documentNumber,
    String? phone,
  }) =>
      throw UnimplementedError();

  @override
  Future<ReceiptResult> getReceipt(String transactionId) =>
      throw UnimplementedError();
}

TransferFlowArgs _ownArgs() {
  final draft = TransferDraft(
    kind: TransferKind.own,
    sourceAccountId: 'a1',
    targetAccountId: 'a2',
    beneficiaryAlias: 'Cuenta CuyCash Corriente',
    destinationMasked: '•••• 4521',
    amountMinor: 25000,
    idempotencyKey: 'k-test-1',
  );
  return TransferFlowArgs(draft: draft, source: _a1);
}

/// Fake mínimo del simulador (pestaña `Simulador` de la home).
class _FakeSimulatorService implements CreditsServiceBase {
  @override
  Future<List<LoanProduct>> products() async => [
        LoanProduct(
          id: 'p1',
          code: 'PERSONAL',
          name: 'Personal',
          tcea: '0.198',
          tea: '0.185',
          insuranceRate: '0.005',
          minAmountMinor: 100000,
          maxAmountMinor: 5000000,
          minTermMonths: 6,
          maxTermMonths: 36,
          allowedTerms: const [6, 12, 24, 36],
          enabled: true,
        ),
      ];

  @override
  Future<SimulationResult> simulate({
    required String productId,
    required int amountMinor,
    required int termMonths,
    required bool insurance,
  }) async =>
      SimulationResult(
        productId: productId,
        productCode: 'PERSONAL',
        productName: 'Personal',
        amountMinor: amountMinor,
        termMonths: termMonths,
        insurance: insurance,
        tea: '0.1850',
        tcea: '0.1980',
        paymentMinor: 92750,
        schedule: [
          ScheduleRow(
            installmentNo: 1,
            principalMinor: 76210,
            interestMinor: 16540,
            insuranceMinor: 0,
            totalMinor: 92750,
            balanceMinor: 923790,
          ),
        ],
      );

  @override
  Future<SimulationExport> exportSimulation({
    required String productId,
    required int amountMinor,
    required int termMonths,
    required bool insurance,
  }) async =>
      const SimulationExport(bytes: [1, 2, 3], suggestedFilename: 's.pdf');
}

/// Fake del listado "Mis créditos" (pestaña `Mis créditos` de la home).
class _FakeMine implements MyCreditsServiceBase {
  @override
  Future<OwnApplicationsPage> listMine({int? limit, int? offset}) async =>
      const OwnApplicationsPage(items: [], total: 0);
}

ContractBundle _bundle({required bool signed}) {
  return ContractBundle.fromJson({
    'application_id': 'app-1',
    'status': signed ? 'SIGNED' : 'GENERATED',
    'documents': [
      {
        'contract_id': 'c-1',
        'doc_type': 'CONTRATO',
        'doc': 'contrato',
        'doc_ref': 'ref-1',
        'doc_hash': 'hash-1',
        'status': signed ? 'SIGNED' : 'GENERATED',
      },
    ],
  });
}

Future<void> _pumpErrorView(
  WidgetTester tester,
  PaymentErrorKind kind,
) async {
  tester.view.physicalSize = const Size(800, 2200);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: PaymentErrorView(
          args: PaymentErrorArgs(
            kind: kind,
            recipientLabel: 'Destinatario',
            amountLabel: 'S/ 350.00',
            totalLabel: 'S/ 350.00',
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  // --M12_copy_honesto----------------------------------------------------------

  group('M12: CTAs de error de pago sin promesas falsas', () {
    testWidgets('insufficientFunds ofrece acciones reales', (tester) async {
      await _pumpErrorView(tester, PaymentErrorKind.insufficientFunds);

      expect(find.text('Volver y ajustar el monto'), findsOneWidget);
      expect(find.text('Volver al inicio'), findsOneWidget);
      expect(find.text('Recargar cuenta'), findsNothing);
      expect(find.text('Elegir otra cuenta de origen'), findsNothing);
    });

    testWidgets('dailyLimit ofrece acciones reales', (tester) async {
      await _pumpErrorView(tester, PaymentErrorKind.dailyLimit);

      expect(find.text('Ver movimientos'), findsOneWidget);
      expect(find.text('Volver al inicio'), findsOneWidget);
      expect(find.text('Ajustar límites de transferencia'), findsNothing);
    });

    testWidgets('preventiveBlock ofrece acciones reales', (tester) async {
      await _pumpErrorView(tester, PaymentErrorKind.preventiveBlock);

      expect(find.text('Ver movimientos'), findsOneWidget);
      expect(find.text('Volver al inicio'), findsOneWidget);
      expect(find.text('Verificar identidad con DNI'), findsNothing);
      expect(find.text('Hablar con asesor (WhatsApp)'), findsNothing);
    });
  });

  // --M12_rutas_reales----------------------------------------------------------

  group('M12: primaria de límite diario navega a /movements', () {
    GoRouter harness(_FakeTransfers fake) => GoRouter(
          initialLocation: '/test-confirm',
          routes: [
            GoRoute(
              path: '/test-confirm',
              builder: (context, state) =>
                  ConfirmPage(args: _ownArgs(), service: fake),
            ),
            GoRoute(
              path: '/movements',
              builder: (context, state) =>
                  const Scaffold(body: Text('Movements test')),
            ),
            GoRoute(
              path: '/home',
              builder: (context, state) =>
                  const Scaffold(body: Text('Home test')),
            ),
          ],
        );

    Future<void> pumpRejected(WidgetTester tester, _FakeTransfers fake) async {
      tester.view.physicalSize = const Size(800, 2200);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      await tester.pumpWidget(
        MaterialApp.router(routerConfig: harness(fake)),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();
      expect(find.text('Superaste tu límite diario'), findsOneWidget);
    }

    testWidgets('primaria Ver movimientos va a /movements', (tester) async {
      final fake = _FakeTransfers(
        ownResult: const TransferResult(
          transactionId: 'tx-2',
          type: 'OWN_TRANSFER',
          status: 'REJECTED',
          amountMinor: 35000,
          currency: 'PEN',
          feeMinor: 350,
          failureCode: 'DAILY_LIMIT_EXCEEDED',
        ),
      );
      await pumpRejected(tester, fake);

      await tester.tap(find.text('Ver movimientos'));
      await tester.pumpAndSettle();

      expect(find.text('Movements test'), findsOneWidget);
    });

    testWidgets('secundaria Volver al inicio va a /home', (tester) async {
      final fake = _FakeTransfers(
        ownResult: const TransferResult(
          transactionId: 'tx-2',
          type: 'OWN_TRANSFER',
          status: 'REJECTED',
          amountMinor: 35000,
          currency: 'PEN',
          feeMinor: 350,
          failureCode: 'DAILY_LIMIT_EXCEEDED',
        ),
      );
      await pumpRejected(tester, fake);

      await tester.tap(find.text('Volver al inicio'));
      await tester.pumpAndSettle();

      expect(find.text('Home test'), findsOneWidget);
    });
  });

  // --m6_indicador_dinamico-------------------------------------------------------

  group('m6: seguimiento sin progreso falso', () {
    testWidgets('muestra estado real y no checklist fija', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: TrackingPage(
            args: const TrackingArgs(transactionId: 'tx-ib-1'),
            service: _FakeTransfers(),
          ),
        ),
      );
      await tester.pump();

      expect(find.byKey(const Key('tracking-loading')), findsOneWidget);
      expect(find.byKey(const Key('tracking-status-line')), findsOneWidget);
      expect(find.text('Validando fondos'), findsNothing);
      expect(find.text('Registrando la operación'), findsNothing);
      expect(find.text('Notificando al destinatario'), findsNothing);
    });
  });

  // --m7_destino------------------------------------------------------------------

  group('m7: destino sin cuentas va a /home', () {
    testWidgets('Ir al inicio navega a /home', (tester) async {
      final fake = _FakeTransfers(accountsSeed: const []);
      final router = GoRouter(
        initialLocation: '/transfers/destination',
        routes: [
          GoRoute(
            path: '/transfers/destination',
            builder: (context, state) =>
                DestinationPage(kind: TransferKind.own, service: fake),
          ),
          GoRoute(
            path: '/home',
            builder: (context, state) =>
                const Scaffold(body: Text('Home test')),
          ),
        ],
      );
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(find.textContaining('Aún no tienes cuentas'), findsOneWidget);

      await tester.tap(find.text('Ir al inicio'));
      await tester.pumpAndSettle();

      expect(find.text('Home test'), findsOneWidget);
    });
  });

  // --M10_dato_de_ejemplo-----------------------------------------------------------

  group('M10: solicitud declara el dato de ejemplo', () {
    testWidgets('por defecto etiqueta documento y cuenta como ejemplo',
        (tester) async {
      await tester.pumpWidget(
        const MaterialApp(home: CreditApplicationPage()),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('Dato de ejemplo'), findsNWidgets(2));
      expect(find.text('DNI •••• 4521 (verificado)'), findsNothing);
    });

    testWidgets('con fuente real no muestra el aviso', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: CreditApplicationPage(
            documentMasked: 'DNI •••• 1234',
            disburseAccountMasked: 'Ahorros •••• 5678',
            isExampleData: false,
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('DNI •••• 1234'), findsOneWidget);
      expect(find.text('Ahorros •••• 5678'), findsOneWidget);
      expect(find.textContaining('Dato de ejemplo'), findsNothing);
    });
  });

  // --M11_contrato_firmado-----------------------------------------------------------

  group('M11: contrato firmado sin CTA de firmar', () {
    testWidgets('firmado muestra Volver al inicio, no Firmar', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: CreditContractPage(initialBundle: _bundle(signed: true)),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('contract-content')), findsOneWidget);
      expect(find.text('Firmado'), findsOneWidget);
      expect(find.byKey(const Key('contract-sign-cta')), findsNothing);
      expect(find.byKey(const Key('contract-home-cta')), findsOneWidget);
    });

    testWidgets('no firmado conserva Firmar contrato', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: CreditContractPage(initialBundle: _bundle(signed: false)),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('contract-sign-cta')), findsOneWidget);
      expect(find.byKey(const Key('contract-home-cta')), findsNothing);
    });
  });

  // --m9_simulador_con_estado----------------------------------------------------------

  group('m9: cambiar de pestaña conserva el simulador', () {
    testWidgets('IndexedStack mantiene el simulador al volver', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: CreditsHomePage(
            simulatorService: _FakeSimulatorService(),
            myCreditsService: _FakeMine(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Simula tu crédito'), findsOneWidget);
      expect(find.byType(IndexedStack), findsOneWidget);

      // A Mis créditos: el simulador sigue montado (fuera de escena).
      await tester.tap(find.text('Mis créditos').first);
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('credits-simulator'), skipOffstage: false),
        findsOneWidget,
      );

      // De vuelta: el simulador sigue ahí, sin re-simular de cero.
      await tester.tap(find.text('Simulador').first);
      await tester.pumpAndSettle();
      expect(find.text('Simula tu crédito'), findsOneWidget);
      expect(find.byKey(const Key('credits-simulator')), findsOneWidget);
    });
  });

  // --m10_tilde--------------------------------------------------------------------------

  group('m10: copy con tilde e interrogación', () {
    testWidgets('firma muestra ¿Sin biometría?', (tester) async {
      tester.view.physicalSize = const Size(800, 2200);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      await tester.pumpWidget(
        const MaterialApp(
          home: CreditSignPage(
            initialChallenge: SignChallenge(
              applicationId: 'app-1',
              nonce: 'nonce-1',
              expiresIn: 120,
              status: 'GENERATED',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('¿Sin biometría? Usa tu PIN'), findsOneWidget);
      expect(find.text('Sin biometría? Usa tu PIN'), findsNothing);
    });
  });

  // --i3_cronograma-----------------------------------------------------------------------

  group('i3-cronograma (C3 sprint-3): Ver cronograma va a /credits/loans', () {
    testWidgets('navega a Mis prestamos sin aviso', (tester) async {
      tester.view.physicalSize = const Size(800, 2200);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      final router = GoRouter(
        initialLocation: '/status',
        routes: [
          GoRoute(
            path: '/status',
            builder: (context, state) => const CreditStatusPage(
              initialResult: LoanApplicationResult(
                id: 'app-1',
                status: LoanApplicationStatus.approved,
              ),
            ),
          ),
          GoRoute(
            path: '/credits/loans',
            builder: (context, state) =>
                const Scaffold(body: Text('Prestamos (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(find.text('Ver cronograma'), findsOneWidget);
      await tester.tap(find.text('Ver cronograma'));
      await tester.pumpAndSettle();

      // C3: el cronograma definitivo vive en "Mis préstamos" (E3-T21):
      // navega a la lista, sin `comingSoon`.
      expect(find.text('Prestamos (fake)'), findsOneWidget);
      expect(find.textContaining('próximamente'), findsNothing);
    });
  });
}
