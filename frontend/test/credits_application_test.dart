import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/credits/credits_routes.dart';
import 'package:banca_online/features/credits/data/credit_applications_service.dart';
import 'package:banca_online/features/credits/models/credit.dart';
import 'package:banca_online/features/credits/models/loan_application.dart';
import 'package:banca_online/features/credits/presentation/credit_application_page.dart';
import 'package:banca_online/features/credits/presentation/credit_status_page.dart';
import 'package:banca_online/features/credits/utils/format.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del expediente (seam `CreditApplicationsServiceBase`): sin red, con
/// shapes `docs/05` §6.4 (`{data, meta}`). El dictamen lo decide el mock, no
/// la UI (cliente delgado, E3-T09).
class FakeCreditApplicationsService
    implements CreditApplicationsServiceBase {
  FakeCreditApplicationsService({LoanApplicationResult? submitResult})
      : submitResult = submitResult ?? _approved('app-1');

  LoanApplicationResult submitResult;
  ApiException? submitError;
  LoanApplicationResult? statusResult;
  ApiException? statusError;

  int submitCalls = 0;
  int getCalls = 0;
  final List<String?> idempotencyKeys = [];
  LoanApplicationRequest? lastRequest;
  String? lastStatusId;

  static LoanApplicationResult _approved(String id) {
    return const LoanApplicationResult(
      id: 'app-1',
      status: LoanApplicationStatus.approved,
      decisionReason: 'Score y capacidad de pago dentro de política.',
      requestedAmountMinor: 1000000,
      termMonths: 12,
      totalIncomeMinor: 350000,
      totalDebtMinor: 0,
    );
  }

  @override
  Future<LoanApplicationResult> submitApplication({
    required LoanApplicationRequest request,
    String? idempotencyKey,
  }) async {
    submitCalls++;
    lastRequest = request;
    idempotencyKeys.add(idempotencyKey);
    final error = submitError;
    if (error != null) throw error;
    return submitResult;
  }

  @override
  Future<LoanApplicationResult> getApplication(String id) async {
    getCalls++;
    lastStatusId = id;
    final error = statusError;
    if (error != null) throw error;
    return statusResult ?? submitResult;
  }
}

SimulationDraft _draft() => const SimulationDraft(
      productId: 'p1',
      productName: 'Personal',
      amountMinor: 1000000,
      termMonths: 12,
      insurance: false,
      paymentMinor: 92750,
    );

Future<void> _pumpApply(
  WidgetTester tester,
  FakeCreditApplicationsService fake, {
  SimulationDraft? draft,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: CreditApplicationPage(
        draft: draft ?? _draft(),
        service: fake,
      ),
    ),
  );
  await tester.pumpAndSettle();
}

/// Desplaza el `ListView` hasta que [finder] sea visible.
///
/// `scrollable` se fija en el primer `Scrollable` (el del `ListView`): sin
/// esto, los `Scrollable` internos de los `TextField` hacen ambiguo el
/// `find.byType(Scrollable)` por defecto.
Future<void> _reveal(WidgetTester tester, Finder finder) async {
  await tester.scrollUntilVisible(
    finder,
    400,
    scrollable: find.byType(Scrollable).first,
  );
  await tester.pumpAndSettle();
}

Future<void> _enterMoney(
  WidgetTester tester,
  Key fieldKey,
  String value,
) async {
  final field = find.descendant(
    of: find.byKey(fieldKey),
    matching: find.byType(TextField),
  );
  await _reveal(tester, field);
  await tester.enterText(field, value);
  await tester.pump();
}

Future<void> _fillValidIncome(WidgetTester tester) async {
  await _enterMoney(tester, const Key('credit-apply-income'), '3500.00');
}

/// Servicio mockeado a nivel HTTP: `dio` resuelve respuestas enlatadas sin
/// salir a red (mismo patron que `test/credits_simulator_test.dart`).
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

Map<String, dynamic> _envelope(Map<String, dynamic> data) => {
      'data': data,
      'meta': {'request_id': 'req-1'},
    };

Map<String, dynamic> _applicationJson({
  String id = 'app-1',
  String status = 'APPROVED',
  String? reason = 'Score y capacidad de pago dentro de política.',
}) =>
    {
      'id': id,
      'user_id': 'user-1',
      'product_id': 'p1',
      'requested_amount_minor': 1000000,
      'term_months': 12,
      'status': status,
      'bureau_authorized': true,
      'bureau_authorized_at': '2026-10-08T10:24:00Z',
      'score': 720,
      'dti_ratio': '0.3800',
      'total_income_minor': 350000,
      'total_debt_minor': 0,
      'decision_reason': reason,
      'created_at': '2026-10-08T10:24:00Z',
      'updated_at': '2026-10-08T10:24:00Z',
      'decided_at': '2026-10-08T10:24:05Z',
    };

void main() {
  group('parseAmountMinor', () {
    test('parsea "3,500.00" a centimos sin floats', () {
      expect(parseAmountMinor('3,500.00'), 350000);
      expect(parseAmountMinor('10,000.00'), 1000000);
      expect(parseAmountMinor('S/ 927.50'), 92750);
      expect(parseAmountMinor('12'), 1200);
      expect(parseAmountMinor('0.00'), 0);
    });

    test('rechaza vacio, negativo, letras y 3 decimales', () {
      expect(parseAmountMinor(''), isNull);
      expect(parseAmountMinor('   '), isNull);
      expect(parseAmountMinor('-5.00'), isNull);
      expect(parseAmountMinor('abc'), isNull);
      expect(parseAmountMinor('10.005'), isNull);
      expect(parseAmountMinor('1,0,0.x'), isNull);
    });
  });

  group('modelos', () {
    test('fromServer cubre los 7 estados del enum', () {
      expect(
        LoanApplicationStatus.fromServer('IN_EVALUATION'),
        LoanApplicationStatus.inEvaluation,
      );
      expect(
        LoanApplicationStatus.fromServer('APPROVED'),
        LoanApplicationStatus.approved,
      );
      expect(
        LoanApplicationStatus.fromServer('REJECTED'),
        LoanApplicationStatus.rejected,
      );
      expect(
        LoanApplicationStatus.fromServer('IN_REVIEW'),
        LoanApplicationStatus.inReview,
      );
      expect(
        LoanApplicationStatus.fromServer('DRAFT'),
        LoanApplicationStatus.draft,
      );
      expect(
        LoanApplicationStatus.fromServer('SUBMITTED'),
        LoanApplicationStatus.submitted,
      );
      expect(
        LoanApplicationStatus.fromServer('EXPIRED'),
        LoanApplicationStatus.expired,
      );
    });

    test('estado desconocido colapsa a revision (nunca promete de mas)', () {
      expect(
        LoanApplicationStatus.fromServer('ALGO_NUEVO'),
        LoanApplicationStatus.inReview,
      );
      expect(
        LoanApplicationStatus.fromServer(null),
        LoanApplicationStatus.inReview,
      );
    });

    test('toJson usa las claves exactas del contrato docs/05 §6.4', () {
      const request = LoanApplicationRequest(
        productId: 'p1',
        requestedAmountMinor: 1000000,
        termMonths: 12,
        totalIncomeMinor: 350000,
        totalDebtMinor: 0,
        bureauAuthorized: true,
      );
      expect(request.toJson(), {
        'product_id': 'p1',
        'requested_amount_minor': 1000000,
        'term_months': 12,
        'total_income_minor': 350000,
        'total_debt_minor': 0,
        'bureau_authorized': true,
      });
    });

    test('fromJson transporta el dictamen tal cual (sin interpretar)', () {
      final result = LoanApplicationResult.fromJson(
        _applicationJson(reason: 'Motivo exacto del servidor.'),
      );
      expect(result.id, 'app-1');
      expect(result.status, LoanApplicationStatus.approved);
      expect(result.decisionReason, 'Motivo exacto del servidor.');
      expect(result.requestedAmountMinor, 1000000);
      expect(result.dtiRatio, '0.3800');
    });
  });

  group('CreditApplicationsService (HTTP)', () {
    test('submit envia el cuerpo exacto con la Idempotency-Key dada',
        () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: _envelope(_applicationJson()),
        ),
      );
      final service = CreditApplicationsService(api: stub.client());
      const request = LoanApplicationRequest(
        productId: 'p1',
        requestedAmountMinor: 1000000,
        termMonths: 12,
        totalIncomeMinor: 350000,
        totalDebtMinor: 0,
        bureauAuthorized: true,
      );

      final result = await service.submitApplication(
        request: request,
        idempotencyKey: 'clave-del-intento',
      );

      expect(result.status, LoanApplicationStatus.approved);
      expect(result.decisionReason, contains('capacidad de pago'));
      final sent = stub.lastOptions?.data as Map?;
      expect(sent?['product_id'], 'p1');
      expect(sent?['requested_amount_minor'], 1000000);
      expect(sent?['bureau_authorized'], isTrue);
      expect(
        stub.lastOptions?.headers['Idempotency-Key'],
        'clave-del-intento',
      );
    });

    test('submit sin clave genera una Idempotency-Key', () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: _envelope(_applicationJson()),
        ),
      );
      final service = CreditApplicationsService(api: stub.client());
      const request = LoanApplicationRequest(
        productId: 'p1',
        requestedAmountMinor: 1000000,
        termMonths: 12,
        totalIncomeMinor: 350000,
        totalDebtMinor: 0,
        bureauAuthorized: true,
      );

      await service.submitApplication(request: request);

      final key = stub.lastOptions?.headers['Idempotency-Key'] as String?;
      expect(key, isNotNull);
      expect(key!, isNotEmpty);
    });

    test('202 con IN_REVIEW se parsea como revision (bureau caido)',
        () async {
      final api = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 202,
          data: _envelope(
            _applicationJson(status: 'IN_REVIEW', reason: 'en revision.'),
          ),
        ),
      ).client();
      final service = CreditApplicationsService(api: api);

      final result = await service.submitApplication(
        request: const LoanApplicationRequest(
          productId: 'p1',
          requestedAmountMinor: 1000000,
          termMonths: 12,
          totalIncomeMinor: 350000,
          totalDebtMinor: 0,
          bureauAuthorized: true,
        ),
      );

      expect(result.status, LoanApplicationStatus.inReview);
    });

    test('getApplication consulta el expediente por id', () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: _envelope(_applicationJson(id: 'app-9')),
        ),
      );
      final service = CreditApplicationsService(api: stub.client());

      final result = await service.getApplication('app-9');

      expect(result.id, 'app-9');
      expect(stub.lastOptions?.path, contains('app-9'));
    });

    test('422 sin consentimiento se mapea a VALIDATION_ERROR', () async {
      final api = _Stub(
        (o) => _reject(o, 422, {
          'error': {
            'code': 'VALIDATION_ERROR',
            'message': 'bureau_authorized debe ser true',
          },
        }),
      ).client();
      final service = CreditApplicationsService(api: api);

      expect(
        () => service.submitApplication(
          request: const LoanApplicationRequest(
            productId: 'p1',
            requestedAmountMinor: 1000000,
            termMonths: 12,
            totalIncomeMinor: 350000,
            totalDebtMinor: 0,
            bureauAuthorized: false,
          ),
        ),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'VALIDATION_ERROR'),
        ),
      );
    });
  });

  group('creditApplicationUserMessage', () {
    test('BUREAU_UNAVAILABLE informa revision (no error)', () {
      expect(
        creditApplicationUserMessage(
          ApiException(code: 'BUREAU_UNAVAILABLE', message: 'tecnico'),
        ),
        contains('revisando'),
      );
    });

    test('422 orienta al consentimiento y los datos', () {
      expect(
        creditApplicationUserMessage(
          ApiException(code: 'VALIDATION_ERROR', message: 'x'),
        ),
        contains('consentimiento'),
      );
    });

    test('404 del expediente es accionable', () {
      expect(
        creditApplicationUserMessage(
          ApiException(code: 'NOT_FOUND', message: 'x'),
        ),
        contains('No encontramos el expediente'),
      );
    });

    test('409 indica solicitud ya registrada', () {
      expect(
        creditApplicationUserMessage(
          ApiException(code: 'IDEMPOTENCY_CONFLICT', message: 'x'),
        ),
        contains('ya fue registrada'),
      );
    });
  });

  group('CreditApplicationPage', () {
    testWidgets('sin consentimiento no se envia (CA-01)', (tester) async {
      final fake = FakeCreditApplicationsService();
      await _pumpApply(tester, fake);
      await _fillValidIncome(tester);

      // El CTA sigue bloqueado: no hay envio aunque los datos esten listos.
      await _reveal(tester, find.byKey(const Key('credit-apply-submit')));
      final submit = tester
          .widget<FilledButton>(find.byKey(const Key('credit-apply-submit')));
      expect(submit.onPressed, isNull);
      expect(fake.submitCalls, 0);
    });

    testWidgets('faltantes se senalan antes de enviar (CA-02)',
        (tester) async {
      final fake = FakeCreditApplicationsService();
      await _pumpApply(tester, fake);

      await _reveal(tester, find.byKey(const Key('credit-apply-consent')));
      await tester.tap(find.byKey(const Key('credit-apply-consent')));
      await tester.pump();
      await _reveal(tester, find.byKey(const Key('credit-apply-submit')));
      await tester.tap(find.byKey(const Key('credit-apply-submit')));
      await tester.pump();

      expect(
        find.text('Ingresa tu ingreso mensual neto (mayor a S/ 0.00).'),
        findsWidgets,
      );
      expect(fake.submitCalls, 0);
    });

    testWidgets('camino feliz: consentimiento -> envio -> estado aprobado',
        (tester) async {
      final fake = FakeCreditApplicationsService();
      creditApplicationsServiceFactory = () => fake;
      addTearDown(() => creditApplicationsServiceFactory = null);
      final router = GoRouter(
        initialLocation: '/credits/apply',
        initialExtra: _draft(),
        routes: [...creditsRoutes],
      );
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _fillValidIncome(tester);
      await _reveal(tester, find.byKey(const Key('credit-apply-consent')));
      await tester.tap(find.byKey(const Key('credit-apply-consent')));
      await tester.pump();
      await _reveal(tester, find.byKey(const Key('credit-apply-submit')));
      await tester.tap(find.byKey(const Key('credit-apply-submit')));
      await tester.pumpAndSettle();

      // El cuerpo enviado usa las claves del contrato, sin PII extra.
      expect(fake.submitCalls, 1);
      expect(fake.lastRequest?.toJson()['bureau_authorized'], isTrue);
      expect(fake.lastRequest?.toJson()['total_income_minor'], 350000);
      // Dictamen del servidor, tal cual.
      expect(find.text('¡Crédito aprobado!'), findsOneWidget);
      expect(
        find.text('Score y capacidad de pago dentro de política.'),
        findsOneWidget,
      );
      expect(find.text('Aprobado'), findsOneWidget);
      expect(find.text('S/ 10,000.00 • 12 meses'), findsOneWidget);
      expect(find.text('Continuar a la firma'), findsOneWidget);
    });

    testWidgets('reintento reutiliza la misma Idempotency-Key',
        (tester) async {
      final fake = FakeCreditApplicationsService()
        ..submitError = ApiException.network();
      await _pumpApply(tester, fake);

      await _fillValidIncome(tester);
      await _reveal(tester, find.byKey(const Key('credit-apply-consent')));
      await tester.tap(find.byKey(const Key('credit-apply-consent')));
      await tester.pump();
      await _reveal(tester, find.byKey(const Key('credit-apply-submit')));
      await tester.tap(find.byKey(const Key('credit-apply-submit')));
      await tester.pumpAndSettle();

      expect(fake.submitCalls, 1);
      await _reveal(tester, find.byKey(const Key('credit-apply-retry')));
      expect(find.byKey(const Key('credit-apply-server-error')), findsOneWidget);

      await tester.tap(find.byKey(const Key('credit-apply-retry')));
      await tester.pumpAndSettle();

      expect(fake.submitCalls, 2);
      expect(fake.idempotencyKeys[0], isNotNull);
      expect(fake.idempotencyKeys[1], fake.idempotencyKeys[0]);
    });

    testWidgets('cuerpo distinto genera una nueva Idempotency-Key',
        (tester) async {
      final fake = FakeCreditApplicationsService()
        ..submitError = ApiException.network();
      await _pumpApply(tester, fake);

      await _fillValidIncome(tester);
      await _reveal(tester, find.byKey(const Key('credit-apply-consent')));
      await tester.tap(find.byKey(const Key('credit-apply-consent')));
      await tester.pump();
      await _reveal(tester, find.byKey(const Key('credit-apply-submit')));
      await tester.tap(find.byKey(const Key('credit-apply-submit')));
      await tester.pumpAndSettle();

      expect(fake.submitCalls, 1);

      // Reintento con el mismo cuerpo: se reutiliza la misma clave.
      await _reveal(tester, find.byKey(const Key('credit-apply-retry')));
      await tester.tap(find.byKey(const Key('credit-apply-retry')));
      await tester.pumpAndSettle();

      expect(fake.submitCalls, 2);
      expect(fake.idempotencyKeys[1], fake.idempotencyKeys[0]);

      // Al cambiar el cuerpo (ingreso), el reenvío usa una clave nueva:
      // editar limpia el error visible, así que se reenvía con el CTA.
      await _enterMoney(tester, const Key('credit-apply-income'), '4000.00');
      await _reveal(tester, find.byKey(const Key('credit-apply-submit')));
      await tester.tap(find.byKey(const Key('credit-apply-submit')));
      await tester.pumpAndSettle();

      expect(fake.submitCalls, 3);
      expect(fake.idempotencyKeys[2], isNotNull);
      expect(fake.idempotencyKeys[2], isNot(fake.idempotencyKeys[0]));
    });

    testWidgets('BUREAU_UNAVAILABLE muestra En revision (no error)',
        (tester) async {
      final fake = FakeCreditApplicationsService()
        ..submitError = ApiException(
          code: 'BUREAU_UNAVAILABLE',
          message: 'bureau caido',
        );
      creditApplicationsServiceFactory = () => fake;
      addTearDown(() => creditApplicationsServiceFactory = null);
      final router = GoRouter(
        initialLocation: '/credits/apply',
        initialExtra: _draft(),
        routes: [...creditsRoutes],
      );
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _fillValidIncome(tester);
      await _reveal(tester, find.byKey(const Key('credit-apply-consent')));
      await tester.tap(find.byKey(const Key('credit-apply-consent')));
      await tester.pump();
      await _reveal(tester, find.byKey(const Key('credit-apply-submit')));
      await tester.tap(find.byKey(const Key('credit-apply-submit')));
      await tester.pumpAndSettle();

      expect(find.text('Estamos revisando tu solicitud.'), findsOneWidget);
      expect(find.text('En revisión'), findsOneWidget);
      expect(find.text('Volver al simulador'), findsOneWidget);
      expect(find.byKey(const Key('credit-status-error')), findsNothing);
    });

    testWidgets('422 muestra error accionable con Reintentar',
        (tester) async {
      final fake = FakeCreditApplicationsService()
        ..submitError = ApiException(
          code: 'VALIDATION_ERROR',
          message: 'tecnico',
          statusCode: 422,
        );
      await _pumpApply(tester, fake);

      await _fillValidIncome(tester);
      await _reveal(tester, find.byKey(const Key('credit-apply-consent')));
      await tester.tap(find.byKey(const Key('credit-apply-consent')));
      await tester.pump();
      await _reveal(tester, find.byKey(const Key('credit-apply-submit')));
      await tester.tap(find.byKey(const Key('credit-apply-submit')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('credit-status-content')), findsNothing);
      expect(
        find.textContaining('Sin el consentimiento marcado'),
        findsOneWidget,
      );
      expect(find.byKey(const Key('credit-apply-retry')), findsOneWidget);
    });
  });

  group('CreditStatusPage', () {
    testWidgets('estado visible con motivo exacto y progreso (CA-04)',
        (tester) async {
      final fake = FakeCreditApplicationsService();
      await tester.pumpWidget(
        MaterialApp(
          home: CreditStatusPage(
            service: fake,
            initialResult: const LoanApplicationResult(
              id: 'app-1',
              status: LoanApplicationStatus.approved,
              decisionReason: 'Motivo exacto del servidor.',
              requestedAmountMinor: 1000000,
              termMonths: 12,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('EXPEDIENTE app-1'), findsOneWidget);
      expect(find.text('Aprobado'), findsOneWidget);
      expect(find.text('¡Crédito aprobado!'), findsOneWidget);
      expect(find.text('Motivo exacto del servidor.'), findsOneWidget);
      expect(find.byKey(const Key('credit-status-steps')), findsOneWidget);
      expect(find.text('Solicitud enviada'), findsOneWidget);
      await _reveal(
        tester,
        find.byKey(const Key('credit-status-schedule-cta')),
      );
      expect(find.text('Continuar a la firma'), findsOneWidget);
      expect(find.text('Ver cronograma'), findsOneWidget);
    });

    testWidgets('rechazado ofrece simular otro monto', (tester) async {
      final fake = FakeCreditApplicationsService();
      await tester.pumpWidget(
        MaterialApp(
          home: CreditStatusPage(
            service: fake,
            initialResult: const LoanApplicationResult(
              id: 'app-2',
              status: LoanApplicationStatus.rejected,
              decisionReason: 'DTI sobre el tope.',
              requestedAmountMinor: 1000000,
              termMonths: 12,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Rechazado'), findsOneWidget);
      expect(find.text('No pudimos aprobarlo esta vez.'), findsOneWidget);
      expect(find.text('DTI sobre el tope.'), findsOneWidget);
      await _reveal(
        tester,
        find.byKey(const Key('credit-status-home-cta')),
      );
      expect(find.text('Simular otro monto'), findsOneWidget);
      expect(find.text('Volver al inicio'), findsOneWidget);
    });

    testWidgets('carga por id con pull-to-refresh', (tester) async {
      final fake = FakeCreditApplicationsService();
      await tester.pumpWidget(
        MaterialApp(
          home: CreditStatusPage(
            service: fake,
            applicationId: 'app-1',
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(fake.getCalls, 1);
      expect(find.text('¡Crédito aprobado!'), findsOneWidget);

      await tester.fling(
        find.byKey(const Key('credit-status-content')),
        const Offset(0, 300),
        1000,
      );
      await tester.pumpAndSettle();

      expect(fake.getCalls, 2);
      expect(fake.lastStatusId, 'app-1');
    });

    testWidgets('404 del estado es accionable con Reintentar',
        (tester) async {
      final fake = FakeCreditApplicationsService()
        ..statusError = ApiException(
          code: 'NOT_FOUND',
          message: 'tecnico',
          statusCode: 404,
        );
      await tester.pumpWidget(
        MaterialApp(
          home: CreditStatusPage(
            service: fake,
            applicationId: 'app-fantasma',
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(
        find.textContaining('No encontramos el expediente.'),
        findsOneWidget,
      );

      fake.statusError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.text('¡Crédito aprobado!'), findsOneWidget);
    });

    testWidgets('estado de carga del expediente', (tester) async {
      final fake = _FakeHangingStatus();
      await tester.pumpWidget(
        MaterialApp(
          home: CreditStatusPage(
            service: fake,
            applicationId: 'app-1',
          ),
        ),
      );
      await tester.pump();

      expect(
        find.text('Consultando el estado del expediente…'),
        findsOneWidget,
      );
    });

    testWidgets('vacio sin expediente (navegacion directa)', (tester) async {
      final fake = FakeCreditApplicationsService();
      await tester.pumpWidget(
        MaterialApp(home: CreditStatusPage(service: fake)),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('Sin expediente'), findsOneWidget);
      expect(fake.getCalls, 0);
    });
  });
}

/// Servicio colgado solo en `getApplication` (estado de carga).
class _FakeHangingStatus extends FakeCreditApplicationsService {
  @override
  Future<LoanApplicationResult> getApplication(String id) =>
      Completer<LoanApplicationResult>().future;
}
