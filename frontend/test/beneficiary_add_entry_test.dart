import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/home/data/profile_service.dart';
import 'package:banca_online/features/home/presentation/home_page.dart';
import 'package:banca_online/features/profile/data/biometric_consent_service.dart';
import 'package:banca_online/features/profile/presentation/profile_page.dart';
import 'package:banca_online/features/transfers/data/transfers_service.dart';
import 'package:banca_online/features/transfers/models/resolve_target.dart';
import 'package:banca_online/features/transfers/models/transfer.dart';
import 'package:banca_online/features/transfers/presentation/destination_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

const _a1 = Account(
  id: 'a1',
  accountNumberMasked: '•••• 8921',
  type: 'SAVINGS',
  currency: 'PEN',
  availableMinor: 485020,
  heldMinor: 0,
  balanceMinor: 485020,
);

const _a2 = Account(
  id: 'a2',
  accountNumberMasked: '•••• 4521',
  type: 'CHECKING',
  currency: 'PEN',
  availableMinor: 425080,
  heldMinor: 0,
  balanceMinor: 425080,
);

const _b1 = Beneficiary(
  id: 'b1',
  alias: 'Mamá Lima',
  accountMasked: '•••• 7744',
  bankCode: 'BCP',
  holderName: 'M. Rojas ••••',
  currency: 'PEN',
);

const _b2 = Beneficiary(
  id: 'b2',
  alias: 'Papá Depa',
  accountMasked: '•••• 1120',
  bankCode: 'BCP',
  holderName: 'P. Alvarado ••••',
  currency: 'PEN',
);

/// Fake mínimo del seam `TransfersServiceBase` para `DestinationPage`:
/// solo resuelve cuentas + agenda; el resto no participa del flujo.
class _FakeTransfers implements TransfersServiceBase {
  _FakeTransfers({List<Beneficiary>? beneficiaries})
    : beneficiariesSeed = List<Beneficiary>.from(
        beneficiaries ?? const [_b1],
      );

  List<Beneficiary> beneficiariesSeed;

  @override
  Future<List<Account>> accounts() async => const [_a1, _a2];

  @override
  Future<List<Beneficiary>> beneficiaries({String? query}) async =>
      List<Beneficiary>.from(beneficiariesSeed);

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
    required String targetAccountId,
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

class _FakeAccounts implements AccountsServiceBase {
  @override
  Future<List<Account>> getAccounts() async => const [_a1, _a2];

  @override
  Future<AccountsTotals> getAccountsTotals() async => const AccountsTotals(
    asOf: '2026-08-24T14:32:00Z',
    primaryCurrency: 'PEN',
    primaryTotalMinor: 99999,
    totals: [
      CurrencyTotal(
        currency: 'PEN',
        availableMinor: 99999,
        heldMinor: 0,
        totalMinor: 99999,
      ),
    ],
  );

  @override
  Future<Account> getAccountDetail(String accountId) async => _a1;

  @override
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  }) async => const MovementsPage(items: [], page: 1, pageSize: 3, total: 0);

  @override
  Future<ExportResult> exportMovements(
    String accountId, {
    required String format,
    required DateTime dateFrom,
    required DateTime dateTo,
  }) => throw UnimplementedError();
}

class _FakeProfile implements ProfileServiceBase {
  @override
  Future<Profile> getProfile() async =>
      const Profile(firstName: 'Ana', lastName: 'Lopez');
}

class _FakeConsent implements BiometricConsentServiceBase {
  @override
  Future<bool> setConsent({required bool enabled}) async => enabled;
}

void main() {
  group('Fix alta múltiple Nivel 1 — DestinationPage', () {
    testWidgets('Agregar beneficiario visible con lista no vacía', (
      tester,
    ) async {
      final fake = _FakeTransfers();
      final router = GoRouter(
        initialLocation: '/transfers/destination?kind=third',
        routes: [
          GoRoute(
            path: '/transfers/destination',
            builder: (context, state) =>
                DestinationPage(kind: TransferKind.thirdParty, service: fake),
          ),
          GoRoute(
            path: '/beneficiaries/new',
            builder: (context, state) =>
                const Scaffold(body: Text('Alta (stub)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      // La lista no vacía muestra su tarjeta Y la acción de alta.
      expect(
        find.byKey(const Key('transfer-beneficiary-card-b1')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('transfer-beneficiary-add')),
        findsOneWidget,
      );
      expect(find.text('Agregar beneficiario'), findsOneWidget);
      // Acción "Nuevo" del AppBar también visible en modo beneficiario.
      expect(
        find.byKey(const Key('destination-new-beneficiary')),
        findsOneWidget,
      );
    });

    testWidgets('refresca la lista tras alta (push devuelve true)', (
      tester,
    ) async {
      final fake = _FakeTransfers();
      final router = GoRouter(
        initialLocation: '/transfers/destination?kind=third',
        routes: [
          GoRoute(
            path: '/transfers/destination',
            builder: (context, state) =>
                DestinationPage(kind: TransferKind.thirdParty, service: fake),
          ),
          GoRoute(
            path: '/beneficiaries/new',
            builder: (context, state) => Scaffold(
              body: FilledButton(
                key: const Key('stub-confirm-new'),
                onPressed: () {
                  // El "backend" ya guardó el segundo beneficiario; el
                  // formulario devuelve `true` para disparar el refresh.
                  fake.beneficiariesSeed.add(_b2);
                  context.pop(true);
                },
                child: const Text('Guardar (stub)'),
              ),
            ),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(
        find.byKey(const Key('transfer-beneficiary-card-b1')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('transfer-beneficiary-card-b2')),
        findsNothing,
      );

      await tester.tap(find.byKey(const Key('transfer-beneficiary-add')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('stub-confirm-new')), findsOneWidget);

      await tester.tap(find.byKey(const Key('stub-confirm-new')));
      await tester.pumpAndSettle();

      // Tras el alta, la lista se recargó y el nuevo aparece de inmediato.
      expect(
        find.byKey(const Key('transfer-beneficiary-card-b1')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('transfer-beneficiary-card-b2')),
        findsOneWidget,
      );
    });
  });

  group('Fix alta múltiple Nivel 2 — acceso a la agenda', () {
    testWidgets('home expone Mis beneficiarios y navega a /beneficiaries', (
      tester,
    ) async {
      final router = GoRouter(
        initialLocation: '/home',
        routes: [
          GoRoute(
            path: '/home',
            builder: (context, state) => HomePage(
              service: _FakeAccounts(),
              profileService: _FakeProfile(),
            ),
          ),
          GoRoute(
            path: '/beneficiaries',
            builder: (context, state) =>
                const Scaffold(body: Text('Agenda (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      // La card vive bajo MIS CUENTAS en un ListView perezoso: se baja
      // hasta construirla antes de aseverar.
      for (
        var i = 0;
        i < 5 &&
            find.byKey(const Key('home-beneficiaries')).evaluate().isEmpty;
        i++
      ) {
        await tester.drag(find.byType(ListView).first, const Offset(0, -500));
        await tester.pumpAndSettle();
      }
      expect(find.byKey(const Key('home-beneficiaries')), findsOneWidget);
      await tester.tap(find.byKey(const Key('home-beneficiaries')));
      await tester.pumpAndSettle();

      expect(find.text('Agenda (fake)'), findsOneWidget);
    });

    testWidgets('perfil expone Mis beneficiarios y navega a /beneficiaries', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(390, 2000);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);

      final router = GoRouter(
        initialLocation: '/profile',
        routes: [
          GoRoute(
            path: '/profile',
            builder: (context, state) => ProfilePage(
              consentService: _FakeConsent(),
              identity: InMemorySessionIdentityStore(),
              reader: FakeBiometricReader(),
              onLogout: () async {},
            ),
          ),
          GoRoute(
            path: '/beneficiaries',
            builder: (context, state) =>
                const Scaffold(body: Text('Agenda (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('profile-beneficiaries')), findsOneWidget);
      await tester.tap(find.byKey(const Key('profile-beneficiaries')));
      await tester.pumpAndSettle();

      expect(find.text('Agenda (fake)'), findsOneWidget);
    });
  });
}
