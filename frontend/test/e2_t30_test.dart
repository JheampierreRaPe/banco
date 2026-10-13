import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/transfers/data/transfers_service.dart';
import 'package:banca_online/features/transfers/models/resolve_target.dart';
import 'package:banca_online/features/transfers/models/transfer.dart';
import 'package:banca_online/features/transfers/presentation/destination_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// E2-T30: la agenda envia `beneficiary_id` (no el id del beneficiario
/// como `target_account_id`); el flujo DNI/celular sigue igual.
///
/// Sin PII: solo UUIDs sinteticos y mascaras.

const _a1 = Account(
  id: 'a1',
  accountNumberMasked: '•••• 8921',
  type: 'SAVINGS',
  currency: 'PEN',
  availableMinor: 485020,
  heldMinor: 0,
  balanceMinor: 485020,
);

const _b1 = Beneficiary(
  id: 'b1',
  alias: 'Mamá Lima',
  accountMasked: '•••• 7744',
  bankCode: 'BCP',
  holderName: 'M. Rojas ••••',
  currency: 'PEN',
);

/// Fake minimo del servicio (solo agenda + cuentas para `DestinationPage`).
class _AgendaFake implements TransfersServiceBase {
  @override
  Future<List<Account>> accounts() async => const [_a1];

  @override
  Future<List<Beneficiary>> beneficiaries({String? query}) async =>
      const [_b1];

  @override
  Future<TransferResult> createOwnTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) => throw UnimplementedError();

  @override
  Future<TransferResult> createThirdPartyTransfer({
    required String sourceAccountId,
    String? targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    int? feeMinor,
    String? beneficiaryId,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) => throw UnimplementedError();

  @override
  Future<TransferResult> createInterbankTransfer({
    required String sourceAccountId,
    required int amountMinor,
    required String cci,
    String currency = 'PEN',
    String? holderName,
    String? bankCode,
    int? feeMinor,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) => throw UnimplementedError();

  @override
  Future<ResolvedTarget> resolveTarget({
    String? documentType,
    String? documentNumber,
    String? phone,
  }) => throw UnimplementedError();

  @override
  Future<TransactionDetail> getTransaction(String transactionId) =>
      throw UnimplementedError();

  @override
  Future<ReceiptResult> getReceipt(String transactionId) =>
      throw UnimplementedError();
}

/// Servicio real sobre un `Dio` capturado (sin red): responde `201 SETTLED`.
TransfersService _capturedService(
  void Function(Map<String, dynamic> body) onBody,
) {
  final dio = Dio(BaseOptions(baseUrl: 'http://localhost'));
  final client = ApiClient.create(
    session: InMemorySessionRepository(),
    baseUrl: 'http://localhost/api/v1',
    dioOverride: dio,
  );
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        onBody(Map<String, dynamic>.from(options.data as Map));
        handler.resolve(
          Response(
            requestOptions: options,
            statusCode: 201,
            data: {
              'data': {
                'transaction_id': 'tx-1',
                'type': 'THIRD_PARTY_TRANSFER',
                'status': 'SETTLED',
                'source_account_id': 'a1',
                'target_account_id': 'real-account-uuid',
                'amount_minor': 10000,
                'currency': 'PEN',
                'fee_minor': 0,
              },
              'meta': {'request_id': 'r1', 'replayed': false},
            },
          ),
        );
      },
    ),
  );
  return TransfersService(api: client);
}

void main() {
  group('E2-T30 cuerpo del POST /transfers/third-party', () {
    test('agenda: beneficiary_id top-level, sin target_account_id', () async {
      Map<String, dynamic>? sent;
      final service = _capturedService((b) => sent = b);

      final result = await service.createThirdPartyTransfer(
        sourceAccountId: 'a1',
        amountMinor: 10000,
        beneficiaryId: 'b1',
      );

      expect(result.status, 'SETTLED');
      expect(sent, isNotNull);
      expect(sent!['source_account_id'], 'a1');
      expect(sent!['beneficiary_id'], 'b1');
      expect(sent!.containsKey('target_account_id'), isFalse);
      final meta = sent!['metadata'] as Map;
      expect(meta['beneficiary_id'], 'b1');
    });

    test('DNI/celular: target_account_id real, sin beneficiary_id', () async {
      Map<String, dynamic>? sent;
      final service = _capturedService((b) => sent = b);

      await service.createThirdPartyTransfer(
        sourceAccountId: 'a1',
        targetAccountId: 'real-account-uuid',
        amountMinor: 10000,
      );

      expect(sent, isNotNull);
      expect(sent!['target_account_id'], 'real-account-uuid');
      expect(sent!.containsKey('beneficiary_id'), isFalse);
    });
  });

  group('E2-T30 borrador de agenda', () {
    testWidgets('lleva beneficiaryId y sin targetAccountId', (tester) async {
      TransferFlowArgs? captured;
      final router = GoRouter(
        initialLocation: '/transfers/destination?kind=third',
        routes: [
          GoRoute(
            path: '/transfers/destination',
            builder: (context, state) => DestinationPage(
              kind: TransferKind.fromQuery(
                state.uri.queryParameters['kind'],
              ),
              service: _AgendaFake(),
            ),
          ),
          GoRoute(
            path: '/transfers/amount',
            builder: (context, state) {
              captured = state.extra as TransferFlowArgs;
              return const Scaffold(body: Text('monto'));
            },
          ),
        ],
      );
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('transfer-beneficiary-card-b1')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('destination-continue')));
      await tester.pumpAndSettle();

      expect(find.text('monto'), findsOneWidget);
      expect(captured, isNotNull);
      expect(captured!.draft.kind, TransferKind.thirdParty);
      expect(captured!.draft.beneficiaryId, 'b1');
      expect(captured!.draft.targetAccountId, isNull);
      expect(captured!.draft.beneficiaryAlias, 'Mamá Lima');
    });
  });
}
