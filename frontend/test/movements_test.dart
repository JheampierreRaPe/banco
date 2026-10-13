import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/movements/data/movements_service.dart';
import 'package:banca_online/features/movements/models/movement.dart';
import 'package:banca_online/features/movements/presentation/movement_detail_page.dart';
import 'package:banca_online/features/movements/presentation/movements_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del servicio (seam `MovementsServiceBase`): sin red, con datos que
/// replican los shapes E2-T24/E2-T25.
class FakeMovementsService implements MovementsServiceBase {
  FakeMovementsService({
    List<Movement>? seed,
    this.hangList = false,
    this.hangDetail = false,
    Map<String, MovementDetail>? details,
  })  : items = List<Movement>.from(seed ?? _defaultSeed),
        details = details ?? _defaultDetails();

  static final _now = DateTime.now();

  static List<Movement> get _defaultSeed => [
        Movement(
          transactionId: 'tx-settled-1',
          type: 'THIRD_PARTY_TRANSFER',
          status: 'SETTLED',
          amountMinor: -35000,
          currency: 'PEN',
          createdAt: _now.subtract(const Duration(hours: 2)),
          sourceMasked: '•••• 8921',
          targetMasked: '•••• 7744',
          externalRef: 'OP-20240827-8841293',
        ),
        Movement(
          transactionId: 'tx-pending-1',
          type: 'INTERBANK_TRANSFER',
          status: 'PENDING',
          amountMinor: -8940,
          currency: 'PEN',
          createdAt: _now.subtract(const Duration(hours: 5)),
          sourceMasked: '•••• 8921',
          targetMasked: 'CCI •••• 4401',
          externalRef: 'OP-20240827-8841300',
        ),
        Movement(
          transactionId: 'tx-reversed-1',
          type: 'THIRD_PARTY_TRANSFER',
          status: 'REVERSED',
          amountMinor: 14250,
          currency: 'PEN',
          createdAt: _now.subtract(const Duration(days: 1)),
          sourceMasked: '•••• 8921',
          targetMasked: '•••• 9920',
          externalRef: 'OP-20240826-8811223',
        ),
      ];

  static Map<String, MovementDetail> _defaultDetails() => {
        'tx-settled-1': MovementDetail(
          transactionId: 'tx-settled-1',
          type: 'THIRD_PARTY_TRANSFER',
          status: 'SETTLED',
          amountMinor: -35000,
          currency: 'PEN',
          feeMinor: 350,
          externalRef: 'OP-20240827-8841293',
          tracking: const {'cce_tracking': 'CCE-TR-992014481'},
          createdAt: _now.subtract(const Duration(hours: 2)),
          sourceAccountId: '11111111-1111-1111-1111-111111118921',
          targetAccountId: '22222222-2222-2222-2222-222222227744',
        ),
        'tx-pending-1': MovementDetail(
          transactionId: 'tx-pending-1',
          type: 'INTERBANK_TRANSFER',
          status: 'PENDING',
          amountMinor: -8940,
          currency: 'PEN',
          createdAt: _now.subtract(const Duration(hours: 5)),
        ),
        'tx-reversed-1': MovementDetail(
          transactionId: 'tx-reversed-1',
          type: 'THIRD_PARTY_TRANSFER',
          status: 'REVERSED',
          amountMinor: 14250,
          currency: 'PEN',
          createdAt: _now.subtract(const Duration(days: 1)),
        ),
        'tx-expired-1': MovementDetail(
          transactionId: 'tx-expired-1',
          type: 'INTERBANK_TRANSFER',
          status: 'SETTLED',
          amountMinor: -10000,
          currency: 'PEN',
          createdAt: _now.subtract(const Duration(days: 4)),
        ),
      };

  List<Movement> items;
  final Map<String, MovementDetail> details;
  final bool hangList;
  final bool hangDetail;

  int listCalls = 0;
  MovementFilters? lastFilters;
  int? lastPage;

  int reverseCalls = 0;
  String? lastReversedId;
  String? lastReason;
  String? lastIdempotencyKey;

  /// Si no es `null`, `list`/`detail` lanzan este error (estado error).
  ApiException? listError;
  ApiException? detailError;

  /// Si no es `null`, `reverse` lanza este error (p. ej. 409).
  ApiException? reverseError;

  int receiptCalls = 0;

  @override
  Future<PaginatedMovements> list({
    int page = 1,
    int pageSize = 10,
    MovementFilters filters = const MovementFilters(),
  }) async {
    listCalls++;
    lastFilters = filters;
    lastPage = page;
    if (listError != null) throw listError!;
    if (hangList) await Completer<void>().future;
    // El "servidor" aplica tipo/estado/fecha; el texto es local en la UI.
    var scoped = List<Movement>.from(items);
    if (filters.type != null) {
      scoped = scoped.where((m) => m.type == filters.type).toList();
    }
    if (filters.status != null) {
      scoped = scoped.where((m) => m.status == filters.status).toList();
    }
    if (filters.dateFrom != null) {
      scoped = scoped
          .where((m) =>
              m.createdAt == null ||
              !m.createdAt!.isBefore(filters.dateFrom!))
          .toList();
    }
    final start = (page - 1) * pageSize;
    if (start >= scoped.length) {
      return PaginatedMovements(
        items: const [],
        page: page,
        pageSize: pageSize,
        total: scoped.length,
      );
    }
    final end =
        (start + pageSize) > scoped.length ? scoped.length : start + pageSize;
    return PaginatedMovements(
      items: scoped.sublist(start, end),
      page: page,
      pageSize: pageSize,
      total: scoped.length,
    );
  }

  @override
  Future<MovementDetail> detail(String transactionId) async {
    if (detailError != null) throw detailError!;
    if (hangDetail) await Completer<void>().future;
    final found = details[transactionId];
    if (found == null) {
      throw ApiException(code: 'NOT_FOUND', message: 'No encontramos ese movimiento.');
    }
    return found;
  }

  @override
  Future<ReversalResult> reverse(
    String transactionId, {
    String? reason,
    String? idempotencyKey,
  }) async {
    reverseCalls++;
    lastReversedId = transactionId;
    lastReason = reason;
    lastIdempotencyKey = idempotencyKey;
    if (reverseError != null) throw reverseError!;
    return ReversalResult(transactionId: transactionId, status: 'REVERSED');
  }

  @override
  Future<MovementReceipt> receipt(String transactionId) async {
    receiptCalls++;
    return MovementReceipt(
      bytes: List<int>.filled(16, 0),
      suggestedFilename: 'constancia-$transactionId.pdf',
      contentType: 'application/pdf',
    );
  }
}

Widget _listHarness(FakeMovementsService fake) {
  final router = GoRouter(
    initialLocation: '/movements',
    routes: [
      GoRoute(
        path: '/movements',
        builder: (context, state) => MovementsPage(service: fake),
      ),
      GoRoute(
        path: '/movements/:id',
        builder: (context, state) => MovementDetailPage(
          transactionId: state.pathParameters['id'] ?? '',
          service: fake,
        ),
      ),
      GoRoute(
        path: '/profile',
        builder: (context, state) =>
            const Scaffold(body: Text('Perfil (fake)')),
      ),
    ],
  );
  return MaterialApp.router(routerConfig: router);
}

Widget _detailHarness(FakeMovementsService fake, String id) {
  return MaterialApp(
    home: MovementDetailPage(transactionId: id, service: fake),
  );
}

Future<void> _pumpList(WidgetTester tester, FakeMovementsService fake) async {
  await tester.pumpWidget(_listHarness(fake));
  await tester.pumpAndSettle();
}

/// Viewport alto para que el `ListView` perezoso construya el detalle
/// completo (hero + desglose + constancia + reverso) sin scroll en asserts.
void _tallViewport(WidgetTester tester) {
  tester.view.physicalSize = const Size(800, 2000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
}

Future<void> _pumpDetail(
  WidgetTester tester,
  FakeMovementsService fake,
  String id,
) async {
  _tallViewport(tester);
  await tester.pumpWidget(_detailHarness(fake, id));
  await tester.pumpAndSettle();
}

/// Lleva el widget al viewport antes de interactuar (el detalle es largo).
Future<void> _reveal(WidgetTester tester, Finder finder) async {
  await tester.ensureVisible(finder);
  await tester.pumpAndSettle();
}

void main() {
  group('MovementsPage (historial)', () {
    testWidgets('lista muestra movimientos con badges y montos', (tester) async {
      await _pumpList(tester, FakeMovementsService());

      expect(find.text('3 movimientos encontrados'), findsOneWidget);
      expect(find.text('SETTLED'), findsOneWidget);
      expect(find.text('PROCESANDO'), findsOneWidget);
      expect(find.text('REVERSADO'), findsOneWidget);
      expect(find.byKey(const Key('movement-card-tx-settled-1')), findsOneWidget);
      expect(find.textContaining('350.00'), findsWidgets);
    });

    testWidgets('muestra skeleton durante la carga', (tester) async {
      final fake = FakeMovementsService(hangList: true);
      await tester.pumpWidget(_listHarness(fake));
      await tester.pump();

      expect(find.byKey(const Key('movements-skeleton')), findsOneWidget);
    });

    testWidgets('vacio ofrece limpiar filtros y recargar', (tester) async {
      await _pumpList(tester, FakeMovementsService(seed: []));

      expect(find.text('No encontramos movimientos'), findsOneWidget);
      expect(find.text('Limpiar filtros y recargar'), findsWidgets);
    });

    testWidgets('error 500 muestra tarjeta y reintenta', (tester) async {
      final fake = FakeMovementsService()
        ..listError = ApiException(
          code: 'SERVER_ERROR',
          message: 'Ocurrió un error inesperado. Inténtalo más tarde.',
          statusCode: 500,
        );
      await tester.pumpWidget(_listHarness(fake));
      await tester.pumpAndSettle();

      expect(find.text('ERROR HTTP 500 · E2-T24-ERR'), findsOneWidget);
      expect(find.text('Fallo de sincronización bancaria'), findsOneWidget);

      fake.listError = null;
      await tester.tap(find.text('Reintentar consulta'));
      await tester.pumpAndSettle();

      expect(find.text('3 movimientos encontrados'), findsOneWidget);
    });

    testWidgets('filtro de tipo consulta al servidor', (tester) async {
      final fake = FakeMovementsService();
      await _pumpList(tester, fake);
      expect(fake.lastFilters?.type, isNull);

      await tester.tap(find.byKey(const Key('movements-filter-type')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('movements-type-INTERBANK_TRANSFER')));
      await tester.pumpAndSettle();

      expect(fake.lastFilters?.type, 'INTERBANK_TRANSFER');
      expect(find.text('1 movimientos encontrados'), findsOneWidget);
      expect(
        find.byKey(const Key('movement-card-tx-pending-1')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('movement-card-tx-settled-1')),
        findsNothing,
      );
    });

    testWidgets('filtro de estado consulta al servidor', (tester) async {
      final fake = FakeMovementsService();
      await _pumpList(tester, fake);

      await tester.tap(find.byKey(const Key('movements-filter-state')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('movements-status-REVERSED')));
      await tester.pumpAndSettle();

      expect(fake.lastFilters?.status, 'REVERSED');
      expect(find.text('1 movimientos encontrados'), findsOneWidget);
    });

    testWidgets('busqueda filtra en memoria sin nueva consulta', (tester) async {
      final fake = FakeMovementsService();
      await _pumpList(tester, fake);
      final callsBefore = fake.listCalls;

      await tester.enterText(
        find.byKey(const Key('movements-search')),
        '8841300',
      );
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();

      expect(fake.listCalls, callsBefore);
      expect(
        find.byKey(const Key('movement-card-tx-pending-1')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('movement-card-tx-settled-1')),
        findsNothing,
      );
    });
  });

  group('MovementDetailPage (reverso)', () {
    testWidgets('detalle reversible muestra desglose y accion habilitada',
        (tester) async {
      await _pumpDetail(tester, FakeMovementsService(), 'tx-settled-1');

      expect(find.text('DESGLOSE DE LA OPERACIÓN'), findsOneWidget);
      expect(find.text('Constancia Oficial (E2-T10)'), findsOneWidget);
      expect(
        find.text('Solicitar reverso de transacción'),
        findsOneWidget,
      );
      final button = tester.widget<FilledButton>(
        find.byKey(const Key('movement-reverse-action')),
      );
      expect(button.onPressed, isNotNull);
    });

    testWidgets('reverso OK con motivo: confirma, usa idempotencia y celebra',
        (tester) async {
      final fake = FakeMovementsService();
      await _pumpDetail(tester, fake, 'tx-settled-1');

      await _reveal(tester, find.byKey(const Key('movement-reverse-action')));
      await tester.tap(find.byKey(const Key('movement-reverse-action')));
      await tester.pumpAndSettle();

      expect(find.text('Solicitar reverso'), findsOneWidget);
      await tester.enterText(
        find.byKey(const Key('reverse-reason')),
        'Cobro duplicado por error',
      );
      await tester.pump();
      expect(find.text('25/280'), findsOneWidget);

      await tester.tap(find.byKey(const Key('reverse-confirm')));
      await tester.pumpAndSettle();

      expect(fake.reverseCalls, 1);
      expect(fake.lastReversedId, 'tx-settled-1');
      expect(fake.lastReason, 'Cobro duplicado por error');
      expect(fake.lastIdempotencyKey, isNotNull);
      expect(find.text('Operación Reversada con Éxito'), findsOneWidget);
      expect(find.text('Operación Ya Reversada'), findsOneWidget);
    });

    testWidgets('reverso no reversible (PENDING) va deshabilitado con ayuda',
        (tester) async {
      await _pumpDetail(tester, FakeMovementsService(), 'tx-pending-1');

      expect(
        find.text('Reverso no disponible (En tránsito)'),
        findsOneWidget,
      );
      expect(
        find.textContaining('Aún en compensación interbancaria'),
        findsOneWidget,
      );
      final button = tester.widget<FilledButton>(
        find.byKey(const Key('movement-reverse-action')),
      );
      expect(button.onPressed, isNull);
    });

    testWidgets('reverso expirado (> 72h) va deshabilitado', (tester) async {
      await _pumpDetail(tester, FakeMovementsService(), 'tx-expired-1');

      expect(find.text('Plazo límite superado (> 72h)'), findsOneWidget);
      expect(
        find.textContaining('Plazo límite superado'),
        findsWidgets,
      );
      final button = tester.widget<FilledButton>(
        find.byKey(const Key('movement-reverse-action')),
      );
      expect(button.onPressed, isNull);
    });

    testWidgets('409 informa conflicto sin duplicar', (tester) async {
      final fake = FakeMovementsService()
        ..reverseError = ApiException(
          code: 'CONFLICT',
          message: 'Conflicto.',
          statusCode: 409,
        );
      await _pumpDetail(tester, fake, 'tx-settled-1');

      await _reveal(tester, find.byKey(const Key('movement-reverse-action')));
      await tester.tap(find.byKey(const Key('movement-reverse-action')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('reverse-confirm')));
      await tester.pumpAndSettle();

      expect(fake.reverseCalls, 1);
      expect(find.textContaining('ya fue reversada o está en conflicto'),
          findsOneWidget);
      // Sin banner de exito: no se duplico.
      expect(find.text('Operación Reversada con Éxito'), findsNothing);
    });

    testWidgets('descargar constancia informa archivo pendiente', (tester) async {
      final fake = FakeMovementsService();
      await _pumpDetail(tester, fake, 'tx-settled-1');

      await _reveal(tester, find.byKey(const Key('movement-download-receipt')));
      await tester.tap(find.byKey(const Key('movement-download-receipt')));
      await tester.pumpAndSettle();

      expect(fake.receiptCalls, 1);
      expect(find.textContaining('constancia-tx-settled-1.pdf'), findsOneWidget);
    });

    testWidgets('m5: avatar del historial navega a /profile', (tester) async {
      final fake = FakeMovementsService();
      await tester.pumpWidget(_listHarness(fake));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('movements-avatar')), findsOneWidget);
      await tester.tap(find.byKey(const Key('movements-avatar')));
      await tester.pumpAndSettle();

      expect(find.text('Perfil (fake)'), findsOneWidget);
    });

    testWidgets('m5: avatar del detalle navega a /profile', (tester) async {
      final fake = FakeMovementsService();
      await tester.pumpWidget(_listHarness(fake));
      await tester.pumpAndSettle();

      // Entrar al detalle tocando la primera tarjeta y luego al avatar.
      await tester.tap(find.byKey(const Key('movement-card-tx-settled-1')));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('movement-detail-avatar')),
        findsOneWidget,
      );
      await tester.tap(find.byKey(const Key('movement-detail-avatar')));
      await tester.pumpAndSettle();

      expect(find.text('Perfil (fake)'), findsOneWidget);
    });
  });

  group('Auditoria M6/M7/M8/m2 (copys honestos + datos reales)', () {
    testWidgets('m8: titulo en español y cabecera con datos reales',
        (tester) async {
      await _pumpList(tester, FakeMovementsService());

      expect(find.text('Historial de movimientos'), findsOneWidget);
      expect(find.text('Transaction History'), findsNothing);
      // Cabecera alimentada del primer movimiento (fuente/destino enmascarado
      // + moneda del servidor), sin datos inventados.
      expect(find.text('Cuenta •••• 8921'), findsOneWidget);
      expect(find.text('Soles (PEN)'), findsOneWidget);
      expect(find.text('Cuenta Principal CuyCash'), findsNothing);
      expect(find.textContaining('8291'), findsNothing);
    });

    testWidgets('m8: sin movimientos la cabecera es neutra', (tester) async {
      await _pumpList(tester, FakeMovementsService(seed: []));

      expect(find.text('Mis movimientos'), findsOneWidget);
      expect(find.text('Todas las cuentas'), findsOneWidget);
    });

    testWidgets('m6: extracto no promete correo; indica descarga real',
        (tester) async {
      await _pumpList(tester, FakeMovementsService());

      await tester.tap(find.byKey(const Key('movements-extract')));
      await tester.pumpAndSettle();

      expect(find.textContaining('detalle de tu cuenta'), findsOneWidget);
      expect(find.textContaining('correo'), findsNothing);
    });

    testWidgets('m6: reportar no simula envio; remite a soporte',
        (tester) async {
      final fake = FakeMovementsService()
        ..listError = ApiException(
          code: 'SERVER_ERROR',
          message: 'Ocurrió un error inesperado. Inténtalo más tarde.',
          statusCode: 500,
        );
      await tester.pumpWidget(_listHarness(fake));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('movements-report')));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('Si necesitas ayuda con este error'),
        findsOneWidget,
      );
      expect(find.textContaining('Reporte enviado'), findsNothing);
    });

    testWidgets('m7: un solo estado vacio (sin EmptyView duplicado)',
        (tester) async {
      await _pumpList(tester, FakeMovementsService(seed: []));

      expect(find.text('No encontramos movimientos'), findsOneWidget);
      expect(find.text('Limpiar filtros y recargar'), findsOneWidget);
      expect(find.text('Sin movimientos en este rango.'), findsNothing);
    });

    testWidgets('m2: propia en USD sin textos CCE; moneda real',
        (tester) async {
      final fake = FakeMovementsService();
      fake.details['tx-own-usd'] = MovementDetail(
        transactionId: 'tx-own-usd',
        type: 'OWN_TRANSFER',
        status: 'SETTLED',
        amountMinor: -50000,
        currency: 'USD',
        createdAt: DateTime.now().subtract(const Duration(hours: 1)),
        sourceAccountId: 'aaaaaaaa-1111-1111-1111-111111111111',
        targetAccountId: 'bbbbbbbb-2222-2222-2222-222222222222',
      );
      await _pumpDetail(tester, fake, 'tx-own-usd');

      expect(find.text('Detalle de movimiento'), findsOneWidget);
      expect(find.text('USD'), findsOneWidget);
      expect(find.text('PEN'), findsNothing);
      expect(find.text('Transf. propia'), findsWidgets);
      expect(find.textContaining('Transferencia CCE'), findsNothing);
      expect(find.text('Interbancaria diferida'), findsNothing);
      expect(find.text('Comisión servicio CCE'), findsNothing);
      expect(find.text('Comisión del servicio'), findsOneWidget);
      expect(find.text('DNI'), findsNothing);
      expect(find.text('Tracking CCE:'), findsNothing);
      expect(find.text('SETTLED · Liquidado'), findsOneWidget);
    });

    testWidgets('m2: interbancaria conserva rotulos CCE', (tester) async {
      final fake = FakeMovementsService();
      fake.details['tx-cce-1'] = MovementDetail(
        transactionId: 'tx-cce-1',
        type: 'INTERBANK_TRANSFER',
        status: 'SETTLED',
        amountMinor: -10000,
        currency: 'PEN',
        feeMinor: 50,
        tracking: const {'cce_tracking': 'CCE-TR-1'},
        createdAt: DateTime.now().subtract(const Duration(hours: 1)),
      );
      await _pumpDetail(tester, fake, 'tx-cce-1');

      expect(find.textContaining('Transferencia CCE'), findsOneWidget);
      expect(find.text('Interbancaria diferida'), findsOneWidget);
      expect(find.text('Comisión servicio CCE'), findsOneWidget);
      expect(find.text('Tracking CCE:'), findsOneWidget);
      expect(find.text('CCE-TR-1'), findsOneWidget);
      expect(find.text('SETTLED · Liquidado CCE'), findsOneWidget);
    });
  });
}
