import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/credits/data/credit_disbursement_service.dart';
import 'package:banca_online/features/credits/data/loan_schedule_service.dart';
import 'package:banca_online/features/credits/data/my_credits_service.dart';
import 'package:banca_online/features/credits/models/loan_application.dart';
import 'package:banca_online/features/credits/models/loan_schedule.dart';
import 'package:banca_online/features/credits/models/own_application.dart';
import 'package:banca_online/features/credits/presentation/credit_status_page.dart';
import 'package:banca_online/features/credits/presentation/my_credits_section.dart';
import 'package:banca_online/features/home/data/profile_service.dart';
import 'package:banca_online/features/home/presentation/home_page.dart';
import 'package:banca_online/features/profile/presentation/profile_page.dart';
import 'package:banca_online/features/wallet/data/wallet_service.dart';
import 'package:banca_online/features/wallet/models/wallet.dart';
import 'package:banca_online/features/wallet/presentation/wallet_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

import 'credits_application_test.dart' show FakeCreditApplicationsService;

/// Descubribilidad sprint-3 (sin PII en logs): el ciclo firmar → desembolsar
/// es reanudable y el cronograma/servicios tienen entradas persistentes.
class _FakeDisburse implements CreditDisbursementServiceBase {
  @override
  Future<DisbursementResult> disburseApplication({
    required String applicationId,
    String? accountId,
    String? idempotencyKey,
  }) async =>
      const DisbursementResult(
        loanId: 'loan-9',
        status: 'SETTLED',
        applicationId: 'app-firmado',
      );
}

class _FakeLoansEmpty implements LoanScheduleServiceBase {
  @override
  Future<List<Loan>> listLoans() async => const [];

  @override
  Future<LoanSchedule> getSchedule(String loanId) =>
      throw UnimplementedError();

  @override
  Future<bool> setAutoDebit(String loanId, {required bool enabled}) =>
      throw UnimplementedError();

  @override
  Future<PayInstallmentResult> payNextInstallment(
    String loanId, {
    String? idempotencyKey,
  }) =>
      throw UnimplementedError();
}

class _FakeAccounts implements AccountsServiceBase {
  @override
  Future<List<Account>> getAccounts() async => const [];

  @override
  Future<AccountsTotals> getAccountsTotals() => throw UnimplementedError();

  @override
  Future<Account> getAccountDetail(String accountId) =>
      throw UnimplementedError();

  @override
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  }) =>
      throw UnimplementedError();

  @override
  Future<ExportResult> exportMovements(
    String accountId, {
    required String format,
    required DateTime dateFrom,
    required DateTime dateTo,
  }) =>
      throw UnimplementedError();
}

class _FakeMineSigned implements MyCreditsServiceBase {
  @override
  Future<OwnApplicationsPage> listMine({int? limit, int? offset}) async =>
      const OwnApplicationsPage(
        items: [
          OwnCreditApplication(
            id: 'app-signed-1',
            productId: 'p1',
            productName: 'Personal',
            requestedAmountMinor: 1000000,
            termMonths: 12,
            status: LoanApplicationStatus.approved,
            contractSigned: true,
            createdAt: '2026-10-10T10:00:00Z',
          ),
        ],
        total: 1,
      );
}

class _OkAccounts implements AccountsServiceBase {
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
  ];

  @override
  Future<List<Account>> getAccounts() async => accounts;

  @override
  Future<AccountsTotals> getAccountsTotals() async => const AccountsTotals(
        asOf: '2026-08-24T14:32:00Z',
        primaryCurrency: 'PEN',
        primaryTotalMinor: 99999,
        totals: [],
      );

  @override
  Future<Account> getAccountDetail(String accountId) async => accounts.first;

  @override
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  }) async =>
      const MovementsPage(items: [], page: 1, pageSize: 20, total: 0);

  @override
  Future<ExportResult> exportMovements(
    String accountId, {
    required String format,
    required DateTime dateFrom,
    required DateTime dateTo,
  }) =>
      throw UnimplementedError();
}

class _OkProfile implements ProfileServiceBase {
  @override
  Future<Profile> getProfile() async =>
      const Profile(firstName: 'Ana', lastName: 'Lopez');
}

class _OkWallet implements WalletServiceBase {
  @override
  Future<Wallet> getWallet() async => const Wallet(
        walletId: 'w-1',
        status: 'ACTIVE',
        links: [
          WalletLink(
            id: 'l-1',
            type: 'ACCOUNT',
            accountMasked: '****1234',
            status: 'ACTIVE',
          ),
        ],
      );

  @override
  Future<WalletLink> linkAccount(
    AccountLinkInput input, {
    String? idempotencyKey,
  }) =>
      throw UnimplementedError();

  @override
  Future<WalletLink> linkCard(
    CardLinkInput input, {
    String? idempotencyKey,
  }) =>
      throw UnimplementedError();

  @override
  Future<WalletLink> unlink(String id) => throw UnimplementedError();
}

Future<void> _reveal(WidgetTester tester, Finder finder) async {
  await tester.scrollUntilVisible(
    finder,
    400,
    scrollable: find.byType(Scrollable).first,
  );
  await tester.pumpAndSettle();
}

void main() {
  group('Descubribilidad sprint-3', () {
    testWidgets('estado firmado reanuda el desembolso + Mis préstamos',
        (tester) async {
      final router = GoRouter(
        initialLocation: '/credits/status',
        routes: [
          GoRoute(
            path: '/credits/status',
            builder: (context, state) => CreditStatusPage(
              service: FakeCreditApplicationsService(),
              initialResult: const LoanApplicationResult(
                id: 'app-firmado',
                status: LoanApplicationStatus.approved,
                decisionReason: 'Aprobado.',
                requestedAmountMinor: 1000000,
                termMonths: 12,
                contractSigned: true,
              ),
              disburseService: _FakeDisburse(),
              loansService: _FakeLoansEmpty(),
              accountsService: _FakeAccounts(),
            ),
          ),
          GoRoute(
            path: '/credits/loans',
            builder: (context, state) =>
                const Scaffold(body: Text('Mis préstamos (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      // La CTA de desembolso sigue presente tras volver (no solo en el
      // éxito de la firma).
      await _reveal(tester, find.byKey(const Key('disburse-cta')));
      expect(find.text('Desembolsar a mi cuenta'), findsOneWidget);

      await _reveal(
        tester,
        find.byKey(const Key('credit-status-loans-cta')),
      );
      expect(find.text('Ver mis préstamos'), findsOneWidget);
      await tester.tap(find.byKey(const Key('credit-status-loans-cta')));
      await tester.pumpAndSettle();
      expect(find.text('Mis préstamos (fake)'), findsOneWidget);
    });

    testWidgets('fila firmada ofrece desembolso + cronograma', (tester) async {
      final router = GoRouter(
        initialLocation: '/mine',
        routes: [
          GoRoute(
            path: '/mine',
            builder: (context, state) => Scaffold(
              body: MyCreditsSection(service: _FakeMineSigned()),
            ),
          ),
          GoRoute(
            path: '/credits/sign',
            builder: (context, state) =>
                const Scaffold(body: Text('Firma (fake)')),
          ),
          GoRoute(
            path: '/credits/loans',
            builder: (context, state) =>
                const Scaffold(body: Text('Mis préstamos (fake)')),
          ),
          GoRoute(
            path: '/credits/status',
            builder: (context, state) =>
                const Scaffold(body: Text('Estado (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(
        find.byKey(const Key('my-credits-disburse-app-signed-1')),
        findsOneWidget,
      );
      expect(find.text('Desembolsar a mi cuenta'), findsOneWidget);
      await tester.tap(
        find.byKey(const Key('my-credits-disburse-app-signed-1')),
      );
      await tester.pumpAndSettle();
      expect(find.text('Firma (fake)'), findsOneWidget);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();
    });

    testWidgets('fila firmada: Ver cronograma navega a /credits/loans',
        (tester) async {
      final router = GoRouter(
        initialLocation: '/mine',
        routes: [
          GoRoute(
            path: '/mine',
            builder: (context, state) => Scaffold(
              body: MyCreditsSection(service: _FakeMineSigned()),
            ),
          ),
          GoRoute(
            path: '/credits/sign',
            builder: (context, state) =>
                const Scaffold(body: Text('Firma (fake)')),
          ),
          GoRoute(
            path: '/credits/loans',
            builder: (context, state) =>
                const Scaffold(body: Text('Mis préstamos (fake)')),
          ),
          GoRoute(
            path: '/credits/status',
            builder: (context, state) =>
                const Scaffold(body: Text('Estado (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(
        find.byKey(const Key('my-credits-schedule-app-signed-1')),
        findsOneWidget,
      );
      expect(find.text('Ver cronograma ›'), findsOneWidget);
      await tester.tap(
        find.byKey(const Key('my-credits-schedule-app-signed-1')),
      );
      await tester.pumpAndSettle();
      expect(find.text('Mis préstamos (fake)'), findsOneWidget);
    });

    testWidgets('home: Mis préstamos y Pagar servicios navegan',
        (tester) async {
      final router = GoRouter(
        initialLocation: '/home',
        routes: [
          GoRoute(
            path: '/home',
            builder: (context, state) => HomePage(
              service: _OkAccounts(),
              profileService: _OkProfile(),
            ),
          ),
          GoRoute(
            path: '/credits/loans',
            builder: (context, state) =>
                const Scaffold(body: Text('Mis préstamos (fake)')),
          ),
          GoRoute(
            path: '/wallet/services',
            builder: (context, state) =>
                const Scaffold(body: Text('Servicios (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.scrollUntilVisible(
        find.byKey(const Key('home-loans')),
        300,
      );
      await tester.pumpAndSettle();
      expect(find.text('Mis préstamos'), findsOneWidget);
      await tester.tap(find.byKey(const Key('home-loans')));
      await tester.pumpAndSettle();
      expect(find.text('Mis préstamos (fake)'), findsOneWidget);
    });

    testWidgets('home: Pagar servicios navega a /wallet/services',
        (tester) async {
      final router = GoRouter(
        initialLocation: '/home',
        routes: [
          GoRoute(
            path: '/home',
            builder: (context, state) => HomePage(
              service: _OkAccounts(),
              profileService: _OkProfile(),
            ),
          ),
          GoRoute(
            path: '/wallet/services',
            builder: (context, state) =>
                const Scaffold(body: Text('Servicios (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.scrollUntilVisible(
        find.byKey(const Key('home-services')),
        300,
      );
      await tester.pumpAndSettle();
      expect(find.text('Pagar servicios'), findsOneWidget);
      await tester.tap(find.byKey(const Key('home-services')));
      await tester.pumpAndSettle();
      expect(find.text('Servicios (fake)'), findsOneWidget);
    });

    testWidgets('perfil: Mis préstamos navega a /credits/loans',
        (tester) async {
      tester.view.physicalSize = const Size(390, 2000);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      final router = GoRouter(
        initialLocation: '/profile',
        routes: [
          GoRoute(
            path: '/profile',
            builder: (context, state) => const ProfilePage(),
          ),
          GoRoute(
            path: '/credits/loans',
            builder: (context, state) =>
                const Scaffold(body: Text('Mis préstamos (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(find.text('Mis préstamos'), findsOneWidget);
      await tester.tap(find.byKey(const Key('profile-loans')));
      await tester.pumpAndSettle();
      expect(find.text('Mis préstamos (fake)'), findsOneWidget);
    });

    testWidgets('billetera: Pagar servicios y Recargar navegan',
        (tester) async {
      final router = GoRouter(
        initialLocation: '/wallet',
        routes: [
          GoRoute(
            path: '/wallet',
            builder: (context, state) => WalletPage(service: _OkWallet()),
          ),
          GoRoute(
            path: '/wallet/services',
            builder: (context, state) =>
                const Scaffold(body: Text('Servicios (fake)')),
          ),
          GoRoute(
            path: '/wallet/topups',
            builder: (context, state) =>
                const Scaffold(body: Text('Recargas (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(find.text('Pagar servicios'), findsOneWidget);
      expect(find.text('Recargar'), findsOneWidget);
      await tester.tap(find.byKey(const Key('wallet-services-action')));
      await tester.pumpAndSettle();
      expect(find.text('Servicios (fake)'), findsOneWidget);
    });

    testWidgets('billetera: Recargar navega a /wallet/topups', (tester) async {
      final router = GoRouter(
        initialLocation: '/wallet',
        routes: [
          GoRoute(
            path: '/wallet',
            builder: (context, state) => WalletPage(service: _OkWallet()),
          ),
          GoRoute(
            path: '/wallet/topups',
            builder: (context, state) =>
                const Scaffold(body: Text('Recargas (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('wallet-topups-action')));
      await tester.pumpAndSettle();
      expect(find.text('Recargas (fake)'), findsOneWidget);
    });
  });
}
