import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

/// Servicio mockeado a nivel HTTP: `dio` resuelve respuestas enlatadas sin
/// salir a red (mismo patron que `test/api_client_test.dart`).
///
/// Shapes copiados de `backend/tests/test_accounts_endpoints.py` y
/// `test_accounts_movements_endpoints.py` (solo shapes, no implementacion).
class _Stub {
  _Stub(this.handler);

  final Response<dynamic> Function(RequestOptions options) handler;

  ApiClient client() {
    final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
    final api = ApiClient.create(
      session: InMemorySessionRepository(initialAccessToken: 'token'),
      dioOverride: dio,
    );
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, h) => h.resolve(handler(options)),
      ),
    );
    return api;
  }
}

const _accountsBody = {
  'data': [
    {
      'id': 'a1',
      'account_number_masked': '****1234',
      'type': 'AHORRO',
      'currency': 'PEN',
      'available_minor': 42000,
      'held_minor': 8000,
      'balance_minor': 50000,
    },
    {
      'id': 'a2',
      'account_number_masked': '****5678',
      'type': 'CORRIENTE',
      'currency': 'PEN',
      'available_minor': 120000,
      'held_minor': 0,
      'balance_minor': 120000,
    },
  ],
  'meta': {'total': 2, 'request_id': 'req-1'},
};

void main() {
  test('getAccounts renderiza 2 cuentas con saldos (shape backend)', () async {
    final api = _Stub(
      (o) => Response(requestOptions: o, statusCode: 200, data: _accountsBody),
    ).client();
    final service = AccountsService(api: api);

    final accounts = await service.getAccounts();

    expect(accounts, hasLength(2));
    expect(accounts[0].accountNumberMasked, '****1234');
    expect(
      (accounts[0].availableMinor, accounts[0].heldMinor, accounts[0].balanceMinor),
      (42000, 8000, 50000),
    );
    expect(accounts[1].accountNumberMasked, '****5678');
  });

  test('getAccountDetail muestra disponible/retenido/contable + estado',
      () async {
    final api = _Stub(
      (o) => Response(
        requestOptions: o,
        statusCode: 200,
        data: {
          'data': {
            'id': 'a1',
            'account_number_masked': '****1234',
            'type': 'AHORRO',
            'currency': 'PEN',
            'available_minor': 42000,
            'held_minor': 8000,
            'balance_minor': 50000,
            'status': 'ACTIVE',
          },
          'meta': {'request_id': 'req-2'},
        },
      ),
    ).client();

    final Account detail =
        await AccountsService(api: api).getAccountDetail('a1');

    expect(detail.balanceMinor, detail.availableMinor + detail.heldMinor);
    expect(detail.status, 'ACTIVE');
    expect(detail.accountNumberMasked, '****1234');
  });

  test('getMovements pagina y filtra por fecha/direccion', () async {
    Map<String, dynamic>? capturedQuery;
    String? capturedPath;
    final api = _Stub((o) {
      capturedQuery = Map<String, dynamic>.from(o.queryParameters);
      capturedPath = o.path;
      return Response(
        requestOptions: o,
        statusCode: 200,
        data: {
          'data': [
            {
              'journal_entry_id': 'j1',
              'direction': 'DEBIT',
              'amount_minor': 2500,
              'currency': 'PEN',
              'description': 'Pago febrero',
              'value_date': '2026-02-15',
            },
          ],
          'meta': {'page': 2, 'page_size': 1, 'total': 2},
        },
      );
    }).client();

    final page = await AccountsService(api: api).getMovements(
      'a1',
      page: 2,
      pageSize: 1,
      filters: MovementFilters(
        dateFrom: DateTime(2026, 2, 1),
        dateTo: DateTime(2026, 2, 28),
        direction: 'DEBIT',
      ),
    );

    expect(capturedPath, contains('/accounts/a1/movements'));
    expect(
      capturedQuery,
      containsPair('direction', 'DEBIT'),
    );
    expect(capturedQuery!['date_from'], '2026-02-01');
    expect(capturedQuery!['date_to'], '2026-02-28');
    expect(capturedQuery!['page'], 2);
    expect(page.total, 2);
    expect(page.items.single.description, 'Pago febrero');
    expect(page.items.single.isCredit, isFalse);
  });

  test('exportMovements invoca servicio con rango y devuelve bytes+nombre',
      () async {
    Map<String, dynamic>? capturedQuery;
    final csv = 'value_date,description,direction,amount_minor\n'
        '2026-02-15,Pago febrero,DEBIT,2500\n';
    final api = _Stub((o) {
      capturedQuery = Map<String, dynamic>.from(o.queryParameters);
      return Response(
        requestOptions: o,
        statusCode: 200,
        data: csv.codeUnits,
        headers: Headers.fromMap({
          'content-type': ['text/csv'],
          'content-disposition': [
            'attachment; filename="movimientos_a1.csv"',
          ],
        }),
      );
    }).client();

    final result = await AccountsService(api: api).exportMovements(
      'a1',
      format: 'csv',
      dateFrom: DateTime(2026, 2, 1),
      dateTo: DateTime(2026, 3, 31),
    );

    expect(capturedQuery!['format'], 'csv');
    expect(capturedQuery!['date_from'], '2026-02-01');
    expect(capturedQuery!['date_to'], '2026-03-31');
    expect(result.suggestedFilename, 'movimientos_a1.csv');
    expect(result.bytes, isNotEmpty);
    expect(result.contentType, contains('text/csv'));
  });
}
