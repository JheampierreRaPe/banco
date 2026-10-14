import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/wallet/data/merchant_qr_service.dart';
import 'package:banca_online/features/wallet/models/merchant_qr.dart';
import 'package:banca_online/features/wallet/presentation/merchant_qr_page.dart';
import 'package:banca_online/features/wallet/presentation/merchant_qr_status_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:qr_flutter/qr_flutter.dart';

/// `qr_content` real de ejemplo (sobre canonico `BO-QR1` base64url,
/// `docs/05` §6.5): decodifica a
/// `{"amount_minor":2550,"currency":"PEN","exp":1790000000,
/// "issuer":"CUYCASH","peer":"CUYCASH","ref":"chg-1",
/// "sig":"sig-chg-1","v":"BO-QR1"}`.
const kSampleQrContent =
    'eyJhbW91bnRfbWlub3IiOjI1NTAsImN1cnJlbmN5IjoiUEVOIiwiZXhwIjoxNzkwMDAwMDAwLCJpc3N1ZXIiOiJDVVlDQVNIIiwicGVlciI6IkNVWUNBU0giLCJyZWYiOiJjaGctMSIsInNpZyI6InNpZy1jaGctMSIsInYiOiJCTy1RUjEifQ';

/// Fake del servicio (seam `MerchantQrServiceBase`): sin red, con datos
/// que replican los shapes E4-T08 + `qr_content` (`docs/05` §6.5):
/// `POST /merchants/qr` -> `data` con `qr_content` (sobre `BO-QR1`
/// escaneable) + `payload` (objeto) + `expires_at`;
/// `GET /merchants/qr/{id}` -> `data` con `status` efectivo).
class FakeMerchantQrService implements MerchantQrServiceBase {
  FakeMerchantQrService();

  /// Cuerpos enviados a `POST /merchants/qr`.
  final List<Map<String, dynamic>> createBodies = [];

  /// `Idempotency-Key` recibidas por intento (reuso en el Reintentar).
  final List<String?> createKeys = [];

  /// Si no es `null`, `createCharge` lanza este error.
  ApiException? createError;

  /// Si es `true`, `createCharge` cuelga (estado carga).
  bool hangCreate = false;

  /// Si es `true`, el proximo `createCharge` falla por red y los
  /// siguientes tienen exito (reuso de clave en el Reintentar).
  bool failNextCreate = false;

  /// Estados a devolver por `getChargeStatus` (por `charge_id`).
  final Map<String, QrChargeStatus> statusesById = {};

  /// Si no es `null`, `getChargeStatus` lanza este error.
  ApiException? statusError;

  /// Si es `true`, `getChargeStatus` cuelga (estado carga).
  bool hangStatus = false;

  int statusCalls = 0;

  String expiresAt({int minutes = 5}) => DateTime.now()
      .toUtc()
      .add(Duration(minutes: minutes))
      .toIso8601String();

  @override
  Future<QrCharge> createCharge(
    QrChargeInput input, {
    String? idempotencyKey,
  }) async {
    createBodies.add(input.toJson());
    createKeys.add(idempotencyKey);
    if (createError != null) throw createError!;
    if (failNextCreate) {
      failNextCreate = false;
      throw ApiException.network();
    }
    if (hangCreate) {
      await Completer<void>().future;
    }
    final now = DateTime.now().toUtc().toIso8601String();
    final charge = QrCharge(
      chargeId: 'chg-1',
      qrContent: kSampleQrContent,
      payload: const {
        'v': 'BO-QR1',
        'charge_id': 'chg-1',
        'amount_minor': 2550,
        'currency': 'PEN',
        'mode': 'DYNAMIC',
      },
      payloadHash: 'hash-chg-1',
      signature: 'sig-chg-1',
      amountMinor: input.amountMinor,
      currency: input.currency,
      concept: input.concept,
      mode: input.mode,
      status: 'ACTIVE',
      expiresAt: input.mode == 'STATIC' ? null : expiresAt(),
      createdAt: now,
    );
    statusesById['chg-1'] = QrChargeStatus(
      chargeId: 'chg-1',
      status: 'ACTIVE',
      amountMinor: input.amountMinor,
      currency: input.currency,
      expiresAt: charge.expiresAt,
    );
    return charge;
  }

  @override
  Future<QrChargeStatus> getChargeStatus(String chargeId) async {
    statusCalls++;
    if (statusError != null) throw statusError!;
    if (hangStatus) {
      await Completer<void>().future;
    }
    final status = statusesById[chargeId];
    if (status == null) {
      throw ApiException(
        code: 'NOT_FOUND',
        message: 'No encontramos ese cobro.',
        statusCode: 404,
      );
    }
    return status;
  }
}

/// Harness con `GoRouter` real (las paginas usan `context.push/go`).
Widget _harness(FakeMerchantQrService service, {String initial = '/m-qr'}) {
  final router = GoRouter(
    initialLocation: initial,
    routes: [
      GoRoute(
        path: '/m-qr',
        builder: (context, state) => MerchantQrPage(service: service),
      ),
      GoRoute(
        path: '/m-qr-status/:chargeId',
        builder: (context, state) => MerchantQrStatusPage(
          chargeId: state.pathParameters['chargeId'] ?? '',
          service: service,
        ),
      ),
      GoRoute(
        path: '/wallet/merchant-charges/:chargeId',
        builder: (context, state) => MerchantQrStatusPage(
          chargeId: state.pathParameters['chargeId'] ?? '',
          service: service,
        ),
      ),
    ],
  );
  return MaterialApp.router(routerConfig: router);
}

Future<void> _enterAmountConcept(
  WidgetTester tester, {
  String amount = '25.50',
  String concept = 'Café',
}) async {
  await tester.enterText(
    find.byKey(const Key('merchant-qr-amount')),
    amount,
  );
  await tester.enterText(
    find.byKey(const Key('merchant-qr-concept')),
    concept,
  );
  await tester.pump();
}

void main() {
  group('parseSolesToMinor / formatMinor (sin recalculo)', () {
    test('parsea soles a centimos exactos sin double', () {
      expect(parseSolesToMinor('25.50'), 2550);
      expect(parseSolesToMinor('25,50'), 2550);
      expect(parseSolesToMinor('100'), 10000);
      expect(parseSolesToMinor('0.01'), 1);
      expect(parseSolesToMinor('  7.5 '), 750);
    });

    test('rechaza formatos invalidos', () {
      expect(parseSolesToMinor(''), isNull);
      expect(parseSolesToMinor('abc'), isNull);
      expect(parseSolesToMinor('10.123'), isNull);
      expect(parseSolesToMinor('-5'), isNull);
    });

    test('formatea centimos del servidor sin redondeo', () {
      expect(formatMinor(2550, 'PEN'), 'S/ 25.50');
      expect(formatMinor(999, 'PEN'), 'S/ 9.99');
      expect(formatMinor(100, 'USD'), 'US\$ 1.00');
    });
  });

  group('MerchantQrPage (generar)', () {
    testWidgets('vacio inicial invita a generar', (tester) async {
      await tester.pumpWidget(_harness(FakeMerchantQrService()));
      await tester.pumpAndSettle();

      expect(find.text('Cobrar con QR'), findsOneWidget);
      expect(
        find.textContaining('Aún no has generado ningún cobro'),
        findsOneWidget,
      );
    });

    testWidgets('muestra carga mientras genera', (tester) async {
      final service = FakeMerchantQrService()..hangCreate = true;
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await _enterAmountConcept(tester);
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pump();

      expect(find.byKey(const Key('merchant-qr-loading')), findsOneWidget);
    });

    testWidgets('camino feliz: genera y muestra QR + monto + vigencia',
        (tester) async {
      final service = FakeMerchantQrService();
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await _enterAmountConcept(tester);
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pumpAndSettle();

      // Request tal cual: centimos + moneda + concepto + modo.
      expect(service.createBodies, hasLength(1));
      expect(service.createBodies.single['amount_minor'], 2550);
      expect(service.createBodies.single['currency'], 'PEN');
      expect(service.createBodies.single['concept'], 'Café');
      expect(service.createBodies.single['mode'], 'DYNAMIC');
      expect(service.createKeys.single, isNotNull);
      expect(service.createKeys.single, isNotEmpty);

      // Resultado: QR real con el `qr_content` del backend + monto +
      // vigencia.
      expect(find.byKey(const Key('merchant-qr-result')), findsOneWidget);
      expect(find.byKey(const Key('merchant-qr-image')), findsOneWidget);
      final qrImage = tester.widget<QrImageView>(
        find.byKey(const Key('merchant-qr-image')),
      );
      // `QrImageView` guarda `data` en privado: se verifica por el
      // `semanticsLabel` (mismo `qr_content`) + el texto visible.
      expect(qrImage.semanticsLabel, kSampleQrContent);
      expect(
        find.text(kSampleQrContent),
        findsOneWidget,
      );
      expect(find.text('S/ 25.50'), findsOneWidget);
      expect(find.byKey(const Key('merchant-qr-expires')), findsOneWidget);
      expect(find.textContaining('Vence:'), findsOneWidget);
      expect(find.text('Pendiente'), findsOneWidget);
    });

    testWidgets('no recalcula: muestra los valores del mock tal cual',
        (tester) async {
      final service = FakeMerchantQrService();
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await _enterAmountConcept(tester, amount: '9.99', concept: '');
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pumpAndSettle();

      expect(service.createBodies.single['amount_minor'], 999);
      expect(find.text('S/ 9.99'), findsOneWidget);
    });

    testWidgets('modo estatico muestra Sin vencimiento', (tester) async {
      final service = FakeMerchantQrService();
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Estático'));
      await tester.pumpAndSettle();
      await _enterAmountConcept(tester);
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pumpAndSettle();

      expect(service.createBodies.single['mode'], 'STATIC');
      expect(find.textContaining('Sin vencimiento'), findsOneWidget);
    });

    testWidgets('monto invalido no llama al servidor', (tester) async {
      final service = FakeMerchantQrService();
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('merchant-qr-amount')),
        'abc',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pump();

      expect(
        find.text('Ingresa un monto válido (p. ej. 25.50).'),
        findsOneWidget,
      );
      expect(service.createBodies, isEmpty);
    });

    testWidgets('monto cero pide mayor a cero sin llamar', (tester) async {
      final service = FakeMerchantQrService();
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('merchant-qr-amount')),
        '0',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pump();

      expect(
        find.text('El monto debe ser mayor a cero.'),
        findsOneWidget,
      );
      expect(service.createBodies, isEmpty);
    });

    testWidgets('403 sin rol muestra mensaje accionable', (tester) async {
      final service = FakeMerchantQrService()
        ..createError = ApiException(
          code: 'NOT_AUTHORIZED',
          message: 'No tienes permiso para esta operacion.',
          statusCode: 403,
        );
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await _enterAmountConcept(tester);
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('merchant-qr-error')), findsOneWidget);
      expect(
        find.text('Tu cuenta no tiene rol de comercio. Contacta a tu banco.'),
        findsOneWidget,
      );
      expect(find.byKey(const Key('merchant-qr-result')), findsNothing);
    });

    testWidgets('422 muestra error accionable bajo el formulario',
        (tester) async {
      final service = FakeMerchantQrService()
        ..createError = ApiException(
          code: 'VALIDATION_ERROR',
          message: 'Invalido.',
          statusCode: 422,
        );
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await _enterAmountConcept(tester);
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pumpAndSettle();

      expect(
        find.text('Revisa el monto y el concepto e inténtalo de nuevo.'),
        findsOneWidget,
      );
    });

    testWidgets('reintento tras fallo de red reusa la misma clave',
        (tester) async {
      final service = FakeMerchantQrService()..failNextCreate = true;
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await _enterAmountConcept(tester);
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('merchant-qr-error')), findsOneWidget);

      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pumpAndSettle();

      expect(service.createBodies, hasLength(2));
      expect(service.createKeys, hasLength(2));
      expect(service.createKeys[0], isNotNull);
      expect(service.createKeys[1], service.createKeys[0]);
      expect(find.byKey(const Key('merchant-qr-result')), findsOneWidget);
    });

    testWidgets('actualizar estado refleja el abono (ACTIVE -> PAID)',
        (tester) async {
      final service = FakeMerchantQrService();
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await _enterAmountConcept(tester);
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pumpAndSettle();
      expect(find.text('Pendiente'), findsOneWidget);

      // El pago (HU15) abona en el servidor: el refresh lo refleja.
      service.statusesById['chg-1'] = QrChargeStatus(
        chargeId: 'chg-1',
        status: 'PAID',
        amountMinor: 2550,
        currency: 'PEN',
        expiresAt: service.statusesById['chg-1']!.expiresAt,
        paidAt: DateTime.now().toUtc().toIso8601String(),
      );
      await tester.ensureVisible(find.byKey(const Key('merchant-qr-refresh')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('merchant-qr-refresh')));
      await tester.pumpAndSettle();

      expect(find.text('Pagado'), findsOneWidget);
      expect(find.text('Pendiente'), findsNothing);
    });

    testWidgets('detalle navega a la ruta de estado', (tester) async {
      final service = FakeMerchantQrService();
      await tester.pumpWidget(_harness(service));
      await tester.pumpAndSettle();

      await _enterAmountConcept(tester);
      await tester.tap(find.byKey(const Key('merchant-qr-generate')));
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.byKey(const Key('merchant-qr-detail')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('merchant-qr-detail')));
      await tester.pumpAndSettle();

      expect(find.text('Estado del cobro'), findsOneWidget);
      expect(find.byKey(const Key('merchant-qr-status-card')), findsOneWidget);
    });
  });

  group('MerchantQrStatusPage (ver estado)', () {
    Future<void> pumpStatus(
      WidgetTester tester,
      FakeMerchantQrService service,
      String chargeId,
    ) async {
      await tester.pumpWidget(
        MaterialApp(
          home: MerchantQrStatusPage(chargeId: chargeId, service: service),
        ),
      );
      await tester.pumpAndSettle();
    }

    FakeMerchantQrService seedWith(String status) {
      final service = FakeMerchantQrService();
      service.statusesById['chg-1'] = QrChargeStatus(
        chargeId: 'chg-1',
        status: status,
        amountMinor: 2550,
        currency: 'PEN',
        expiresAt: service.expiresAt(),
        paidAt: status == 'PAID'
            ? DateTime.now().toUtc().toIso8601String()
            : null,
      );
      return service;
    }

    testWidgets('muestra carga mientras consulta', (tester) async {
      final service = FakeMerchantQrService()..hangStatus = true;
      await tester.pumpWidget(
        MaterialApp(
          home: MerchantQrStatusPage(chargeId: 'chg-1', service: service),
        ),
      );
      await tester.pump();

      expect(
        find.byKey(const Key('merchant-qr-status-loading')),
        findsOneWidget,
      );
    });

    testWidgets('vacio sin identificador ofrece generar', (tester) async {
      await pumpStatus(tester, FakeMerchantQrService(), '');

      expect(find.textContaining('Sin cobro seleccionado'), findsOneWidget);
      expect(find.text('Generar cobro'), findsOneWidget);
    });

    testWidgets('estado pendiente muestra badge Pendiente', (tester) async {
      await pumpStatus(tester, seedWith('ACTIVE'), 'chg-1');

      expect(find.text('Pendiente'), findsOneWidget);
      expect(find.text('S/ 25.50'), findsOneWidget);
      expect(
        find.byKey(const Key('merchant-qr-status-expires')),
        findsOneWidget,
      );
    });

    testWidgets('estado pagado muestra badge Pagado', (tester) async {
      await pumpStatus(tester, seedWith('PAID'), 'chg-1');

      expect(find.text('Pagado'), findsOneWidget);
      expect(
        find.byKey(const Key('merchant-qr-status-paid')),
        findsOneWidget,
      );
    });

    testWidgets('estado vencido muestra badge Vencido (del servidor)',
        (tester) async {
      final service = FakeMerchantQrService();
      service.statusesById['chg-9'] = QrChargeStatus(
        chargeId: 'chg-9',
        status: 'EXPIRED',
        amountMinor: 1000,
        currency: 'PEN',
        expiresAt: DateTime.now()
            .toUtc()
            .subtract(const Duration(minutes: 10))
            .toIso8601String(),
      );
      await pumpStatus(tester, service, 'chg-9');

      expect(find.text('Vencido'), findsOneWidget);
      expect(find.text('S/ 10.00'), findsOneWidget);
    });

    testWidgets('404 muestra error con Reintentar y reintenta',
        (tester) async {
      final service = FakeMerchantQrService()
        ..statusError = ApiException(
          code: 'NOT_FOUND',
          message: 'No encontramos lo que buscas.',
          statusCode: 404,
        );
      await pumpStatus(tester, service, 'chg-1');

      expect(
        find.text('No encontramos ese cobro. Verifica el identificador.'),
        findsOneWidget,
      );
      expect(find.text('Reintentar'), findsOneWidget);

      service.statusError = null;
      service.statusesById['chg-1'] = const QrChargeStatus(
        chargeId: 'chg-1',
        status: 'ACTIVE',
        amountMinor: 2550,
        currency: 'PEN',
      );
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();

      expect(find.text('Pendiente'), findsOneWidget);
    });

    testWidgets('error de red muestra Reintentar', (tester) async {
      final service = FakeMerchantQrService()
        ..statusError = ApiException.network();
      await pumpStatus(tester, service, 'chg-1');

      expect(find.textContaining('Ocurrió un error al consultar'), findsOneWidget);
      expect(find.text('Reintentar'), findsOneWidget);
    });

    testWidgets('actualizar estado re-consulta al servidor', (tester) async {
      final service = seedWith('ACTIVE');
      await pumpStatus(tester, service, 'chg-1');
      final callsBefore = service.statusCalls;

      await tester.tap(find.byKey(const Key('merchant-qr-status-refresh')));
      await tester.pumpAndSettle();

      expect(service.statusCalls, greaterThan(callsBefore));
      expect(find.text('Pendiente'), findsOneWidget);
    });
  });

  group('MerchantQrService (HTTP)', () {
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

    const createBody = {
      'data': {
        'charge_id': 'chg-1',
        'qr_content': kSampleQrContent,
        'payload': {
          'v': 'BO-QR1',
          'charge_id': 'chg-1',
          'amount_minor': 2550,
          'currency': 'PEN',
          'mode': 'DYNAMIC',
        },
        'payload_hash': 'hash-1',
        'signature': 'sig-1',
        'amount_minor': 2550,
        'currency': 'PEN',
        'concept': 'Café',
        'mode': 'DYNAMIC',
        'status': 'ACTIVE',
        'expires_at': '2026-10-13T01:00:00Z',
        'created_at': '2026-10-13T00:55:00Z',
      },
      'meta': {'request_id': 'r-1', 'replayed': false},
    };

    const statusBody = {
      'data': {
        'charge_id': 'chg-1',
        'status': 'PAID',
        'amount_minor': 2550,
        'currency': 'PEN',
        'expires_at': '2026-10-13T01:00:00Z',
        'paid_at': '2026-10-13T00:57:00Z',
      },
      'meta': {'request_id': 'r-2'},
    };

    test('createCharge envia monto/concepto y parsea el QR tal cual',
        () async {
      Map<String, dynamic>? capturedBody;
      String? path;
      final api = stubClient(
        (o) => Response(requestOptions: o, statusCode: 201, data: createBody),
        onRequest: (o) {
          path = o.path;
          if (o.data is Map) {
            capturedBody = Map<String, dynamic>.from(o.data as Map);
          }
          return null;
        },
      );

      final charge = await MerchantQrService(api: api).createCharge(
        const QrChargeInput(
          amountMinor: 2550,
          currency: 'PEN',
          concept: 'Café',
        ),
      );

      expect(path, endsWith('/merchants/qr'));
      expect(capturedBody!['amount_minor'], 2550);
      expect(capturedBody!['currency'], 'PEN');
      expect(capturedBody!['concept'], 'Café');
      expect(capturedBody!['mode'], 'DYNAMIC');
      // Sin clave: no viaja `Idempotency-Key` (opcional, E4-T08).
      expect(charge.chargeId, 'chg-1');
      expect(charge.qrContent, kSampleQrContent);
      expect(charge.payload['charge_id'], 'chg-1');
      expect(charge.status, 'ACTIVE');
      expect(charge.expiresAt, '2026-10-13T01:00:00Z');
    });

    test('QrCharge.fromJson lee qr_content + payload objeto (legado string)',
        () {
      final charge = QrCharge.fromJson({
        'charge_id': 'chg-1',
        'qr_content': kSampleQrContent,
        'payload': {'v': 'BO-QR1', 'charge_id': 'chg-1'},
        'payload_hash': 'hash-1',
        'amount_minor': 2550,
        'currency': 'PEN',
        'mode': 'DYNAMIC',
        'status': 'ACTIVE',
      });
      expect(charge.qrContent, kSampleQrContent);
      expect(charge.payload['charge_id'], 'chg-1');

      // Legado E4-T09 (payload como String): se usa como contenido.
      final legacy = QrCharge.fromJson({
        'charge_id': 'chg-0',
        'payload': 'BO-QR1.legacy',
        'payload_hash': 'hash-0',
        'amount_minor': 100,
        'currency': 'PEN',
        'mode': 'DYNAMIC',
        'status': 'ACTIVE',
      });
      expect(legacy.qrContent, 'BO-QR1.legacy');
      expect(legacy.payload, isEmpty);
    });

    test('createCharge con clave envia Idempotency-Key reutilizable',
        () async {
      String? idempotencyKey;
      final api = stubClient(
        (o) => Response(requestOptions: o, statusCode: 201, data: createBody),
        onRequest: (o) {
          idempotencyKey = o.headers['Idempotency-Key']?.toString();
          return null;
        },
      );

      await MerchantQrService(api: api).createCharge(
        const QrChargeInput(amountMinor: 100),
        idempotencyKey: 'key-1',
      );

      expect(idempotencyKey, 'key-1');
    });

    test('getChargeStatus pide el cargo y parsea el estado efectivo',
        () async {
      String? path;
      final api = stubClient(
        (o) => Response(requestOptions: o, statusCode: 200, data: statusBody),
        onRequest: (o) {
          path = o.path;
          return null;
        },
      );

      final status =
          await MerchantQrService(api: api).getChargeStatus('chg-1');

      expect(path, endsWith('/merchants/qr/chg-1'));
      expect(status.chargeId, 'chg-1');
      expect(status.status, 'PAID');
      expect(status.isPaid, isTrue);
      expect(status.statusLabel, 'Pagado');
      expect(status.paidAt, '2026-10-13T00:57:00Z');
    });

    test('422 se mapea a VALIDATION_ERROR', () async {
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
                statusCode: 422,
                data: {
                  'error': {'code': 'VALIDATION_ERROR', 'message': 'Invalido.'},
                },
              ),
              type: DioExceptionType.badResponse,
            ),
          ),
        ),
      );

      try {
        await MerchantQrService(api: api).createCharge(
          const QrChargeInput(amountMinor: 0),
        );
        fail('debió lanzar ApiException');
      } on ApiException catch (e) {
        expect(e.code, 'VALIDATION_ERROR');
      }
    });

    test('404 en estado se mapea a NOT_FOUND neutro', () async {
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
                  'error': {'code': 'NOT_FOUND', 'message': 'No existe.'},
                },
              ),
              type: DioExceptionType.badResponse,
            ),
          ),
        ),
      );

      try {
        await MerchantQrService(api: api).getChargeStatus('ajeno');
        fail('debió lanzar ApiException');
      } on ApiException catch (e) {
        expect(e.code, 'NOT_FOUND');
        expect(e.statusCode, 404);
      }
    });
  });
}
