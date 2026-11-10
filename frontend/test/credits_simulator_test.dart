import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/credits/credits_routes.dart';
import 'package:banca_online/features/credits/data/credits_service.dart';
import 'package:banca_online/features/credits/models/credit.dart';
import 'package:banca_online/features/credits/presentation/credits_simulator_page.dart';
import 'package:banca_online/features/credits/utils/format.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del servicio (seam `CreditsServiceBase`): sin red, con shapes
/// docs/05 §6.4 (`{data, meta}`, tasas como strings exactos, dinero en
/// centimos). La cuota que muestra la UI sale de este mock: el cliente no
/// recalcula nada (E3-T04, cliente delgado).
class FakeCreditsService implements CreditsServiceBase {
  FakeCreditsService({required this.productsSeed});

  List<LoanProduct> productsSeed;
  ApiException? productsError;

  ApiException? simulateError;
  ApiException? exportError;
  SimulationExport? exportStub;

  int simulateCalls = 0;
  String? lastProductId;
  int? lastAmountMinor;
  int? lastTermMonths;
  bool? lastInsurance;

  @override
  Future<List<LoanProduct>> products() async {
    final error = productsError;
    if (error != null) throw error;
    return productsSeed;
  }

  /// Resultado determinista del "servidor": la cuota con desgravamen es
  /// mayor (la decide el mock, no la UI).
  SimulationResult _result({
    required String productId,
    required int amountMinor,
    required int termMonths,
    required bool insurance,
  }) {
    return SimulationResult(
      productId: productId,
      productCode: 'PERSONAL',
      productName: 'Personal',
      amountMinor: amountMinor,
      termMonths: termMonths,
      insurance: insurance,
      tea: '0.1850',
      tcea: insurance ? '0.1990' : '0.1980',
      paymentMinor: insurance ? 95120 : 92750,
      schedule: List.generate(
        12,
        (i) => ScheduleRow(
          installmentNo: i + 1,
          principalMinor: 76210 + i * 100,
          interestMinor: 16540 - i * 100,
          insuranceMinor: insurance ? 2370 : 0,
          totalMinor: insurance ? 95120 : 92750,
          balanceMinor: 923790 - i * 77000,
        ),
      ),
    );
  }

  @override
  Future<SimulationResult> simulate({
    required String productId,
    required int amountMinor,
    required int termMonths,
    required bool insurance,
  }) async {
    simulateCalls++;
    lastProductId = productId;
    lastAmountMinor = amountMinor;
    lastTermMonths = termMonths;
    lastInsurance = insurance;
    final error = simulateError;
    if (error != null) throw error;
    return _result(
      productId: productId,
      amountMinor: amountMinor,
      termMonths: termMonths,
      insurance: insurance,
    );
  }

  @override
  Future<SimulationExport> exportSimulation({
    required String productId,
    required int amountMinor,
    required int termMonths,
    required bool insurance,
  }) async {
    final error = exportError;
    if (error != null) throw error;
    return exportStub ??
        const SimulationExport(
          bytes: [1, 2, 3],
          suggestedFilename: 'simulacion-credito-personal.pdf',
        );
  }
}

LoanProduct _product({
  required String id,
  required String code,
  required String name,
  String? tea,
  int min = 100000,
  int max = 5000000,
  List<int>? terms,
}) {
  return LoanProduct(
    id: id,
    code: code,
    name: name,
    tcea: '0.198',
    tea: tea ?? '0.185',
    insuranceRate: '0.005',
    minAmountMinor: min,
    maxAmountMinor: max,
    minTermMonths: 6,
    maxTermMonths: 48,
    allowedTerms: terms ?? const [6, 12, 24, 36],
    enabled: true,
  );
}

FakeCreditsService _fake() => FakeCreditsService(
      productsSeed: [
        _product(id: 'p1', code: 'PERSONAL', name: 'Personal'),
        _product(
          id: 'p2',
          code: 'VEHICULAR',
          name: 'Vehicular',
          tea: '0.129',
          min: 500000,
          max: 20000000,
          terms: const [12, 24, 36, 48],
        ),
      ],
    );

Future<void> _pumpPage(WidgetTester tester, FakeCreditsService fake) async {
  await tester.pumpWidget(
    MaterialApp(home: CreditsSimulatorPage(service: fake)),
  );
  await tester.pumpAndSettle();
}

/// Desplaza el `ListView` hasta que [finder] sea visible (el contenido de
/// la pantalla excede el viewport de 600px y se construye perezosamente).
Future<void> _reveal(WidgetTester tester, Finder finder) async {
  await tester.scrollUntilVisible(finder, 400);
  await tester.pumpAndSettle();
}

/// Servicio mockeado a nivel HTTP: `dio` resuelve respuestas enlatadas sin
/// salir a red (mismo patron que `test/accounts_service_test.dart`).
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
  group('format', () {
    test('formatMinor usa S/ con miles y 2 decimales', () {
      expect(formatMinor(1000000, 'PEN'), 'S/ 10,000.00');
      expect(formatMinor(92750, 'PEN'), 'S/ 927.50');
    });

    test('formatPercent convierte el decimal exacto a %', () {
      expect(formatPercent('0.185'), '18.50%');
      expect(formatPercent('0.1980'), '19.80%');
    });
  });

  group('modelos', () {
    test('LoanProduct parsea allowed_terms null (solo rango)', () {
      final product = LoanProduct.fromJson({
        'id': 'p9',
        'code': 'PYME',
        'name': 'Pyme',
        'tcea': '0.22',
        'tea': null,
        'insurance_rate': '0.005',
        'min_amount_minor': 100000,
        'max_amount_minor': 5000000,
        'min_term_months': 6,
        'max_term_months': 36,
        'allowed_terms': null,
        'enabled': true,
      });
      expect(product.allowedTerms, isNull);
      expect(product.tea, isNull);
    });
  });

  group('CreditsService (HTTP)', () {
    test('products parsea el catalogo (shape docs/05 §6.4)', () async {
      final api = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': [
              {
                'id': 'p1',
                'code': 'PERSONAL',
                'name': 'Personal',
                'tcea': '0.198',
                'tea': '0.185',
                'insurance_rate': '0.005',
                'min_amount_minor': 100000,
                'max_amount_minor': 5000000,
                'min_term_months': 6,
                'max_term_months': 36,
                'allowed_terms': [6, 12, 24, 36],
                'enabled': true,
              },
            ],
            'meta': {'total': 1},
          },
        ),
      ).client();
      final service = CreditsService(api: api);

      final products = await service.products();

      expect(products, hasLength(1));
      expect(products.first.code, 'PERSONAL');
      expect(products.first.allowedTerms, [6, 12, 24, 36]);
    });

    test('simulate envia parametros sin Idempotency-Key y parsea la cuota',
        () async {
      final stub = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': {
              'product': {
                'id': 'p1',
                'code': 'PERSONAL',
                'name': 'Personal',
              },
              'amount_minor': 1000000,
              'term_months': 12,
              'insurance': false,
              'tea': '0.1850',
              'tcea': '0.1980',
              'payment_minor': 92750,
              'total_principal_minor': 1000000,
              'total_interest_minor': 113000,
              'total_insurance_minor': 0,
              'total_paid_minor': 1113000,
              'schedule': [
                {
                  'installment_no': 1,
                  'due_date': null,
                  'principal_minor': 76210,
                  'interest_minor': 16540,
                  'insurance_minor': 0,
                  'total_minor': 92750,
                  'balance_minor': 923790,
                },
              ],
            },
            'meta': {},
          },
        ),
      );
      final service = CreditsService(api: stub.client());

      final result = await service.simulate(
        productId: 'p1',
        amountMinor: 1000000,
        termMonths: 12,
        insurance: false,
      );

      expect(result.paymentMinor, 92750);
      expect(result.schedule, hasLength(1));
      expect(result.schedule.first.balanceMinor, 923790);
      final sent = stub.lastOptions?.data as Map?;
      expect(sent?['product_id'], 'p1');
      expect(sent?['amount_minor'], 1000000);
      // La simulacion no mueve dinero: sin clave de idempotencia.
      expect(
        (stub.lastOptions?.headers ?? {}).containsKey('Idempotency-Key'),
        isFalse,
      );
    });

    test('simulate mapea 422 a VALIDATION_ERROR', () async {
      final api = _Stub(
        (o) => _reject(o, 422, {
          'error': {
            'code': 'VALIDATION_ERROR',
            'message': 'monto fuera de rango',
          },
        }),
      ).client();
      final service = CreditsService(api: api);

      expect(
        () => service.simulate(
          productId: 'p1',
          amountMinor: 1,
          termMonths: 12,
          insurance: false,
        ),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'VALIDATION_ERROR'),
        ),
      );
    });

    test('exportSimulation devuelve bytes + nombre del servidor', () async {
      final api = _Stub(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: [37, 80, 68, 70],
          headers: Headers.fromMap({
            'content-disposition': [
              'attachment; filename="simulacion-credito-personal.pdf"',
            ],
          }),
        ),
      ).client();
      final service = CreditsService(api: api);

      final file = await service.exportSimulation(
        productId: 'p1',
        amountMinor: 1000000,
        termMonths: 12,
        insurance: false,
      );

      expect(file.suggestedFilename, 'simulacion-credito-personal.pdf');
      expect(file.bytes, hasLength(4));
    });

    test('exportSimulation mapea 501 a EXPORT_FORMAT_NOT_SUPPORTED',
        () async {
      final api = _Stub(
        (o) => _reject(o, 501, {
          'error': {
            'code': 'EXPORT_FORMAT_NOT_SUPPORTED',
            'message': 'sin backend PDF',
          },
        }),
      ).client();
      final service = CreditsService(api: api);

      expect(
        () => service.exportSimulation(
          productId: 'p1',
          amountMinor: 1000000,
          termMonths: 12,
          insurance: false,
        ),
        throwsA(
          isA<ApiException>().having(
            (e) => e.code,
            'code',
            'EXPORT_FORMAT_NOT_SUPPORTED',
          ),
        ),
      );
    });
  });

  group('creditUserMessage', () {
    test('501 del export usa el mensaje claro de la spec', () {
      expect(
        creditUserMessage(
          ApiException(
            code: 'EXPORT_FORMAT_NOT_SUPPORTED',
            message: 'tecnico',
            statusCode: 501,
          ),
        ),
        'Exportación no disponible por ahora.',
      );
    });

    test('422 orienta a revisar monto y plazo', () {
      expect(
        creditUserMessage(
          ApiException(code: 'VALIDATION_ERROR', message: 'x'),
        ),
        'Revisa el monto y el plazo e intentalo de nuevo.',
      );
    });
  });

  group('CreditsSimulatorPage', () {
    testWidgets('camino feliz: cuota del servidor + cronograma + export',
        (tester) async {
      final fake = _fake();
      await _pumpPage(tester, fake);

      // Producto default (primero) + controles con valores de la spec.
      expect(find.text('Simula tu crédito'), findsOneWidget);
      expect(find.text('Monto: S/ 10,000.00'), findsOneWidget);
      expect(find.text('Plazo: 12 meses'), findsOneWidget);
      // La cuota mostrada es la del mock del backend (sin recalculo).
      await _reveal(tester, find.byKey(const Key('credit-monthly-payment')));
      expect(find.byKey(const Key('credit-monthly-payment')), findsOneWidget);
      expect(find.text('S/ 927.50'), findsOneWidget);
      expect(
        find.text('TEA 18.50% • TCEA 19.80% • Francesa'),
        findsOneWidget,
      );
      // Cronograma resumido: 3 primeras filas + link a la completa.
      expect(find.byKey(const Key('credit-schedule')), findsOneWidget);
      expect(find.text('Ver tabla completa (12 cuotas)'), findsOneWidget);

      // Tabla completa en el bottom-sheet.
      await _reveal(tester, find.byKey(const Key('credit-full-schedule')));
      await tester.tap(find.byKey(const Key('credit-full-schedule')));
      await tester.pumpAndSettle();
      expect(find.text('Cronograma completo (12 cuotas)'), findsOneWidget);
      await tester.tap(find.text('Cerrar'));
      await tester.pumpAndSettle();

      // Export informa nombre/tamano (guardado nativo pendiente).
      await _reveal(tester, find.byKey(const Key('credit-export-cta')));
      await tester.tap(find.byKey(const Key('credit-export-cta')));
      await tester.pumpAndSettle();
      expect(
        find.textContaining('simulacion-credito-personal.pdf'),
        findsOneWidget,
      );
    });

    testWidgets('toggle de desgravamen re-simula con insurance:true',
        (tester) async {
      final fake = _fake();
      await _pumpPage(tester, fake);
      expect(fake.lastInsurance, isFalse);

      await _reveal(
        tester,
        find.byKey(const Key('credit-insurance-switch')),
      );
      await tester.tap(find.byKey(const Key('credit-insurance-switch')));
      await tester.pump(const Duration(milliseconds: 500));
      await tester.pumpAndSettle();

      expect(fake.lastInsurance, isTrue);
      expect(fake.simulateCalls, greaterThanOrEqualTo(2));
      // Cuota recalculada por el servidor (mock), no por la UI.
      expect(find.text('S/ 951.20'), findsOneWidget);
      expect(
        find.text('TEA 18.50% • TCEA 19.90% • Francesa'),
        findsOneWidget,
      );
    });

    testWidgets('cambio de plazo y producto re-simula', (tester) async {
      final fake = _fake();
      await _pumpPage(tester, fake);
      final baseline = fake.simulateCalls;

      await tester.tap(find.byKey(const Key('credit-term-24')));
      await tester.pump(const Duration(milliseconds: 500));
      await tester.pumpAndSettle();
      expect(find.text('Plazo: 24 meses'), findsOneWidget);
      expect(fake.lastTermMonths, 24);

      await tester.tap(find.byKey(const Key('credit-product-VEHICULAR')));
      await tester.pump(const Duration(milliseconds: 500));
      await tester.pumpAndSettle();
      expect(fake.lastProductId, 'p2');
      expect(fake.simulateCalls, greaterThan(baseline + 1));
    });

    testWidgets('CTA solicitar navega con el borrador pre-cargado',
        (tester) async {
      final fake = _fake();
      creditsServiceFactory = () => fake;
      addTearDown(() => creditsServiceFactory = null);
      final router = GoRouter(
        initialLocation: '/credits',
        routes: [...creditsRoutes],
      );
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _reveal(tester, find.byKey(const Key('credit-apply-cta')));
      await tester.tap(find.byKey(const Key('credit-apply-cta')));
      await tester.pumpAndSettle();

      expect(find.text('Tu solicitud'), findsWidgets);
      expect(
        find.text('Personal • S/ 10,000.00 • 12 m • S/ 927.50/mes'),
        findsOneWidget,
      );
    });

    testWidgets('estado de carga de productos', (tester) async {
      final fake = _FakeHangingProducts();
      await tester.pumpWidget(
        MaterialApp(home: CreditsSimulatorPage(service: fake)),
      );
      await tester.pump();

      expect(find.text('Cargando productos...'), findsOneWidget);
    });

    testWidgets('estado vacio sin productos', (tester) async {
      final fake = FakeCreditsService(productsSeed: const []);
      await _pumpPage(tester, fake);

      expect(
        find.text('Aún no hay productos de crédito disponibles.'),
        findsOneWidget,
      );
    });

    testWidgets('error de productos con Reintentar', (tester) async {
      final fake = _fake()
        ..productsError = ApiException.network();
      await tester.pumpWidget(
        MaterialApp(home: CreditsSimulatorPage(service: fake)),
      );
      await tester.pumpAndSettle();
      expect(find.textContaining('Sin conexion'), findsOneWidget);

      fake.productsError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      await _reveal(
        tester,
        find.byKey(const Key('credit-monthly-payment')),
      );
      expect(find.text('S/ 927.50'), findsOneWidget);
    });

    testWidgets('simulate 422 muestra error accionable en la card',
        (tester) async {
      final fake = _fake()
        ..simulateError = ApiException(
          code: 'VALIDATION_ERROR',
          message: 'tecnico',
          statusCode: 422,
        );
      await _pumpPage(tester, fake);

      await _reveal(tester, find.byKey(const Key('credit-quota-card')));
      expect(
        find.text('Revisa el monto y el plazo e intentalo de nuevo.'),
        findsOneWidget,
      );

      fake.simulateError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.text('S/ 927.50'), findsOneWidget);
    });

    testWidgets('export 501 muestra "no disponible por ahora"',
        (tester) async {
      final fake = _fake()
        ..exportError = ApiException(
          code: 'EXPORT_FORMAT_NOT_SUPPORTED',
          message: 'tecnico',
          statusCode: 501,
        );
      await _pumpPage(tester, fake);

      await _reveal(tester, find.byKey(const Key('credit-export-cta')));
      await tester.tap(find.byKey(const Key('credit-export-cta')));
      await tester.pumpAndSettle();
      expect(
        find.text('Exportación no disponible por ahora.'),
        findsOneWidget,
      );
    });

    testWidgets('error de red en simulate con Reintentar', (tester) async {
      final fake = _fake()..simulateError = ApiException.network();
      await _pumpPage(tester, fake);

      await _reveal(tester, find.byKey(const Key('credit-quota-card')));
      expect(find.textContaining('Sin conexion'), findsOneWidget);
      fake.simulateError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.text('S/ 927.50'), findsOneWidget);
    });
  });
}

/// Servicio colgado solo en `products()` (estado de carga).
class _FakeHangingProducts extends FakeCreditsService {
  _FakeHangingProducts() : super(productsSeed: const []);

  @override
  Future<List<LoanProduct>> products() =>
      Completer<List<LoanProduct>>().future;
}
