import 'package:banca_online/features/movements/data/movements_service.dart';
import 'package:banca_online/features/movements/models/movement.dart';
import 'package:banca_online/features/movements/presentation/movement_detail_page.dart';
import 'package:banca_online/features/movements/presentation/movements_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Fake minimo del historial (seam `MovementsServiceBase`): un item para
/// que el historial pinte contenido.
class FakeBackMovementsService implements MovementsServiceBase {
  static final _now = DateTime.now();

  @override
  Future<PaginatedMovements> list({
    int page = 1,
    int pageSize = 10,
    MovementFilters filters = const MovementFilters(),
  }) async =>
      PaginatedMovements(
        items: [
          Movement(
            transactionId: 'tx-1',
            type: 'THIRD_PARTY_TRANSFER',
            status: 'SETTLED',
            amountMinor: -35000,
            currency: 'PEN',
            createdAt: _now,
            sourceMasked: '**** 8921',
            targetMasked: '**** 7744',
            externalRef: 'OP-1',
          ),
        ],
        page: page,
        pageSize: pageSize,
        total: 1,
      );

  @override
  Future<MovementDetail> detail(String transactionId) async => MovementDetail(
        transactionId: transactionId,
        type: 'THIRD_PARTY_TRANSFER',
        status: 'SETTLED',
        amountMinor: -35000,
        currency: 'PEN',
        createdAt: _now,
      );

  @override
  Future<ReversalResult> reverse(
    String transactionId, {
    String? reason,
    String? idempotencyKey,
  }) async =>
      ReversalResult(transactionId: transactionId, status: 'REVERSED');

  @override
  Future<MovementReceipt> receipt(String transactionId) async =>
      MovementReceipt(
        bytes: const [0, 1, 2],
        suggestedFilename: 'constancia-$transactionId.pdf',
        contentType: 'application/pdf',
      );
}

/// Raiz falsa con un boton que apila la pantalla bajo prueba con el
/// `Navigator` (hay pila -> el back debe aparecer y hacer `pop` real).
Widget _pushedHarness(Widget child) => MaterialApp(
      home: Builder(
        builder: (context) => Scaffold(
          body: Center(
            child: TextButton(
              key: const Key('back-harness-open'),
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute<void>(builder: (_) => child),
              ),
              child: const Text('Abrir'),
            ),
          ),
        ),
      ),
    );

void main() {
  group('retroceso en movimientos (E4-T25)', () {
    testWidgets('historial en raiz NO muestra back (sin pila)',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: MovementsPage(service: FakeBackMovementsService()),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Historial de movimientos'), findsOneWidget);
      // Sin pila no hay back muerto (criterio 2: raiz sin salida forzada).
      expect(find.byKey(const Key('movements-back')), findsNothing);
    });

    testWidgets('historial apilado muestra back y hace pop real',
        (tester) async {
      await tester.pumpWidget(
        _pushedHarness(MovementsPage(service: FakeBackMovementsService())),
      );
      await tester.tap(find.byKey(const Key('back-harness-open')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('movements-back')), findsOneWidget);

      await tester.tap(find.byKey(const Key('movements-back')));
      await tester.pumpAndSettle();
      // Volvio a la pantalla previa (pop real, sin salto a Home).
      expect(find.byKey(const Key('back-harness-open')), findsOneWidget);
      expect(find.text('Historial de movimientos'), findsNothing);
    });

    testWidgets('detalle en raiz NO muestra back (sin pila)',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: MovementDetailPage(
            transactionId: 'tx-1',
            service: FakeBackMovementsService(),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Detalle de movimiento'), findsOneWidget);
      expect(find.byKey(const Key('movement-detail-back')), findsNothing);
    });

    testWidgets('detalle apilado muestra back y hace pop real',
        (tester) async {
      await tester.pumpWidget(
        _pushedHarness(
          MovementDetailPage(
            transactionId: 'tx-1',
            service: FakeBackMovementsService(),
          ),
        ),
      );
      await tester.tap(find.byKey(const Key('back-harness-open')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('movement-detail-back')), findsOneWidget);

      await tester.tap(find.byKey(const Key('movement-detail-back')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('back-harness-open')), findsOneWidget);
      expect(find.text('Detalle de movimiento'), findsNothing);
    });
  });
}
