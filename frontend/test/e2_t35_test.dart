/// Regresion E2-T35 (dueno 2026-10-09): movimientos reales en el home.
///
/// - El preview muestra el movimiento real con etiqueta de negocio y signo
///   correctos (nunca un UUID), filtrando por `entry_type`.
/// - El preview no queda vacio por paginacion (se pagina hasta 3 visibles).
/// - El preview se recarga con *pull-to-refresh* y al re-entrar a `/home`.
/// - El historial de cuenta no muestra `settle <uuid>`.
///
/// Cada grupo FALLA sin el fix y PASA con el.
library;

import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/accounts/models/movement_concept.dart';
import 'package:banca_online/features/accounts/presentation/account_detail_page.dart';
import 'package:banca_online/features/home/data/profile_service.dart';
import 'package:banca_online/features/home/presentation/home_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

const _uuid = 'a06e5007-c25d-4e86-976d-fc44ab921ae5';

/// Fake contador: registra llamadas/paginas de `getMovements`.
class _CountingFake implements AccountsServiceBase {
  _CountingFake({required this.pages});

  /// Paginas servidas por numero (`page -> items`).
  final Map<int, List<Movement>> pages;

  int movementsCalls = 0;
  final List<int> requestedPages = [];
  final List<int> requestedSizes = [];

  static const account = Account(
    id: 'a1',
    accountNumberMasked: '****0013',
    type: 'AHORRO',
    currency: 'PEN',
    availableMinor: 10000,
    heldMinor: 0,
    balanceMinor: 10000,
    status: 'ACTIVE',
  );

  static const totals = AccountsTotals(
    asOf: '2026-10-09T12:00:00Z',
    primaryCurrency: 'PEN',
    primaryTotalMinor: 10000,
    totals: [],
  );

  @override
  Future<List<Account>> getAccounts() async => const [account];

  @override
  Future<AccountsTotals> getAccountsTotals() async => totals;

  @override
  Future<Account> getAccountDetail(String accountId) async => account;

  @override
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  }) async {
    movementsCalls++;
    requestedPages.add(page);
    requestedSizes.add(pageSize);
    final items = pages[page] ?? const <Movement>[];
    final total = pages.values.fold(0, (n, list) => n + list.length);
    return MovementsPage(
      items: items,
      page: page,
      pageSize: pageSize,
      total: total,
    );
  }

  @override
  Future<ExportResult> exportMovements(
    String accountId, {
    required String format,
    required DateTime dateFrom,
    required DateTime dateTo,
  }) =>
      throw UnimplementedError();
}

class _FakeProfile implements ProfileServiceBase {
  @override
  Future<Profile> getProfile() async =>
      const Profile(firstName: 'Juan', lastName: 'Perez');
}

/// Transferencia a tercero (origen): `TRANSFER_HOLD` DEBIT + `TRANSFER_SETTLE`
/// DEBIT. El preview muestra 1 fila "Transferencia" (cargo), nunca UUID.
Map<int, List<Movement>> _thirdPartyPages() => {
      1: [
        const Movement(
          journalEntryId: 'j-hold',
          direction: 'DEBIT',
          description: 'hold $_uuid',
          valueDate: '2026-10-09',
          amountMinor: 110000,
          entryType: 'TRANSFER_HOLD',
        ),
        const Movement(
          journalEntryId: 'j-settle',
          direction: 'DEBIT',
          description: 'settle $_uuid',
          valueDate: '2026-10-09',
          amountMinor: 110000,
          entryType: 'TRANSFER_SETTLE',
        ),
      ],
    };

void main() {
  group('E2-T35 mapper entry_type -> (etiqueta, interno)', () {
    test('visibles llevan su etiqueta y no son internos', () {
      expect(
        conceptForMovement(entryType: 'TRANSFER_SETTLE').label,
        'Transferencia',
      );
      expect(
        conceptForMovement(entryType: 'OWN_TRANSFER').label,
        'Transferencia entre mis cuentas',
      );
      expect(
        conceptForMovement(entryType: 'INTERBANK_SEND').label,
        'Transferencia interbancaria',
      );
      expect(
        conceptForMovement(entryType: 'HOLD_RELEASE').label,
        'Devolución por operación no completada',
      );
      expect(
        conceptForMovement(entryType: 'INTERBANK_RETURN').label,
        'Devolución de transferencia interbancaria',
      );
      expect(
        conceptForMovement(entryType: 'INTERBANK_SETTLE').label,
        'Comisión',
      );
      expect(conceptForMovement(entryType: 'REVERSAL').label, 'Reverso');
      for (final type in [
        'TRANSFER_SETTLE',
        'OWN_TRANSFER',
        'INTERBANK_SEND',
        'HOLD_RELEASE',
        'INTERBANK_RETURN',
        'INTERBANK_SETTLE',
        'REVERSAL',
      ]) {
        expect(
          isInternalMovement(entryType: type),
          isFalse,
          reason: type,
        );
      }
    });

    test('retenciones son internas (se ocultan)', () {
      expect(isInternalMovement(entryType: 'TRANSFER_HOLD'), isTrue);
      expect(isInternalMovement(entryType: 'INTERBANK_HOLD'), isTrue);
    });

    test('desconocido/NULL nunca renderiza texto crudo', () {
      expect(
        conceptForMovement(entryType: 'ALGO_NUEVO', description: 'x').label,
        'x',
      );
      expect(
        isInternalMovement(entryType: 'ALGO_NUEVO', description: 'x'),
        isFalse,
      );
      // Descripcion tecnica o con UUID -> generico, sin UUID visible.
      for (final raw in [
        'settle $_uuid',
        'hold $_uuid',
        'own settle $_uuid',
        'interbank send $_uuid',
        'hold release abc (tx $_uuid)',
        '',
        '   ',
      ]) {
        final concept = conceptForMovement(description: raw);
        expect(concept.label, 'Movimiento', reason: raw);
        expect(concept.label.contains(_uuid.substring(0, 8)), isFalse);
      }
    });
  });

  group('E2-T35 preview del home (CA-01/CA-03)', () {
    testWidgets('tercero: 1 fila "Transferencia" (cargo), sin UUID',
        (tester) async {
      final fake = _CountingFake(pages: _thirdPartyPages());
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: HomePage(service: fake, profileService: _FakeProfile()),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.text('ÚLTIMOS MOVIMIENTOS'),
        300,
      );
      await tester.pumpAndSettle();

      expect(find.textContaining(_uuid.substring(0, 8)), findsNothing);
      expect(find.text('Transferencia'), findsOneWidget);
      // Cargo de S/ 1,100.00 (110000 centimos, signo de `direction`).
      expect(find.text('-S/ 1,100.00'), findsOneWidget);
      // La pata interna no se muestra (solo 1 fila visible).
      expect(find.text('Retención por transferencia'), findsNothing);
    });

    testWidgets('propia: "Transferencia entre mis cuentas"', (tester) async {
      final fake = _CountingFake(pages: {
        1: [
          const Movement(
            journalEntryId: 'j-own',
            direction: 'DEBIT',
            description: 'own settle $_uuid',
            valueDate: '2026-10-09',
            amountMinor: 50000,
            entryType: 'OWN_TRANSFER',
          ),
        ],
      });
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: HomePage(service: fake, profileService: _FakeProfile()),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.text('ÚLTIMOS MOVIMIENTOS'),
        300,
      );
      await tester.pumpAndSettle();

      expect(find.text('Transferencia entre mis cuentas'), findsOneWidget);
      expect(find.textContaining(_uuid.substring(0, 8)), findsNothing);
    });

    testWidgets('legacy sin entry_type: descripcion tecnica -> "Movimiento"',
        (tester) async {
      final fake = _CountingFake(pages: {
        1: [
          const Movement(
            journalEntryId: 'j-legacy',
            direction: 'CREDIT',
            description: 'settle $_uuid',
            valueDate: '2026-10-08',
            amountMinor: 50000,
          ),
        ],
      });
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: HomePage(service: fake, profileService: _FakeProfile()),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.text('ÚLTIMOS MOVIMIENTOS'),
        300,
      );
      await tester.pumpAndSettle();

      expect(find.text('Movimiento'), findsOneWidget);
      expect(find.textContaining(_uuid.substring(0, 8)), findsNothing);
    });

    testWidgets('paginacion: primera pagina solo interna -> pide la siguiente',
        (tester) async {
      final fake = _CountingFake(pages: {
        1: [
          const Movement(
            journalEntryId: 'j-h1',
            direction: 'DEBIT',
            description: 'hold $_uuid',
            valueDate: '2026-10-09',
            amountMinor: 1000,
            entryType: 'TRANSFER_HOLD',
          ),
          const Movement(
            journalEntryId: 'j-h2',
            direction: 'CREDIT',
            description: 'hold $_uuid',
            valueDate: '2026-10-09',
            amountMinor: 1000,
            entryType: 'TRANSFER_HOLD',
          ),
        ],
        2: [
          const Movement(
            journalEntryId: 'j-real',
            direction: 'CREDIT',
            description: 'Abono sueldo',
            valueDate: '2026-10-08',
            amountMinor: 50000,
            entryType: 'TRANSFER_SETTLE',
          ),
        ],
      });
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: HomePage(service: fake, profileService: _FakeProfile()),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.text('ÚLTIMOS MOVIMIENTOS'),
        300,
      );
      await tester.pumpAndSettle();

      // Sin el fix el preview quedaba vacio (`take(3)` sobre 1 pagina de
      // internos): aqui se pidio la pagina 2 y se muestra lo real.
      expect(fake.requestedPages, contains(2));
      expect(find.text('Transferencia'), findsOneWidget);
      expect(find.text('Aun no tienes movimientos.'), findsNothing);
    });
  });

  group('E2-T35 refresh del preview (CA-02)', () {
    testWidgets('pull-to-refresh recarga el preview (segundo fetch)',
        (tester) async {
      final fake = _CountingFake(pages: _thirdPartyPages());
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: HomePage(service: fake, profileService: _FakeProfile()),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(fake.movementsCalls, 1);

      // Arrastre hacia abajo sobre la lista (RefreshIndicator).
      await tester.drag(find.byType(Scrollable).first, const Offset(0, 400));
      await tester.pumpAndSettle();

      expect(fake.movementsCalls, greaterThanOrEqualTo(2));
    });

    testWidgets('volver de /transfers a /home recarga el preview',
        (tester) async {
      final fake = _CountingFake(pages: _thirdPartyPages());
      final router = GoRouter(
        initialLocation: '/home',
        routes: [
          GoRoute(
            path: '/home',
            builder: (context, state) => HomePage(
              service: fake,
              profileService: _FakeProfile(),
            ),
          ),
          GoRoute(
            path: '/transfers',
            builder: (context, state) =>
                const Scaffold(body: Text('Transferencias (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();
      expect(fake.movementsCalls, 1);

      router.push('/transfers');
      await tester.pumpAndSettle();
      expect(find.text('Transferencias (fake)'), findsOneWidget);
      expect(fake.movementsCalls, 1);

      router.pop();
      await tester.pumpAndSettle();
      expect(fake.movementsCalls, 2);
    });
  });

  group('E2-T35 historial de cuenta (CA-04)', () {
    testWidgets('no muestra settle <uuid>; usa etiqueta y signo',
        (tester) async {
      final fake = _CountingFake(pages: {
        1: [
          const Movement(
            journalEntryId: 'j-settle',
            direction: 'DEBIT',
            description: 'settle $_uuid',
            valueDate: '2026-10-09',
            amountMinor: 110000,
            entryType: 'TRANSFER_SETTLE',
          ),
          const Movement(
            journalEntryId: 'j-hold',
            direction: 'DEBIT',
            description: 'hold $_uuid',
            valueDate: '2026-10-09',
            amountMinor: 110000,
            entryType: 'TRANSFER_HOLD',
          ),
        ],
      });
      await tester.pumpWidget(
        MaterialApp(home: AccountDetailPage(accountId: 'a1', service: fake)),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining(_uuid.substring(0, 8)), findsNothing);
      expect(find.textContaining('settle '), findsNothing);
      expect(find.text('Transferencia'), findsOneWidget);
      expect(find.text('-S/ 1,100.00'), findsOneWidget);
      // La pata interna no aparece en el historial.
      expect(find.text('Retención por transferencia'), findsNothing);
    });
  });
}
