import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/home/presentation/home_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Fakes del servicio (seam `AccountsServiceBase`): sin red, con datos fijos
/// que replican los shapes del backend (solo shapes).
class _OkFake implements AccountsServiceBase {
  static const accounts = [
    Account(
      id: 'a1',
      accountNumberMasked: '****1234',
      type: 'AHORRO',
      currency: 'PEN',
      availableMinor: 42000,
      heldMinor: 8000,
      balanceMinor: 50000,
      status: 'ACTIVE',
    ),
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

  static const movements = [
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
  Future<Account> getAccountDetail(String accountId) async => accounts.first;

  @override
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  }) async =>
      MovementsPage(
        items: movements,
        page: 1,
        pageSize: pageSize,
        total: movements.length,
      );

  @override
  Future<ExportResult> exportMovements(
    String accountId, {
    required String format,
    required DateTime dateFrom,
    required DateTime dateTo,
  }) =>
      throw UnimplementedError();
}

class _EmptyFake extends _OkFake {
  @override
  Future<List<Account>> getAccounts() async => const [];
}

class _ErrorFake extends _OkFake {
  @override
  Future<List<Account>> getAccounts() async => throw ApiException(
        code: 'NETWORK_ERROR',
        message: 'Sin conexion. Revisa tu red e intentalo de nuevo.',
      );
}

/// Falla la primera carga y responde bien al reintentar.
class _FlakyFake extends _OkFake {
  var calls = 0;

  @override
  Future<List<Account>> getAccounts() async {
    calls++;
    if (calls == 1) {
      throw ApiException(
        code: 'NETWORK_ERROR',
        message: 'Sin conexion. Revisa tu red e intentalo de nuevo.',
      );
    }
    return _OkFake.accounts;
  }
}

class _HangingFake extends _OkFake {
  final completer = Completer<List<Account>>();

  @override
  Future<List<Account>> getAccounts() => completer.future;
}

Widget _harness(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  testWidgets('home real renderiza contenido y no el placeholder',
      (tester) async {
    await tester.pumpWidget(_harness(HomePage(service: _OkFake())));
    await tester.pumpAndSettle();

    expect(
      find.text('Pantalla de inicio (placeholder F-T01).'),
      findsNothing,
    );
    // Cabecera + consolidado + cuentas + movimientos (mockup F-T42).
    expect(find.text('Hola'), findsOneWidget);
    expect(find.text('2 cuentas'), findsOneWidget);
    expect(find.text('Mis cuentas'), findsOneWidget);
    expect(find.text('AHORRO ****1234'), findsOneWidget);
    expect(find.text('CORRIENTE ****5678'), findsOneWidget);
    expect(find.text('S/ 420.00 disponible'), findsOneWidget);
    expect(find.text('Últimos movimientos'), findsOneWidget);
    expect(find.text('Abono enero'), findsOneWidget);
    expect(find.text('Pago febrero'), findsOneWidget);
    expect(find.text('Ver todos los movimientos'), findsOneWidget);
    expect(find.text('Ver todas las cuentas'), findsOneWidget);
  });

  testWidgets('home muestra numeros enmascarados tal como llegan',
      (tester) async {
    await tester.pumpWidget(_harness(HomePage(service: _OkFake())));
    await tester.pumpAndSettle();

    // El enmascarado viaja del backend y se muestra sin tocar.
    expect(find.textContaining('****1234'), findsWidgets);
    expect(find.textContaining('****5678'), findsWidgets);
  });

  testWidgets('home no suma saldos en el cliente (cliente delgado)',
      (tester) async {
    await tester.pumpWidget(_harness(HomePage(service: _OkFake())));
    await tester.pumpAndSettle();

    // 420.00 + 1200.00 = 1620.00 jamas se calcula ni se muestra.
    expect(find.text('SALDO TOTAL'), findsNothing);
    expect(find.text('S/ 1,620.00'), findsNothing);
  });

  testWidgets('home cubre el estado cargando', (tester) async {
    await tester.pumpWidget(_harness(HomePage(service: _HangingFake())));
    await tester.pump();

    expect(find.text('Cargando tu inicio...'), findsOneWidget);
    expect(find.text('Hola'), findsOneWidget);
  });

  testWidgets('home cubre el estado vacio', (tester) async {
    await tester.pumpWidget(_harness(HomePage(service: _EmptyFake())));
    await tester.pumpAndSettle();

    expect(find.text('Aun no tienes cuentas.'), findsOneWidget);
    expect(find.text('Mis cuentas'), findsNothing);
  });

  testWidgets('home cubre error con reintento accionable', (tester) async {
    final fake = _FlakyFake();
    await tester.pumpWidget(_harness(HomePage(service: fake)));
    await tester.pumpAndSettle();

    expect(
      find.text('Sin conexion. Revisa tu red e intentalo de nuevo.'),
      findsOneWidget,
    );
    await tester.tap(find.text('Reintentar'));
    await tester.pumpAndSettle();

    expect(fake.calls, 2);
    expect(find.text('Mis cuentas'), findsOneWidget);
    expect(find.text('AHORRO ****1234'), findsOneWidget);
  });

  testWidgets('home sin servicio muestra error accionable', (tester) async {
    await tester.pumpWidget(_harness(const HomePage()));
    await tester.pumpAndSettle();

    expect(
      find.text('Servicio de cuentas no configurado.'),
      findsOneWidget,
    );
    expect(find.text('Reintentar'), findsOneWidget);
  });

  testWidgets('home con servicio que falla muestra su mensaje', (tester) async {
    await tester.pumpWidget(_harness(HomePage(service: _ErrorFake())));
    await tester.pumpAndSettle();

    expect(
      find.text('Sin conexion. Revisa tu red e intentalo de nuevo.'),
      findsOneWidget,
    );
  });
}
