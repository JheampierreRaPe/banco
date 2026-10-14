import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/credits/data/credit_disbursement_service.dart';
import 'package:banca_online/features/credits/data/loan_schedule_service.dart';
import 'package:banca_online/features/credits/models/credit_contract.dart';
import 'package:banca_online/features/credits/models/loan_schedule.dart';
import 'package:banca_online/features/credits/presentation/credit_disburse_section.dart';
import 'package:banca_online/features/credits/presentation/credit_sign_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake de desembolso (seam `CreditDisbursementServiceBase`): sin red.
/// Registra `account_id` + `Idempotency-Key` para verificar el contrato
/// M-1 (`POST .../disburse` con clave obligatoria y cuenta elegida o
/// principal por defecto). El resultado lo decide el mock (cliente
/// delgado).
class FakeDisbursementService implements CreditDisbursementServiceBase {
  DisbursementResult result = const DisbursementResult(
    loanId: 'loan-9',
    status: 'SETTLED',
    applicationId: 'app-1',
    transactionId: 'tx-9',
    disbursedAt: '2026-10-13T10:00:00Z',
    accountMasked: '****4521',
    amountMinor: 1000000,
    currency: 'PEN',
  );
  ApiException? error;

  int calls = 0;
  String? lastApplicationId;
  String? lastAccountId;
  String? lastKey;

  @override
  Future<DisbursementResult> disburseApplication({
    required String applicationId,
    String? accountId,
    String? idempotencyKey,
  }) async {
    calls++;
    lastApplicationId = applicationId;
    lastAccountId = accountId;
    lastKey = idempotencyKey;
    final failure = error;
    if (failure != null) throw failure;
    return result;
  }
}

/// Fake de préstamos mínimo para la verificación "ya desembolsado".
class FakeDisburseLoansService implements LoanScheduleServiceBase {
  FakeDisburseLoansService(this.loans);

  final List<Loan> loans;

  @override
  Future<List<Loan>> listLoans() async => List.of(loans);

  @override
  Future<LoanSchedule> getSchedule(String loanId) =>
      throw UnimplementedError();

  @override
  Future<bool> setAutoDebit(String loanId, {required bool enabled}) =>
      throw UnimplementedError();

  @override
  Future<PayInstallmentResult> payNextInstallment(
    String loanId, {
    String? idempotencyKey,
  }) =>
      throw UnimplementedError();
}

const _loanForApp = Loan(
  id: 'loan-7',
  applicationId: 'app-1',
  principalMinor: 1000000,
  outstandingPrincipalMinor: 1000000,
  termMonths: 12,
  status: 'ACTIVE',
  autoDebitEnabled: false,
);

/// Fake de cuentas mínimo para el selector (solo `getAccounts`).
class FakeDisburseAccountsService implements AccountsServiceBase {
  FakeDisburseAccountsService(this.accounts);

  final List<Account> accounts;

  @override
  Future<List<Account>> getAccounts() async => List.of(accounts);

  @override
  Future<AccountsTotals> getAccountsTotals() =>
      throw UnimplementedError();

  @override
  Future<Account> getAccountDetail(String accountId) =>
      throw UnimplementedError();

  @override
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  }) =>
      throw UnimplementedError();

  @override
  Future<ExportResult> exportMovements(
    String accountId, {
    required String format,
    required DateTime dateFrom,
    required DateTime dateTo,
  }) =>
      throw UnimplementedError();
}

const _twoAccounts = [
  Account(
    id: 'acc-1',
    accountNumberMasked: '****4521',
    type: 'Ahorros',
    currency: 'PEN',
    availableMinor: 50000,
    heldMinor: 0,
    balanceMinor: 50000,
  ),
  Account(
    id: 'acc-2',
    accountNumberMasked: '****7777',
    type: 'Corriente',
    currency: 'PEN',
    availableMinor: 10000,
    heldMinor: 0,
    balanceMinor: 10000,
  ),
];

/// Servicio mockeado a nivel HTTP (mismo patrón que
/// `test/credit_loan_schedule_test.dart`).
class _Stub {
  _Stub(this.handler);

  final Response<dynamic> Function(RequestOptions options) handler;
  RequestOptions? lastOptions;

  ApiClient client() {
    final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
    final api = ApiClient.create(
      session: InMemorySessionRepository(initialAccessToken: 'token'),
      dioOverride: dio,
    );
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, h) {
          lastOptions = options;
          h.resolve(handler(options));
        },
      ),
    );
    return api;
  }
}

void main() {
  group('DisbursementResult (cliente delgado)', () {
    test('parsea el payload del servidor sin calcular', () {
      final result = DisbursementResult.fromJson({
        'loan_id': 'loan-9',
        'application_id': 'app-1',
        'transaction_id': 'tx-9',
        'status': 'SETTLED',
        'disbursed_at': '2026-10-13T10:00:00Z',
        'account_masked': '****4521',
        'amount_minor': 1000000,
        'currency': 'PEN',
      });

      expect(result.loanId, 'loan-9');
      expect(result.isSettled, isTrue);
      expect(result.accountMasked, '****4521');
      expect(result.amountMinor, 1000000);
      expect(result.replayed, isFalse);
    });

    test('replayed viaja en el flag, no en el cuerpo', () {
      final result = DisbursementResult.fromJson(
        const {'loan_id': 'l', 'status': 'SETTLED'},
        replayed: true,
      );
      expect(result.replayed, isTrue);
      expect(result.isSettled, isTrue);
    });
  });

  group('CreditDisbursementService (HTTP)', () {
    Map<String, dynamic> disburseJson() => {
          'loan_id': 'loan-9',
          'application_id': 'app-1',
          'transaction_id': 'tx-9',
          'status': 'SETTLED',
          'disbursed_at': '2026-10-13T10:00:00Z',
          'account_masked': '****4521',
          'amount_minor': 1000000,
          'currency': 'PEN',
        };

    test('envía POST .../disburse con Idempotency-Key y {account_id}',
        () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: {
            'data': disburseJson(),
            'meta': {'request_id': 'r1', 'replayed': false},
          },
        ),
      );
      final service = CreditDisbursementService(api: stub.client());

      final result = await service.disburseApplication(
        applicationId: 'app-1',
        accountId: 'acc-2',
        idempotencyKey: 'k-1',
      );

      expect(
        stub.lastOptions?.path,
        '/credits/applications/app-1/disburse',
      );
      expect(stub.lastOptions?.method, 'POST');
      expect(stub.lastOptions?.data, {'account_id': 'acc-2'});
      final headers = stub.lastOptions?.headers ?? {};
      expect(headers['Idempotency-Key'], 'k-1');
      expect(result.loanId, 'loan-9');
      expect(result.isSettled, isTrue);
      expect(result.replayed, isFalse);
    });

    test('sin cuenta elegida omite account_id (default a principal)',
        () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: {
            'data': disburseJson(),
            'meta': {'request_id': 'r1', 'replayed': false},
          },
        ),
      );
      final service = CreditDisbursementService(api: stub.client());

      await service.disburseApplication(applicationId: 'app-1');

      expect(stub.lastOptions?.data, isEmpty);
      expect(
        (stub.lastOptions?.headers ?? {}).containsKey('Idempotency-Key'),
        isTrue,
      );
    });

    test('replay del servidor se propaga sin romper', () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': disburseJson(),
            'meta': {'request_id': 'r1', 'replayed': true},
          },
        ),
      );
      final service = CreditDisbursementService(api: stub.client());

      final result = await service.disburseApplication(
        applicationId: 'app-1',
        accountId: 'acc-1',
      );

      expect(result.replayed, isTrue);
      expect(result.loanId, 'loan-9');
    });

    test('mensajes en español accionables (409/404/422)', () {
      expect(
        creditDisbursementUserMessage(
          ApiException(code: 'CONFLICT', message: 'x'),
        ),
        contains('aprobado y firmado'),
      );
      expect(
        creditDisbursementUserMessage(
          ApiException(code: 'IDEMPOTENCY_CONFLICT', message: 'x'),
        ),
        contains('ya fue registrada'),
      );
      expect(
        creditDisbursementUserMessage(
          ApiException(code: 'NOT_FOUND', message: 'x'),
        ),
        contains('No encontramos el expediente'),
      );
      expect(
        creditDisbursementUserMessage(
          ApiException(code: 'VALIDATION_ERROR', message: 'x'),
        ),
        contains('Verifica la cuenta'),
      );
    });
  });

  group('CreditDisburseSection', () {
    Future<void> pumpSection(
      WidgetTester tester, {
      required FakeDisbursementService disburse,
      List<Loan> loans = const [],
      bool noLoansService = false,
      List<Account>? accounts,
    }) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SingleChildScrollView(
              child: CreditDisburseSection(
                applicationId: 'app-1',
                disburseService: disburse,
                loansService:
                    noLoansService ? null : FakeDisburseLoansService(loans),
                accountsService: accounts == null
                    ? null
                    : FakeDisburseAccountsService(accounts),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
    }

    testWidgets('CTA visible cuando aplica (sin préstamo)', (tester) async {
      final fake = FakeDisbursementService();
      await pumpSection(tester, disburse: fake);

      expect(find.byKey(const Key('disburse-section')), findsOneWidget);
      expect(find.byKey(const Key('disburse-cta')), findsOneWidget);
      expect(find.text('Desembolsar a mi cuenta'), findsOneWidget);
      // Sin selector: default honesto a la principal.
      expect(find.byKey(const Key('disburse-note')), findsOneWidget);
    });

    testWidgets('éxito con default: envía sin account_id y con clave',
        (tester) async {
      final fake = FakeDisbursementService();
      await pumpSection(tester, disburse: fake);

      await tester.tap(find.byKey(const Key('disburse-cta')));
      await tester.pumpAndSettle();

      expect(fake.calls, 1);
      expect(fake.lastApplicationId, 'app-1');
      // Sin elección: se omite (el backend usa la principal).
      expect(fake.lastAccountId, isNull);
      expect(fake.lastKey, isNotNull);
      expect(find.byKey(const Key('disburse-success')), findsOneWidget);
      expect(find.text('¡Desembolso acreditado!'), findsOneWidget);
      // Monto y cuenta del servidor, sin PII en claro.
      expect(find.textContaining('S/ 10,000.00'), findsOneWidget);
      expect(find.textContaining('****4521'), findsOneWidget);
      expect(find.byKey(const Key('disburse-schedule-cta')), findsOneWidget);
      // Tras el éxito no se re-ofrece el desembolso.
      expect(find.byKey(const Key('disburse-cta')), findsNothing);
    });

    testWidgets('cuenta elegida viaja como {account_id}', (tester) async {
      final fake = FakeDisbursementService();
      await pumpSection(
        tester,
        disburse: fake,
        accounts: _twoAccounts,
      );

      expect(
        find.byKey(const Key('disburse-account-selector')),
        findsOneWidget,
      );
      await tester.tap(find.byKey(const Key('disburse-account-selector')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Corriente ****7777'));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('disburse-cta')));
      await tester.pumpAndSettle();

      expect(fake.calls, 1);
      expect(fake.lastAccountId, 'acc-2');
      expect(fake.lastKey, isNotNull);
      expect(find.byKey(const Key('disburse-success')), findsOneWidget);
    });

    testWidgets('sin CTA si ya desembolsado (card + cronograma)',
        (tester) async {
      await pumpSection(
        tester,
        disburse: FakeDisbursementService(),
        loans: [_loanForApp],
      );

      expect(find.byKey(const Key('disburse-cta')), findsNothing);
      expect(find.byKey(const Key('disburse-existing')), findsOneWidget);
      expect(find.text('Desembolso completado'), findsOneWidget);
      expect(find.byKey(const Key('disburse-schedule-cta')), findsOneWidget);
    });

    testWidgets('409 muestra aviso accionable con link a préstamos',
        (tester) async {
      final fake = FakeDisbursementService()
        ..error = ApiException(code: 'CONFLICT', message: 'no firmado');
      await pumpSection(tester, disburse: fake);

      await tester.tap(find.byKey(const Key('disburse-cta')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('disburse-error')), findsOneWidget);
      expect(find.textContaining('aprobado y firmado'), findsOneWidget);
      expect(find.byKey(const Key('disburse-loans-cta')), findsOneWidget);
      // La CTA sigue para reintentar con clave nueva.
      expect(find.byKey(const Key('disburse-cta')), findsOneWidget);
    });

    testWidgets('404 neutro sin prometer nada', (tester) async {
      final fake = FakeDisbursementService()
        ..error = ApiException(code: 'NOT_FOUND', message: 'ajeno');
      await pumpSection(tester, disburse: fake);

      await tester.tap(find.byKey(const Key('disburse-cta')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('disburse-error')), findsOneWidget);
      expect(
        find.textContaining('No encontramos el expediente'),
        findsOneWidget,
      );
    });

    testWidgets('sin servicio: aviso honesto sin CTA', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: SingleChildScrollView(
              child: CreditDisburseSection(applicationId: 'app-1'),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('disburse-unavailable')), findsOneWidget);
      expect(find.byKey(const Key('disburse-cta')), findsNothing);
    });

    testWidgets('éxito navega al cronograma del préstamo creado',
        (tester) async {
      final fake = FakeDisbursementService();
      final router = GoRouter(
        initialLocation: '/',
        routes: [
          GoRoute(
            path: '/',
            builder: (context, state) => Scaffold(
              body: SingleChildScrollView(
                child: CreditDisburseSection(
                  applicationId: 'app-1',
                  disburseService: fake,
                  loansService: FakeDisburseLoansService(const []),
                ),
              ),
            ),
          ),
          GoRoute(
            path: '/credits/loans/:id',
            builder: (context, state) => Scaffold(
              body: Text(
                'cronograma ${state.pathParameters['id']}',
              ),
            ),
          ),
        ],
      );
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('disburse-cta')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('disburse-schedule-cta')));
      await tester.pumpAndSettle();

      expect(find.text('cronograma loan-9'), findsOneWidget);
    });
  });

  group('CreditSignPage con desembolso (M-1)', () {
    /// La cola del `ListView` de firma es perezosa (mismo patrón que
    /// `credit_contract_sign_test.dart`): el éxito solo existe tras el
    /// scroll, igual que en uso real.
    Future<void> scrollSignToEnd(WidgetTester tester) async {
      await tester.drag(
        find.byKey(const Key('sign-content')),
        const Offset(0, -5000),
      );
      await tester.pumpAndSettle();
    }

    testWidgets('éxito de firma ofrece Desembolsar a mi cuenta',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: CreditSignPage(
            initialChallenge: const SignChallenge(
              applicationId: 'app-1',
              nonce: '',
              expiresIn: 0,
              status: 'SIGNED',
            ),
            disburseService: FakeDisbursementService(),
            loansService: FakeDisburseLoansService(const []),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await scrollSignToEnd(tester);

      expect(find.byKey(const Key('sign-success')), findsOneWidget);
      expect(find.byKey(const Key('disburse-cta')), findsOneWidget);
      // Los CTA existentes siguen (sin duplicar ni retirar).
      expect(find.byKey(const Key('sign-home-cta')), findsOneWidget);
      expect(find.byKey(const Key('sign-loans-cta')), findsOneWidget);
    });

    testWidgets('ya desembolsado: firma exitosa sin CTA de desembolso',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: CreditSignPage(
            initialChallenge: const SignChallenge(
              applicationId: 'app-1',
              nonce: '',
              expiresIn: 0,
              status: 'SIGNED',
            ),
            disburseService: FakeDisbursementService(),
            loansService: FakeDisburseLoansService([_loanForApp]),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await scrollSignToEnd(tester);

      expect(find.byKey(const Key('sign-success')), findsOneWidget);
      expect(find.byKey(const Key('disburse-cta')), findsNothing);
      expect(find.byKey(const Key('disburse-existing')), findsOneWidget);
    });
  });
}
