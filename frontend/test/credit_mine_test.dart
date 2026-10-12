import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/credits/credits_routes.dart';
import 'package:banca_online/features/credits/data/credit_applications_service.dart';
import 'package:banca_online/features/credits/data/credits_service.dart';
import 'package:banca_online/features/credits/data/my_credits_service.dart';
import 'package:banca_online/features/credits/models/credit.dart';
import 'package:banca_online/features/credits/models/loan_application.dart';
import 'package:banca_online/features/credits/models/own_application.dart';
import 'package:banca_online/features/credits/presentation/credits_home_page.dart';
import 'package:banca_online/features/credits/presentation/my_credits_section.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del listado (seam `MyCreditsServiceBase`): sin red. El `status` lo
/// decide el mock, no la UI (cliente delgado, E3-T23).
class FakeMyCreditsService implements MyCreditsServiceBase {
  FakeMyCreditsService({List<OwnCreditApplication>? items, int? total})
      : items = items ?? _approvedPlusPending(),
        totalOverride = total;

  List<OwnCreditApplication> items;
  int? totalOverride;
  ApiException? error;
  int calls = 0;
  int? lastLimit;
  int? lastOffset;

  FakeMyCreditsService.empty()
      : items = const [],
        totalOverride = 0;

  static List<OwnCreditApplication> _approvedPlusPending() => [
        const OwnCreditApplication(
          id: 'app-approved-1',
          productId: 'p1',
          productName: 'Personal',
          requestedAmountMinor: 1000000,
          termMonths: 12,
          status: LoanApplicationStatus.approved,
          decisionReason: 'Score y capacidad dentro de política.',
          createdAt: '2026-10-10T10:00:00Z',
        ),
        const OwnCreditApplication(
          id: 'app-review-2',
          productId: 'p2',
          productName: 'Vehicular',
          requestedAmountMinor: 2000000,
          termMonths: 24,
          status: LoanApplicationStatus.inReview,
          createdAt: '2026-10-09T09:00:00Z',
        ),
      ];

  @override
  Future<OwnApplicationsPage> listMine({int? limit, int? offset}) async {
    calls++;
    lastLimit = limit;
    lastOffset = offset;
    final err = error;
    if (err != null) throw err;
    return OwnApplicationsPage(
      items: List.of(items),
      total: totalOverride ?? items.length,
    );
  }
}

/// Fake colgado (estado de carga).
class HangingMyCreditsService extends FakeMyCreditsService {
  HangingMyCreditsService() : super(items: const []);

  @override
  Future<OwnApplicationsPage> listMine({int? limit, int? offset}) =>
      Completer<OwnApplicationsPage>().future;
}

/// Fake mínimo del simulador para la pestaña `Simulador` de la home.
class FakeSimulatorService implements CreditsServiceBase {
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

Map<String, dynamic> _mineJson({
  String id = 'app-1',
  String status = 'APPROVED',
  String? productName = 'Personal',
}) =>
    {
      'id': id,
      'product_id': 'p1',
      'product_name': productName,
      'requested_amount_minor': 1000000,
      'term_months': 12,
      'status': status,
      'decision_reason': 'Motivo del servidor.',
      'created_at': '2026-10-10T10:00:00Z',
      'updated_at': '2026-10-10T11:00:00Z',
      'decided_at': '2026-10-10T11:00:00Z',
    };

Future<void> _pumpSection(WidgetTester tester, FakeMyCreditsService fake) async {
  await tester.pumpWidget(
    MaterialApp(home: Scaffold(body: MyCreditsSection(service: fake))),
  );
  await tester.pumpAndSettle();
}

Future<void> _pumpHome(
  WidgetTester tester, {
  required FakeSimulatorService sim,
  required FakeMyCreditsService mine,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: CreditsHomePage(simulatorService: sim, myCreditsService: mine),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _goToMine(WidgetTester tester) async {
  await tester.tap(find.text('Mis créditos').first);
  await tester.pumpAndSettle();
}

/// Servicio mockeado a nivel HTTP (mismo patrón que
/// `test/credits_simulator_test.dart`).
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

void main() {
  group('OwnCreditApplication (modelo)', () {
    test('parsea el payload mínimo del listado', () {
      final app = OwnCreditApplication.fromJson(_mineJson());

      expect(app.id, 'app-1');
      expect(app.productId, 'p1');
      expect(app.productName, 'Personal');
      expect(app.displayProduct, 'Personal');
      expect(app.requestedAmountMinor, 1000000);
      expect(app.termMonths, 12);
      expect(app.status, LoanApplicationStatus.approved);
      expect(app.decisionReason, 'Motivo del servidor.');
      expect(app.createdAt, '2026-10-10T10:00:00Z');
    });

    test('displayProduct cae a product_id sin product_name', () {
      final app = OwnCreditApplication.fromJson(_mineJson(productName: null));

      expect(app.productName, isNull);
      expect(app.displayProduct, 'p1');
    });

    test('mapea los 5 estados a etiquetas ES', () {
      expect(
        ownApplicationStatusLabel(LoanApplicationStatus.approved),
        'Aprobado',
      );
      expect(
        ownApplicationStatusLabel(LoanApplicationStatus.rejected),
        'Rechazado',
      );
      expect(
        ownApplicationStatusLabel(LoanApplicationStatus.inReview),
        'En revisión',
      );
      expect(
        ownApplicationStatusLabel(LoanApplicationStatus.expired),
        'Vencido',
      );
      for (final s in [
        LoanApplicationStatus.draft,
        LoanApplicationStatus.submitted,
        LoanApplicationStatus.inEvaluation,
      ]) {
        expect(ownApplicationStatusLabel(s), 'Evaluando');
      }
    });

    test('OwnApplicationsPage parsea data + meta.total', () {
      final page = OwnApplicationsPage.fromJson({
        'data': [_mineJson(), _mineJson(id: 'app-2', status: 'IN_REVIEW')],
        'meta': {'total': 2, 'limit': 50, 'offset': 0},
      });

      expect(page.items, hasLength(2));
      expect(page.total, 2);
      expect(page.items[1].status, LoanApplicationStatus.inReview);
    });

    test('parsea contract_signed y por defecto es false', () {
      // Sin el campo (respuestas previas): no firmado.
      expect(
        OwnCreditApplication.fromJson(_mineJson()).contractSigned,
        isFalse,
      );
      final signed = OwnCreditApplication.fromJson({
        ..._mineJson(),
        'contract_signed': true,
      });
      expect(signed.contractSigned, isTrue);
    });

    test('sin PII de más: no existen campos de ingresos/deuda/score', () {      final app = OwnCreditApplication.fromJson(_mineJson());

      // El modelo ni siquiera declara esos campos: el JSON con extras se
      // ignora sin romper (el servidor no los manda en el listado).
      final withExtras = OwnCreditApplication.fromJson({
        ..._mineJson(),
        'total_income_minor': 999,
        'score': 800,
      });
      expect(withExtras.requestedAmountMinor, app.requestedAmountMinor);
    });
  });

  group('listMine (HTTP)', () {
    test('CreditApplicationsService parsea data + meta y pagina', () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': [_mineJson(), _mineJson(id: 'app-2', status: 'IN_REVIEW')],
            'meta': {'total': 2, 'limit': 10, 'offset': 0},
          },
        ),
      );
      final service = CreditApplicationsService(api: stub.client());

      final page = await service.listMine(limit: 10, offset: 0);

      expect(page.items, hasLength(2));
      expect(page.total, 2);
      expect(page.items.first.displayProduct, 'Personal');
      expect(stub.lastOptions?.queryParameters['limit'], 10);
      expect(stub.lastOptions?.queryParameters['offset'], 0);
      // Lectura: sin clave de idempotencia.
      expect(
        (stub.lastOptions?.headers ?? {}).containsKey('Idempotency-Key'),
        isFalse,
      );
    });

    test('MyCreditsService usa GET /credits/applications sin status', () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': [_mineJson()],
            'meta': {'total': 1},
          },
        ),
      );
      final service = MyCreditsService(api: stub.client());

      final page = await service.listMine();

      expect(page.items, hasLength(1));
      expect(stub.lastOptions?.path, '/credits/applications');
      expect(
        (stub.lastOptions?.queryParameters ?? {}).containsKey('status'),
        isFalse,
      );
    });

    test('mapea 401 a NOT_AUTHENTICATED y 422 a VALIDATION_ERROR', () async {
      final api401 = _Stub(
        (o) => _reject(o, 401, {
          'error': {'code': 'NOT_AUTHENTICATED', 'message': 'sin jwt'}
        }),
      ).client();
      await expectLater(
        () => CreditApplicationsService(api: api401).listMine(),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'NOT_AUTHENTICATED'),
        ),
      );

      final api422 = _Stub(
        (o) => _reject(o, 422, {
          'error': {'code': 'VALIDATION_ERROR', 'message': 'paginación'}
        }),
      ).client();
      await expectLater(
        () => CreditApplicationsService(api: api422).listMine(),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'VALIDATION_ERROR'),
        ),
      );
    });
  });

  group('MyCreditsSection', () {
    testWidgets('lista con badges por estado', (tester) async {
      await _pumpSection(tester, FakeMyCreditsService());

      expect(find.byKey(const Key('my-credits-list')), findsOneWidget);
      expect(
        find.text('Personal • S/ 10,000.00 • 12 m'),
        findsOneWidget,
      );
      expect(find.text('Aprobado'), findsOneWidget);
      expect(find.text('En revisión'), findsOneWidget);
      expect(
        find.textContaining('EXPEDIENTE app-approved'),
        findsOneWidget,
      );
    });

    testWidgets('fila aprobada con CTA a firma; no aprobada sin firma',
        (tester) async {
      await _pumpSection(tester, FakeMyCreditsService());

      expect(
        find.byKey(const Key('my-credits-sign-app-approved-1')),
        findsOneWidget,
      );
      expect(find.text('Continuar a la firma'), findsOneWidget);
      expect(
        find.byKey(const Key('my-credits-sign-app-review-2')),
        findsNothing,
      );
      // Todas las filas ofrecen ver el estado.
      expect(
        find.byKey(const Key('my-credits-status-app-approved-1')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('my-credits-status-app-review-2')),
        findsOneWidget,
      );
    });

    testWidgets('estado vacío con CTA al simulador', (tester) async {
      var wentToSimulator = false;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: MyCreditsSection(
              service: FakeMyCreditsService.empty(),
              onGoSimulator: () => wentToSimulator = true,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Aún no tienes solicitudes'), findsOneWidget);
      expect(
        find.text('Simula un crédito y envía tu primera solicitud.'),
        findsOneWidget,
      );
      await tester.tap(find.byKey(const Key('my-credits-empty-cta')));
      await tester.pumpAndSettle();
      expect(wentToSimulator, isTrue);
    });

    testWidgets('estado de carga', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: MyCreditsSection(service: HangingMyCreditsService())),
        ),
      );
      await tester.pump();

      expect(find.byKey(const Key('my-credits-loading')), findsOneWidget);
      expect(find.text('Cargando tus créditos...'), findsOneWidget);
    });

    testWidgets('error accionable con Reintentar', (tester) async {
      final fake = FakeMyCreditsService()..error = ApiException.network();
      await _pumpSection(tester, fake);

      expect(find.byKey(const Key('my-credits-error')), findsOneWidget);
      expect(find.textContaining('Sin conexion'), findsOneWidget);

      fake.error = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('my-credits-list')), findsOneWidget);
    });

    testWidgets('sin servicio: error accionable', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(body: MyCreditsSection(service: null)),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('my-credits-error')), findsOneWidget);
    });
  });

  group('CreditsHomePage (/credits con dos secciones)', () {
    testWidgets('muestra SegmentedButton y el simulador por defecto',
        (tester) async {
      await _pumpHome(
        tester,
        sim: FakeSimulatorService(),
        mine: FakeMyCreditsService(),
      );

      expect(find.text('Créditos'), findsOneWidget);
      expect(find.byKey(const Key('credits-segments')), findsOneWidget);
      expect(find.text('Simulador'), findsOneWidget);
      expect(find.text('Mis créditos'), findsOneWidget);
      // Pestaña por defecto: el simulador actual sigue ahí.
      expect(find.text('Simula tu crédito'), findsOneWidget);
      expect(find.byKey(const Key('my-credits-list')), findsNothing);
    });

    testWidgets('cambiar a Mis créditos muestra el listado', (tester) async {
      await _pumpHome(
        tester,
        sim: FakeSimulatorService(),
        mine: FakeMyCreditsService(),
      );

      await _goToMine(tester);

      expect(find.text('Mis créditos', skipOffstage: false), findsWidgets);
      expect(find.byKey(const Key('my-credits-list')), findsOneWidget);
      expect(find.text('Aprobado'), findsOneWidget);
    });

    testWidgets('vacío: Ir al simulador vuelve a la pestaña', (tester) async {
      await _pumpHome(
        tester,
        sim: FakeSimulatorService(),
        mine: FakeMyCreditsService.empty(),
      );

      await _goToMine(tester);
      expect(find.text('Aún no tienes solicitudes'), findsOneWidget);

      await tester.tap(find.byKey(const Key('my-credits-empty-cta')));
      await tester.pumpAndSettle();
      expect(find.text('Simula tu crédito'), findsOneWidget);
    });

    testWidgets('aprobada navega a /credits/contract con el id',
        (tester) async {
      final sim = FakeSimulatorService();
      creditsServiceFactory = () => sim;
      creditApplicationsServiceFactory = () => _AppsForRouter();
      addTearDown(() {
        creditsServiceFactory = null;
        creditApplicationsServiceFactory = null;
      });
      final router = GoRouter(
        initialLocation: '/credits',
        routes: [...creditsRoutes],
      );
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _goToMine(tester);
      await tester.tap(
        find.byKey(const Key('my-credits-sign-app-approved-1')),
      );
      await tester.pumpAndSettle();

      expect(find.text('Contrato de crédito'), findsOneWidget);
    });

    testWidgets('toda fila abre /credits/status con el id', (tester) async {
      creditsServiceFactory = () => FakeSimulatorService();
      creditApplicationsServiceFactory = () => _AppsForRouter();
      addTearDown(() {
        creditsServiceFactory = null;
        creditApplicationsServiceFactory = null;
      });
      final router = GoRouter(
        initialLocation: '/credits',
        routes: [...creditsRoutes],
      );
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _goToMine(tester);
      await tester.tap(
        find.byKey(const Key('my-credits-status-app-review-2')),
      );
      await tester.pumpAndSettle();

      expect(find.text('Estado del expediente'), findsOneWidget);
    });
  });
}

/// Servicio del expediente para el router en tests: listado propio poblado.
class _AppsForRouter extends FakeCreditApplicationsBridge
    implements CreditApplicationsServiceBase, MyCreditsServiceBase {
  @override
  Future<OwnApplicationsPage> listMine({int? limit, int? offset}) =>
      FakeMyCreditsService().listMine(limit: limit, offset: offset);
}

/// Puente mínimo de `CreditApplicationsServiceBase` (solo lo que el router
/// necesita para estas pruebas de navegación).
class FakeCreditApplicationsBridge implements CreditApplicationsServiceBase {
  @override
  Future<LoanApplicationResult> submitApplication({
    required LoanApplicationRequest request,
    String? idempotencyKey,
  }) =>
      throw UnimplementedError();

  @override
  Future<LoanApplicationResult> getApplication(String id) =>
      throw UnimplementedError();
}
