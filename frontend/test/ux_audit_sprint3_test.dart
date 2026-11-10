import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/beneficiaries/data/beneficiaries_service.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/beneficiaries/presentation/beneficiary_form_page.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/credits/models/loan_application.dart';
import 'package:banca_online/features/credits/presentation/credit_status_page.dart';
import 'package:banca_online/features/home/data/profile_service.dart';
import 'package:banca_online/features/home/presentation/home_page.dart';
import 'package:banca_online/features/profile/data/biometric_consent_service.dart';
import 'package:banca_online/features/profile/presentation/profile_page.dart';
import 'package:banca_online/features/transfers/data/transfers_service.dart';
import 'package:banca_online/features/transfers/models/resolve_target.dart';
import 'package:banca_online/features/transfers/models/transfer.dart';
import 'package:banca_online/features/transfers/presentation/interbank_form_page.dart';
import 'package:banca_online/features/transfers/presentation/receipt_page.dart';
import 'package:banca_online/features/transfers/presentation/tracking_page.dart';
import 'package:banca_online/features/transfers/transfers_routes.dart';
import 'package:banca_online/features/wallet/data/wallet_service.dart';
import 'package:banca_online/features/wallet/models/wallet.dart';
import 'package:banca_online/features/welcome/onboarding_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

// Suite de la auditoria UX/flujo (sprint-3): verifica cada arreglo C/H/M con
// rutas reales (sin placeholders). Sin red ni PII: solo fakes en memoria.

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

  static const totals = AccountsTotals(
    asOf: '2026-08-24T14:32:00Z',
    primaryCurrency: 'PEN',
    primaryTotalMinor: 99999,
    totals: [
      CurrencyTotal(
        currency: 'PEN',
        availableMinor: 91999,
        heldMinor: 8000,
        totalMinor: 99999,
      ),
    ],
  );

  static const movements = [
    Movement(
      journalEntryId: 'j1',
      direction: 'CREDIT',
      description: 'Abono enero',
      valueDate: '2026-01-10',
      amountMinor: 10000,
    ),
  ];

  @override
  Future<List<Account>> getAccounts() async => accounts;

  @override
  Future<AccountsTotals> getAccountsTotals() async => totals;

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

class _OkProfile implements ProfileServiceBase {
  @override
  Future<Profile> getProfile() async =>
      const Profile(firstName: 'Ana', lastName: 'Lopez');
}

/// Billetera de comercio (E4-T23): el inicio solo muestra "Cobrar QR"
/// (`qa-qr`) con `is_merchant == true`.
class _MerchantWallet implements WalletServiceBase {
  @override
  Future<Wallet> getWallet() async => const Wallet(
        walletId: 'w-1',
        status: 'ACTIVE',
        links: [],
        isMerchant: true,
        merchantStatus: 'ACTIVE',
      );

  @override
  Future<WalletLink> linkAccount(AccountLinkInput input,
          {String? idempotencyKey}) =>
      throw UnimplementedError();

  @override
  Future<WalletLink> linkCard(CardLinkInput input, {String? idempotencyKey}) =>
      throw UnimplementedError();

  @override
  Future<WalletLink> unlink(String id) => throw UnimplementedError();
}

class _FakeConsent implements BiometricConsentServiceBase {
  @override
  Future<bool> setConsent({required bool enabled}) async => enabled;
}

ProfilePage _profilePage({Future<void> Function()? onLogout}) => ProfilePage(
      consentService: _FakeConsent(),
      identity: InMemorySessionIdentityStore(),
      reader: FakeBiometricReader(),
      onLogout: onLogout ?? () async {},
    );

void _tallViewport(WidgetTester tester) {
  tester.view.physicalSize = const Size(390, 2000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
}

class _DuplicateFake implements BeneficiariesServiceBase {
  static const existing = Beneficiary(
    id: 'b1',
    alias: 'Proveedor',
    accountMasked: '•••• 4444',
    bankCode: 'BCP',
    currency: 'PEN',
  );

  @override
  Future<List<Beneficiary>> list({String? query}) async => [existing];

  @override
  Future<Beneficiary> create(
    BeneficiaryInput input, {
    String? idempotencyKey,
  }) async =>
      throw ApiException(
        code: 'DUPLICATE_BENEFICIARY',
        message: 'Duplicado.',
        statusCode: 409,
      );

  @override
  Future<Beneficiary> update(String id, BeneficiaryPatch patch) async =>
      existing;

  @override
  Future<Beneficiary> remove(String id) async => existing;
}

class _EmptyAccountsTransfers implements TransfersServiceBase {
  @override
  Future<List<Account>> accounts() async => const [];

  @override
  Future<List<Beneficiary>> beneficiaries({String? query}) async => const [];

  @override
  Future<TransferResult> createOwnTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) =>
      throw UnimplementedError();

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
  }) =>
      throw UnimplementedError();

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
  }) =>
      throw UnimplementedError();

  @override
  Future<ResolvedTarget> resolveTarget({
    String? documentType,
    String? documentNumber,
    String? phone,
  }) =>
      throw UnimplementedError();

  @override
  Future<TransactionDetail> getTransaction(String transactionId) async =>
      throw UnimplementedError();

  @override
  Future<ReceiptResult> getReceipt(String transactionId) async =>
      throw UnimplementedError();
}

void main() {
  group('C1: wallet y notificaciones alcanzables', () {
    testWidgets('campana del home navega a /notifications', (tester) async {
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
            path: '/notifications',
            builder: (context, state) =>
                const Scaffold(body: Text('Notificaciones (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('homeBell')));
      await tester.pumpAndSettle();

      expect(find.text('Notificaciones (fake)'), findsOneWidget);
      expect(find.text('Próximamente'), findsNothing);
    });

    testWidgets('Cobrar QR va a /wallet/merchant-qr', (tester) async {
      final router = GoRouter(
        initialLocation: '/home',
        routes: [
          GoRoute(
            path: '/home',
            builder: (context, state) => HomePage(
              service: _OkAccounts(),
              profileService: _OkProfile(),
              walletService: _MerchantWallet(),
            ),
          ),
          GoRoute(
            path: '/wallet/merchant-qr',
            builder: (context, state) =>
                const Scaffold(body: Text('Cobro QR (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('qa-qr')));
      await tester.pumpAndSettle();

      expect(find.text('Cobro QR (fake)'), findsOneWidget);
    });

    testWidgets('Recargar va a /wallet/topups', (tester) async {
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
            path: '/wallet/topups',
            builder: (context, state) =>
                const Scaffold(body: Text('Recargas (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('qa-recargar')));
      await tester.pumpAndSettle();

      expect(find.text('Recargas (fake)'), findsOneWidget);
    });

    testWidgets('tarjeta Mi billetera del home va a /wallet', (tester) async {
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
            path: '/wallet',
            builder: (context, state) =>
                const Scaffold(body: Text('Billetera (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.scrollUntilVisible(
        find.byKey(const Key('home-wallet')),
        300,
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('home-wallet')));
      await tester.pumpAndSettle();

      expect(find.text('Billetera (fake)'), findsOneWidget);
    });

    testWidgets('fila Mi billetera del perfil va a /wallet', (tester) async {
      _tallViewport(tester);
      final router = GoRouter(
        initialLocation: '/profile',
        routes: [
          GoRoute(
            path: '/profile',
            builder: (context, state) => _profilePage(),
          ),
          GoRoute(
            path: '/wallet',
            builder: (context, state) =>
                const Scaffold(body: Text('Billetera (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('profile-wallet')));
      await tester.pumpAndSettle();

      expect(find.text('Billetera (fake)'), findsOneWidget);
    });

    testWidgets('bottom-nav Tarjetas del perfil va a /wallet', (tester) async {
      _tallViewport(tester);
      final router = GoRouter(
        initialLocation: '/profile',
        routes: [
          GoRoute(
            path: '/profile',
            builder: (context, state) => _profilePage(),
          ),
          GoRoute(
            path: '/wallet',
            builder: (context, state) =>
                const Scaffold(body: Text('Billetera (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('bottomNav-tarjetas')));
      await tester.pumpAndSettle();

      expect(find.text('Billetera (fake)'), findsOneWidget);
    });
  });

  group('C2: onboarding retornante', () {
    testWidgets('retornante (userRef conocido) va a /login', (tester) async {
      final identity = InMemorySessionIdentityStore(userRef: 'u-1');
      addTearDown(identity.dispose);
      final router = GoRouter(
        initialLocation: '/welcome',
        routes: [
          GoRoute(
            path: '/welcome',
            builder: (context, state) => OnboardingPage(identity: identity),
          ),
          GoRoute(
            path: '/login',
            builder: (context, state) =>
                const Scaffold(body: Text('Login (fake)')),
          ),
          GoRoute(
            path: '/login/device',
            builder: (context, state) =>
                const Scaffold(body: Text('Device (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('onboarding-cta-secondary')));
      await tester.pumpAndSettle();

      expect(router.state.matchedLocation, '/login');
      expect(find.text('Login (fake)'), findsOneWidget);
    });

    testWidgets('nuevo (sin userRef) va a /login/device', (tester) async {
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final router = GoRouter(
        initialLocation: '/welcome',
        routes: [
          GoRoute(
            path: '/welcome',
            builder: (context, state) => OnboardingPage(identity: identity),
          ),
          GoRoute(
            path: '/login',
            builder: (context, state) =>
                const Scaffold(body: Text('Login (fake)')),
          ),
          GoRoute(
            path: '/login/device',
            builder: (context, state) =>
                const Scaffold(body: Text('Device (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('onboarding-cta-secondary')));
      await tester.pumpAndSettle();

      expect(router.state.matchedLocation, '/login/device');
      expect(find.text('Device (fake)'), findsOneWidget);
    });
  });

  group('C3: Ver cronograma navega a /credits/loans', () {
    testWidgets('CTA aprobado va a la lista de prestamos', (tester) async {
      tester.view.physicalSize = const Size(390, 1400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      final router = GoRouter(
        initialLocation: '/status',
        routes: [
          GoRoute(
            path: '/status',
            builder: (context, state) => const CreditStatusPage(
              initialResult: LoanApplicationResult(
                id: 'app-1',
                status: LoanApplicationStatus.approved,
                decisionReason: 'Motivo exacto del servidor.',
                requestedAmountMinor: 1000000,
                termMonths: 12,
              ),
            ),
          ),
          GoRoute(
            path: '/credits/loans',
            builder: (context, state) =>
                const Scaffold(body: Text('Prestamos (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.scrollUntilVisible(
        find.byKey(const Key('credit-status-schedule-cta')),
        300,
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('credit-status-schedule-cta')));
      await tester.pumpAndSettle();

      expect(find.text('Prestamos (fake)'), findsOneWidget);
      expect(find.text('Próximamente'), findsNothing);
    });
  });

  group('H1: ?kind=interbank no degrada a propia', () {
    testWidgets('kind=interbank redirige a /transfers/interbank',
        (tester) async {
      final router = GoRouter(
        initialLocation: '/transfers/destination?kind=interbank',
        routes: transfersRoutes,
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(router.state.matchedLocation, '/transfers/interbank');
    });

    testWidgets('kind=own sigue en el destino propio', (tester) async {
      final router = GoRouter(
        initialLocation: '/transfers/destination?kind=own',
        routes: transfersRoutes,
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(router.state.matchedLocation, '/transfers/destination');
    });
  });

  group('H5: logout navega a /login con pila limpia', () {
    testWidgets('exito va a /login y atras no reexpone /home',
        (tester) async {
      _tallViewport(tester);
      var calls = 0;
      final router = GoRouter(
        initialLocation: '/home',
        routes: [
          GoRoute(
            path: '/home',
            builder: (context, state) =>
                const Scaffold(body: Text('Home (fake)')),
          ),
          GoRoute(
            path: '/profile',
            builder: (context, state) => _profilePage(
              onLogout: () async => calls++,
            ),
          ),
          GoRoute(
            path: '/login',
            builder: (context, state) =>
                const Scaffold(body: Text('Login (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      router.push('/profile');
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('profile-logout')));
      await tester.pumpAndSettle();

      expect(calls, 1);
      expect(router.state.matchedLocation, '/login');
      expect(find.text('Login (fake)'), findsOneWidget);
      // `go` reemplaza la pila: no hay a donde volver.
      expect(router.canPop(), isFalse);
    });

    testWidgets('error muestra tilde y Reintentar', (tester) async {
      _tallViewport(tester);
      var calls = 0;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: _profilePage(onLogout: () async {
              calls++;
              throw Exception('boom');
            }),
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('profile-logout')));
      await tester.pumpAndSettle();

      expect(calls, 1);
      expect(
        find.text('No se pudo cerrar la sesión. Inténtalo de nuevo.'),
        findsOneWidget,
      );
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(calls, 2);
    });
  });

  group('M1: error con salida (sin atrapar)', () {
    testWidgets('receipt en error ofrece Volver a /home', (tester) async {
      final router = GoRouter(
        initialLocation: '/receipt-test',
        routes: [
          GoRoute(
            path: '/receipt-test',
            builder: (context, state) => const ReceiptPage(
              transactionId: 'tx-1',
            ),
          ),
          GoRoute(
            path: '/home',
            builder: (context, state) =>
                const Scaffold(body: Text('Home (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('receipt-back-cta')), findsOneWidget);
      await tester.tap(find.byKey(const Key('receipt-back-cta')));
      await tester.pumpAndSettle();

      expect(router.state.matchedLocation, '/home');
    });

    testWidgets('tracking en error ofrece Volver a /transfers',
        (tester) async {
      final router = GoRouter(
        initialLocation: '/tracking-test',
        routes: [
          GoRoute(
            path: '/tracking-test',
            builder: (context, state) => const TrackingPage(
              args: TrackingArgs(transactionId: 'tx-1'),
            ),
          ),
          GoRoute(
            path: '/transfers',
            builder: (context, state) =>
                const Scaffold(body: Text('Transfers (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('tracking-back-cta')), findsOneWidget);
      await tester.tap(find.byKey(const Key('tracking-back-cta')));
      await tester.pumpAndSettle();

      expect(router.state.matchedLocation, '/transfers');
    });
  });

  group('M2: duplicado sin apilar rutas', () {
    testWidgets('Ver existente reemplaza y atras vuelve a la agenda',
        (tester) async {
      tester.view.physicalSize = const Size(800, 1400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      final router = GoRouter(
        initialLocation: '/beneficiaries',
        routes: [
          GoRoute(
            path: '/beneficiaries',
            builder: (context, state) => Scaffold(
              appBar: AppBar(title: const Text('Agenda')),
              body: Center(
                child: FilledButton(
                  key: const Key('agenda-new'),
                  onPressed: () => context.push('/beneficiaries/new'),
                  child: const Text('Nuevo'),
                ),
              ),
            ),
          ),
          GoRoute(
            path: '/beneficiaries/new',
            builder: (context, state) => BeneficiaryFormPage(
              service: _DuplicateFake(),
            ),
          ),
          GoRoute(
            path: '/beneficiaries/:id',
            builder: (context, state) => Scaffold(
              appBar: AppBar(title: const Text('Editar (fake)')),
              body: const Text('Detalle (fake)'),
            ),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('agenda-new')));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('beneficiary-alias')),
        'Proveedor',
      );
      await tester.enterText(
        find.byKey(const Key('beneficiary-doc-number')),
        '12345678',
      );
      await tester.enterText(
        find.byKey(const Key('beneficiary-account')),
        '00219100284729104444',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('duplicate-banner')), findsOneWidget);
      await tester.tap(find.byKey(const Key('duplicate-view-existing')));
      await tester.pumpAndSettle();

      expect(router.state.matchedLocation, '/beneficiaries/b1');
      expect(find.text('Detalle (fake)'), findsOneWidget);
      // Sin apilar `/new`: atras vuelve a la agenda, no al formulario.
      router.pop();
      await tester.pumpAndSettle();
      expect(router.state.matchedLocation, '/beneficiaries');
      expect(find.text('Agenda'), findsOneWidget);
    });
  });

  group('M4: interbank vacio vuelve a /transfers sin pila', () {
    testWidgets('Volver sin pila va a /transfers', (tester) async {
      final router = GoRouter(
        initialLocation: '/transfers/interbank',
        routes: [
          GoRoute(
            path: '/transfers/interbank',
            builder: (context, state) => InterbankFormPage(
              service: _EmptyAccountsTransfers(),
            ),
          ),
          GoRoute(
            path: '/transfers',
            builder: (context, state) =>
                const Scaffold(body: Text('Transfers (fake)')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(find.text('Aún no tienes cuentas para transferir.'),
          findsOneWidget);
      await tester.tap(find.text('Volver'));
      await tester.pumpAndSettle();

      expect(router.state.matchedLocation, '/transfers');
    });
  });
}
