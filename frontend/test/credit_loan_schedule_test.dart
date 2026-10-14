import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/credits/credits_routes.dart';
import 'package:banca_online/features/credits/data/loan_schedule_service.dart';
import 'package:banca_online/features/credits/models/loan_schedule.dart';
import 'package:banca_online/features/credits/presentation/loan_list_page.dart';
import 'package:banca_online/features/credits/presentation/loan_schedule_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del servicio de préstamos (seam `LoanScheduleServiceBase`): sin red.
/// Los montos/estados los decide el mock (cliente delgado, E3-T21).
class FakeLoanScheduleService implements LoanScheduleServiceBase {
  FakeLoanScheduleService({List<Loan>? loans, LoanSchedule? schedule})
      : loans = loans ?? _twoLoans(),
        schedule = schedule ?? _schedule(autoDebit: false);

  List<Loan> loans;
  LoanSchedule? schedule;
  ApiException? error;
  int listCalls = 0;
  int scheduleCalls = 0;
  int autoDebitCalls = 0;
  bool? lastAutoDebitEnabled;
  int payCalls = 0;
  String? lastIdempotencyKey;
  PayInstallmentResult? payResult;

  FakeLoanScheduleService.empty()
      : loans = const [],
        schedule = null;

  static List<Loan> _twoLoans() => const [
        Loan(
          id: 'loan-aaa-1111',
          applicationId: 'app-1',
          principalMinor: 1000000,
          outstandingPrincipalMinor: 923790,
          termMonths: 12,
          status: 'ACTIVE',
          autoDebitEnabled: false,
          tcea: '0.198',
          disbursedAt: '2026-10-12T10:00:00Z',
        ),
        Loan(
          id: 'loan-bbb-2222',
          applicationId: 'app-2',
          principalMinor: 500000,
          outstandingPrincipalMinor: 0,
          termMonths: 6,
          status: 'PAID',
          autoDebitEnabled: true,
          tcea: '0.150',
          disbursedAt: '2026-09-01T10:00:00Z',
        ),
      ];

  /// Cronograma del mock del backend: los totales son del servidor (la UI no
  /// los suma: 762.10 + 165.40 + 0.00 = 927.50 solo se MUESTRA).
  static LoanSchedule _schedule({required bool autoDebit}) => LoanSchedule(
        loan: Loan(
          id: 'loan-aaa-1111',
          applicationId: 'app-1',
          principalMinor: 1000000,
          outstandingPrincipalMinor: 923790,
          termMonths: 12,
          status: 'ACTIVE',
          autoDebitEnabled: autoDebit,
        ),
        installments: const [
          LoanInstallment(
            installmentNo: 1,
            dueDate: '2026-11-12',
            principalMinor: 76210,
            interestMinor: 16540,
            insuranceMinor: 0,
            totalMinor: 92750,
            status: LoanInstallmentStatus.pending,
          ),
          LoanInstallment(
            installmentNo: 2,
            dueDate: '2026-10-12',
            principalMinor: 70000,
            interestMinor: 20000,
            insuranceMinor: 2750,
            totalMinor: 92750,
            status: LoanInstallmentStatus.overdue,
          ),
          LoanInstallment(
            installmentNo: 3,
            dueDate: '2026-09-12',
            principalMinor: 75000,
            interestMinor: 17000,
            insuranceMinor: 750,
            totalMinor: 92750,
            status: LoanInstallmentStatus.paid,
            paidAt: '2026-09-12T10:00:00Z',
          ),
        ],
      );

  static LoanSchedule _allPaid() => LoanSchedule(
        loan: const Loan(
          id: 'loan-bbb-2222',
          applicationId: 'app-2',
          principalMinor: 500000,
          outstandingPrincipalMinor: 0,
          termMonths: 6,
          status: 'PAID',
          autoDebitEnabled: true,
        ),
        installments: const [
          LoanInstallment(
            installmentNo: 1,
            dueDate: '2026-09-01',
            principalMinor: 80000,
            interestMinor: 3000,
            insuranceMinor: 375,
            totalMinor: 83375,
            status: LoanInstallmentStatus.paid,
            paidAt: '2026-09-01T10:00:00Z',
          ),
        ],
      );

  @override
  Future<List<Loan>> listLoans() async {
    listCalls++;
    final err = error;
    if (err != null) throw err;
    return List.of(loans);
  }

  @override
  Future<LoanSchedule> getSchedule(String loanId) async {
    scheduleCalls++;
    final err = error;
    if (err != null) throw err;
    return schedule ?? _schedule(autoDebit: false);
  }

  @override
  Future<bool> setAutoDebit(String loanId, {required bool enabled}) async {
    autoDebitCalls++;
    lastAutoDebitEnabled = enabled;
    final err = error;
    if (err != null) throw err;
    // El servidor confirma: la UI refleja el valor devuelto.
    final current = schedule;
    if (current != null) {
      schedule = LoanSchedule(
        loan: Loan(
          id: current.loan.id,
          applicationId: current.loan.applicationId,
          principalMinor: current.loan.principalMinor,
          outstandingPrincipalMinor: current.loan.outstandingPrincipalMinor,
          termMonths: current.loan.termMonths,
          status: current.loan.status,
          autoDebitEnabled: enabled,
          tcea: current.loan.tcea,
          disbursedAt: current.loan.disbursedAt,
          createdAt: current.loan.createdAt,
        ),
        installments: current.installments,
      );
    }
    return enabled;
  }

  @override
  Future<PayInstallmentResult> payNextInstallment(
    String loanId, {
    String? idempotencyKey,
  }) async {
    payCalls++;
    lastIdempotencyKey = idempotencyKey;
    final err = error;
    if (err != null) throw err;
    return payResult ??
        const PayInstallmentResult(
          loanId: 'loan-aaa-1111',
          scheduleId: 'sch-1',
          transactionId: 'tx-1',
          status: 'SETTLED',
          amountMinor: 92750,
          outstandingPrincipalMinor: 847580,
          paidAt: '2026-10-13T10:00:00Z',
        );
  }
}

/// Fake colgado (estado de carga).
class HangingLoanService extends FakeLoanScheduleService {
  HangingLoanService() : super(loans: const [], schedule: null);

  @override
  Future<List<Loan>> listLoans() => Completer<List<Loan>>().future;

  @override
  Future<LoanSchedule> getSchedule(String loanId) =>
      Completer<LoanSchedule>().future;
}

/// Servicio mockeado a nivel HTTP (mismo patrón que
/// `test/credit_mine_test.dart`).
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

Never _reject(RequestOptions o, int status, Map<String, dynamic> body) {
  throw DioException(
    requestOptions: o,
    response: Response(requestOptions: o, statusCode: status, data: body),
    type: DioExceptionType.badResponse,
  );
}

Map<String, dynamic> _loanJson({String id = 'loan-1'}) => {
      'id': id,
      'application_id': 'app-1',
      'account_id': 'acc-1',
      'principal_minor': 1000000,
      'outstanding_principal_minor': 923790,
      'tcea': '0.198',
      'term_months': 12,
      'status': 'ACTIVE',
      'disbursed_at': '2026-10-12T10:00:00Z',
      'auto_debit_enabled': false,
      'created_at': '2026-10-12T10:00:00Z',
    };

Map<String, dynamic> _rowJson({int no = 1, String status = 'PENDING'}) => {
      'installment_no': no,
      'due_date': '2026-11-12',
      'principal_minor': 76210,
      'interest_minor': 16540,
      'insurance_minor': 0,
      'total_minor': 92750,
      'status': status,
      'paid_at': null,
    };

void main() {
  group('Modelos (cliente delgado)', () {
    test('estados de cuota con etiquetas ES y parseo del servidor', () {
      expect(LoanInstallmentStatus.fromServer('PENDING').label, 'Pendiente');
      expect(LoanInstallmentStatus.fromServer('PARTIAL').label, 'Parcial');
      expect(LoanInstallmentStatus.fromServer('PAID').label, 'Pagada');
      expect(LoanInstallmentStatus.fromServer('OVERDUE').label, 'Vencida');
      // Desconocido colapsa a pendiente (el servidor decide el pago real).
      expect(LoanInstallmentStatus.fromServer('WEIRD').label, 'Pendiente');
    });

    test('solo Pendiente/Parcial/Vencida admiten pago manual', () {
      expect(LoanInstallmentStatus.pending.isPayable, isTrue);
      expect(LoanInstallmentStatus.partial.isPayable, isTrue);
      expect(LoanInstallmentStatus.overdue.isPayable, isTrue);
      expect(LoanInstallmentStatus.paid.isPayable, isFalse);
    });

    test('Loan parsea el payload del servidor', () {
      final loan = Loan.fromJson(_loanJson());

      expect(loan.id, 'loan-1');
      expect(loan.principalMinor, 1000000);
      expect(loan.outstandingPrincipalMinor, 923790);
      expect(loan.termMonths, 12);
      expect(loan.statusLabel, 'Activo');
      expect(loan.autoDebitEnabled, isFalse);
    });

    test('LoanSchedule parsea loan + schedule y detecta pago pendiente', () {
      final schedule = LoanSchedule.fromJson({
        'loan': _loanJson(),
        'schedule': [_rowJson(), _rowJson(no: 2, status: 'PAID')],
      });

      expect(schedule.installments, hasLength(2));
      expect(schedule.installments.first.totalMinor, 92750);
      expect(schedule.hasPayable, isTrue);

      final allPaid = LoanSchedule.fromJson({
        'loan': _loanJson(),
        'schedule': [_rowJson(status: 'PAID')],
      });
      expect(allPaid.hasPayable, isFalse);
    });
  });

  group('LoanScheduleService (HTTP)', () {
    test('listLoans usa GET /credits/loans sin Idempotency-Key', () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': [_loanJson(), _loanJson(id: 'loan-2')],
            'meta': {'total': 2},
          },
        ),
      );
      final service = LoanScheduleService(api: stub.client());

      final loans = await service.listLoans();

      expect(loans, hasLength(2));
      expect(stub.lastOptions?.path, '/credits/loans');
      expect(stub.lastOptions?.method, 'GET');
      expect(
        (stub.lastOptions?.headers ?? {}).containsKey('Idempotency-Key'),
        isFalse,
      );
    });

    test('getSchedule usa GET .../schedule y parsea loan + filas', () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': {
              'loan': _loanJson(),
              'schedule': [_rowJson(), _rowJson(no: 2, status: 'OVERDUE')],
            },
            'meta': {'request_id': 'r1'},
          },
        ),
      );
      final service = LoanScheduleService(api: stub.client());

      final schedule = await service.getSchedule('loan-1');

      expect(stub.lastOptions?.path, '/credits/loans/loan-1/schedule');
      expect(schedule.loan.outstandingPrincipalMinor, 923790);
      expect(schedule.installments, hasLength(2));
      expect(schedule.installments[1].status.label, 'Vencida');
      expect(
        (stub.lastOptions?.headers ?? {}).containsKey('Idempotency-Key'),
        isFalse,
      );
    });

    test('setAutoDebit usa PUT con {enabled} (contrato E3-T19)', () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': {'loan_id': 'loan-1', 'auto_debit_enabled': true},
            'meta': {'request_id': 'r1'},
          },
        ),
      );
      final service = LoanScheduleService(api: stub.client());

      final confirmed =
          await service.setAutoDebit('loan-1', enabled: true);

      expect(confirmed, isTrue);
      expect(stub.lastOptions?.method, 'PUT');
      expect(stub.lastOptions?.path, '/credits/loans/loan-1/auto-debit');
      // Cuerpo `{enabled}` (no `auto_debit_enabled`) y sin clave.
      expect(stub.lastOptions?.data, {'enabled': true});
      expect(
        (stub.lastOptions?.headers ?? {}).containsKey('Idempotency-Key'),
        isFalse,
      );
    });

    test('payNext usa POST .../pay CON Idempotency-Key y sin schedule_id',
        () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': {
              'loan_id': 'loan-1',
              'schedule_id': 'sch-1',
              'transaction_id': 'tx-1',
              'status': 'SETTLED',
              'amount_minor': 92750,
              'outstanding_principal_minor': 847580,
              'paid_at': '2026-10-13T10:00:00Z',
            },
            'meta': {'request_id': 'r1', 'replayed': false},
          },
        ),
      );
      final service = LoanScheduleService(api: stub.client());

      final result = await service.payNextInstallment('loan-1');

      expect(stub.lastOptions?.path, '/credits/loans/loan-1/pay');
      expect(
        (stub.lastOptions?.headers ?? {}).containsKey('Idempotency-Key'),
        isTrue,
      );
      expect(result.isSettled, isTrue);
      expect(result.amountMinor, 92750);
      expect(result.outstandingPrincipalMinor, 847580);
    });

    test('payNext propaga REJECTED + INSUFFICIENT_FUNDS sin romper', () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': {
              'loan_id': 'loan-1',
              'schedule_id': 'sch-1',
              'transaction_id': 'tx-1',
              'status': 'REJECTED',
              'amount_minor': 92750,
              'outstanding_principal_minor': 923790,
              'paid_at': null,
              'failure_code': 'INSUFFICIENT_FUNDS',
            },
            'meta': {'request_id': 'r1'},
          },
        ),
      );
      final service = LoanScheduleService(api: stub.client());

      final result = await service.payNextInstallment('loan-1');

      expect(result.isSettled, isFalse);
      expect(result.isInsufficientFunds, isTrue);
    });

    test('errores 404/409 mapeados a mensajes accionables', () async {
      final api404 = _Stub(
        (o) => _reject(o, 404, {
          'error': {'code': 'NOT_FOUND', 'message': 'ajeno'}
        }),
      ).client();
      await expectLater(
        () => LoanScheduleService(api: api404).getSchedule('x'),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'NOT_FOUND'),
        ),
      );
      expect(
        loanScheduleUserMessage(
          ApiException(code: 'NOT_FOUND', message: 'x'),
        ),
        contains('No encontramos el préstamo'),
      );
      expect(
        loanScheduleUserMessage(
          ApiException(code: 'CONFLICT', message: 'x'),
        ),
        contains('ya fue pagada'),
      );
      expect(
        loanScheduleUserMessage(
          ApiException(code: 'INSUFFICIENT_FUNDS', message: 'x'),
        ),
        contains('Saldo insuficiente'),
      );
    });
  });

  group('LoanListPage', () {
    testWidgets('contenido: filas con montos del servidor', (tester) async {
      await tester.pumpWidget(
        MaterialApp(home: LoanListPage(service: FakeLoanScheduleService())),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('loan-list-content')), findsOneWidget);
      expect(
        find.text('Préstamo • S/ 10,000.00 • 12 m'),
        findsOneWidget,
      );
      expect(find.textContaining('Saldo de capital:'), findsNWidgets(2));
      expect(find.text('Activo'), findsOneWidget);
      expect(find.text('Débito automático desactivado'), findsOneWidget);
    });

    testWidgets('vacío con CTA al simulador', (tester) async {
      await tester.pumpWidget(
        MaterialApp(home: LoanListPage(service: FakeLoanScheduleService.empty())),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('loan-list-empty')), findsOneWidget);
      expect(find.text('Aún no tienes préstamos desembolsados.'), findsNothing);
      expect(find.textContaining('Aún no tienes préstamos'), findsOneWidget);
    });

    testWidgets('carga', (tester) async {
      await tester.pumpWidget(
        MaterialApp(home: LoanListPage(service: HangingLoanService())),
      );
      await tester.pump();

      expect(find.byKey(const Key('loan-list-loading')), findsOneWidget);
    });

    testWidgets('error accionable con Reintentar', (tester) async {
      final fake = FakeLoanScheduleService()..error = ApiException.network();
      await tester.pumpWidget(MaterialApp(home: LoanListPage(service: fake)));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('loan-list-error')), findsOneWidget);

      fake.error = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('loan-list-content')), findsOneWidget);
    });

    testWidgets('sin servicio: error accionable', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(home: LoanListPage(service: null)),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('loan-list-error')), findsOneWidget);
    });

    testWidgets('Ver cronograma navega a /credits/loans/:id', (tester) async {
      final fake = FakeLoanScheduleService();
      loanScheduleServiceFactory = () => fake;
      addTearDown(() => loanScheduleServiceFactory = null);
      final router = GoRouter(
        initialLocation: '/credits/loans',
        routes: [...creditsRoutes],
      );
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('loan-schedule-loan-aaa-1111')));
      await tester.pumpAndSettle();

      expect(find.text('Cronograma del préstamo'), findsOneWidget);
      expect(find.byKey(const Key('loan-schedule-content')), findsOneWidget);
    });
  });

  group('LoanSchedulePage', () {
    Future<void> pump(
      WidgetTester tester,
      FakeLoanScheduleService fake, {
      String loanId = 'loan-aaa-1111',
    }) async {
      await tester.pumpWidget(
        MaterialApp(
          home: LoanSchedulePage(service: fake, loanId: loanId),
        ),
      );
      await tester.pumpAndSettle();
    }

    testWidgets('contenido: saldo + filas con montos del servidor',
        (tester) async {
      await pump(tester, FakeLoanScheduleService());

      expect(find.byKey(const Key('loan-schedule-content')), findsOneWidget);
      // Saldo de capital del servidor (sin recalcular).
      expect(
        find.text('Saldo de capital: S/ 9,237.90'),
        findsOneWidget,
      );
      // Filas con N, vencimiento, componentes y total del servidor.
      expect(find.text('Cuota 1 • Vence 2026-11-12'), findsOneWidget);
      expect(find.text('Total: S/ 927.50'), findsNWidgets(3));
      expect(
        find.text('Capital S/ 762.10 • Interés S/ 165.40 • Seguro S/ 0.00'),
        findsOneWidget,
      );
      // Badges con los 3 estados presentes.
      expect(find.text('Pendiente'), findsOneWidget);
      expect(find.text('Vencida'), findsOneWidget);
      expect(find.text('Pagada'), findsOneWidget);
    });

    testWidgets('toggle de débito refleja el servidor', (tester) async {
      final fake = FakeLoanScheduleService();
      await pump(tester, fake);

      final toggle =
          find.byKey(const Key('loan-schedule-auto-debit'));
      expect(toggle, findsOneWidget);
      expect(fake.autoDebitCalls, 0);

      await tester.tap(toggle);
      await tester.pumpAndSettle();

      expect(fake.autoDebitCalls, 1);
      expect(fake.lastAutoDebitEnabled, isTrue);
      expect(find.text('Débito automático activado.'), findsOneWidget);
      // El switch refleja el valor confirmado por el servidor.
      final sw = tester.widget<Switch>(toggle);
      expect(sw.value, isTrue);
    });

    testWidgets('pago manual: éxito actualiza el cronograma', (tester) async {
      final fake = FakeLoanScheduleService();
      await pump(tester, fake);
      expect(fake.scheduleCalls, 1);

      await tester.tap(find.byKey(const Key('loan-schedule-pay-cta')));
      await tester.pumpAndSettle();

      expect(fake.payCalls, 1);
      // Clave de idempotencia generada para el intento.
      expect(fake.lastIdempotencyKey, isNotNull);
      expect(find.textContaining('Cuota pagada.'), findsOneWidget);
      // Tras cobrar, recarga el cronograma (nuevo saldo del servidor).
      expect(fake.scheduleCalls, 2);
    });

    testWidgets('pago sin fondos: mensaje accionable, cuota intacta',
        (tester) async {
      final fake = FakeLoanScheduleService()
        ..payResult = const PayInstallmentResult(
          loanId: 'loan-aaa-1111',
          scheduleId: 'sch-1',
          transactionId: 'tx-1',
          status: 'REJECTED',
          amountMinor: 92750,
          outstandingPrincipalMinor: 923790,
          failureCode: 'INSUFFICIENT_FUNDS',
        );
      await pump(tester, fake);

      await tester.tap(find.byKey(const Key('loan-schedule-pay-cta')));
      await tester.pumpAndSettle();

      expect(find.textContaining('Saldo insuficiente'), findsOneWidget);
      // Sin recarga: la cuota no cambió.
      expect(fake.scheduleCalls, 1);
    });

    testWidgets('todo pagado: sin CTA de pago', (tester) async {
      final fake = FakeLoanScheduleService(
        schedule: FakeLoanScheduleService._allPaid(),
      );
      await pump(tester, fake, loanId: 'loan-bbb-2222');

      expect(find.byKey(const Key('loan-schedule-pay-cta')), findsNothing);
      expect(find.text('Pagada'), findsOneWidget);
    });

    testWidgets('error 409 del toggle: mensaje sin cambiar el switch',
        (tester) async {
      final fake = FakeLoanScheduleService()
        ..error = ApiException(code: 'CONFLICT', message: 'x');
      // El fetch inicial falla primero: se reintenta con servicio sano para
      // el contenido y se rompe solo el toggle.
      await pump(tester, fake);
      expect(find.byKey(const Key('loan-schedule-error')), findsOneWidget);

      fake.error = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('loan-schedule-content')), findsOneWidget);

      fake.error = ApiException(code: 'CONFLICT', message: 'ya pagada');
      await tester.tap(find.byKey(const Key('loan-schedule-auto-debit')));
      await tester.pumpAndSettle();

      expect(find.textContaining('ya fue pagada'), findsOneWidget);
      final sw =
          tester.widget<Switch>(find.byKey(const Key('loan-schedule-auto-debit')));
      expect(sw.value, isFalse);
    });

    testWidgets('carga', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: LoanSchedulePage(service: HangingLoanService(), loanId: 'x'),
        ),
      );
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.byKey(const Key('loan-schedule-loading')), findsOneWidget);
    });

    testWidgets('sin servicio: error accionable', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(home: LoanSchedulePage(service: null)),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('loan-schedule-error')), findsOneWidget);
    });
  });
}
