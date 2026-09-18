import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/accounts/presentation/account_detail_page.dart';
import 'package:banca_online/features/accounts/presentation/dashboard_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Fake del servicio (seam `AccountsServiceBase`): sin red, con datos fijos
/// que replican los shapes del backend (solo shapes).
class FakeAccountsService implements AccountsServiceBase {
  String? lastMovementsDirection;
  Map<String, String>? lastExport;
  int exportCalls = 0;

  static const detail = Account(
    id: 'a1',
    accountNumberMasked: '****1234',
    type: 'AHORRO',
    currency: 'PEN',
    availableMinor: 42000,
    heldMinor: 8000,
    balanceMinor: 50000,
    status: 'ACTIVE',
  );

  static const accounts = [
    detail,
    Account(
      id: 'a2',
      accountNumberMasked: '****5678',
      type: 'CORRIENTE',
      currency: 'PEN',
      availableMinor: 120000,
      heldMinor: 0,
      balanceMinor: 120000,
      status: 'ACTIVE',
    ),
  ];

  static const allMovements = [
    Movement(
      journalEntryId: 'j1',
      direction: 'CREDIT',
      description: 'Abono enero',
      valueDate: '2026-01-10',
      amountMinor: 10000,
    ),
    Movement(
      journalEntryId: 'j2',
      direction: 'DEBIT',
      description: 'Pago febrero',
      valueDate: '2026-02-15',
      amountMinor: 2500,
    ),
  ];

  @override
  Future<List<Account>> getAccounts() async => accounts;

  @override
  Future<Account> getAccountDetail(String accountId) async => detail;

  @override
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  }) async {
    lastMovementsDirection = filters.direction;
    final items = filters.direction == null
        ? allMovements
        : allMovements
            .where((m) => m.direction == filters.direction)
            .toList();
    return MovementsPage(
      items: items,
      page: 1,
      pageSize: pageSize,
      total: items.length,
    );
  }

  @override
  Future<ExportResult> exportMovements(
    String accountId, {
    required String format,
    required DateTime dateFrom,
    required DateTime dateTo,
  }) async {
    exportCalls++;
    String iso(DateTime d) =>
        '${d.year.toString().padLeft(4, '0')}'
        '${d.month.toString().padLeft(2, '0')}'
        '${d.day.toString().padLeft(2, '0')}';
    lastExport = {
      'format': format,
      'date_from': iso(dateFrom),
      'date_to': iso(dateTo),
    };
    return ExportResult(
      bytes: const [65, 66, 67],
      suggestedFilename:
          'movimientos_${accountId}_${iso(dateFrom)}_${iso(dateTo)}.csv',
      contentType: 'text/csv',
    );
  }
}

Widget _harness(Widget child) =>
    MaterialApp(home: Scaffold(body: child));

void main() {
  testWidgets('dashboard renderiza 2 cuentas con saldos', (tester) async {
    await tester.pumpWidget(_harness(DashboardPage(service: FakeAccountsService())));
    await tester.pumpAndSettle();

    // Numeros enmascarados tal como vienen del backend.
    expect(find.text('AHORRO ****1234'), findsOneWidget);
    expect(find.text('CORRIENTE ****5678'), findsOneWidget);
    // Saldos diferenciados (centimos -> soles).
    expect(find.text('S/ 420.00'), findsOneWidget); // disponible a1
    expect(find.text('S/ 80.00'), findsOneWidget); // retenido a1
    expect(find.text('S/ 500.00'), findsOneWidget); // contable a1
    expect(find.text('S/ 1,200.00'), findsNWidgets(2)); // a2 disp.+cont.
  });

  testWidgets('detalle muestra disponible/retenido/contable', (tester) async {
    await tester.pumpWidget(
      _harness(AccountDetailPage(accountId: 'a1', service: FakeAccountsService())),
    );
    await tester.pumpAndSettle();

    expect(find.text('AHORRO ****1234'), findsOneWidget);
    expect(find.text('Disponible'), findsOneWidget);
    expect(find.text('Retenido'), findsOneWidget);
    expect(find.text('Contable'), findsOneWidget);
    expect(find.text('S/ 420.00'), findsOneWidget);
    expect(find.text('S/ 80.00'), findsOneWidget);
    expect(find.text('S/ 500.00'), findsOneWidget);
    // Movimientos iniciales sin filtro.
    expect(find.text('Abono enero'), findsOneWidget);
    expect(find.text('Pago febrero'), findsOneWidget);
  });

  testWidgets('filtro por direccion recarga solo abonos', (tester) async {
    final fake = FakeAccountsService();
    await tester.pumpWidget(
      _harness(AccountDetailPage(accountId: 'a1', service: fake)),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('directionFilter')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Abonos').last);
    await tester.pumpAndSettle();

    expect(fake.lastMovementsDirection, 'CREDIT');
    expect(find.text('Abono enero'), findsOneWidget);
    expect(find.text('Pago febrero'), findsNothing);
  });

  testWidgets('export invoca servicio con rango y muestra exito',
      (tester) async {
    final fake = FakeAccountsService();
    await tester.pumpWidget(
      _harness(
        AccountDetailPage(
          accountId: 'a1',
          service: fake,
          initialDateFrom: DateTime(2026, 2, 1),
          initialDateTo: DateTime(2026, 3, 31),
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('exportButton')));
    await tester.pumpAndSettle();
    expect(find.text('Exportar movimientos'), findsOneWidget);
    // Dialogo de guardado con el rango precargado de los filtros
    // (uno en la barra de filtros de la pagina, otro en el dialogo).
    expect(find.text('Desde: 2026-02-01'), findsWidgets);

    await tester.tap(find.byKey(const Key('confirmExport')));
    await tester.pumpAndSettle();

    expect(fake.exportCalls, 1);
    expect(fake.lastExport, {
      'format': 'csv',
      'date_from': '20260201',
      'date_to': '20260331',
    });
    // Dialogo de exito con nombre sugerido.
    expect(find.text('Exportacion lista'), findsOneWidget);
    expect(
      find.textContaining('movimientos_a1_20260201_20260331.csv'),
      findsOneWidget,
    );
  });
}
