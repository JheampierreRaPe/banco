// Regresion 8-10-2026-2.txt (dueno, rama sprint-2).
//
// D1: tope de beneficiario eliminado (solo umbral biometrico S/ 1,000).
// D2: perfil muestra numero enmascarado (4 finales) + copiar.
// D3: preview home oculta mecanica `hold`/`settle` (UUIDs internos).
//
// Cada grupo FALLA sin el fix y PASA con el.
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/beneficiaries/data/beneficiaries_service.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/beneficiaries/presentation/beneficiary_form_page.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/home/data/profile_service.dart';
import 'package:banca_online/features/home/presentation/home_page.dart';
import 'package:banca_online/features/profile/data/biometric_consent_service.dart';
import 'package:banca_online/features/profile/presentation/profile_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

// ------------------------------- Fakes comunes ------------------------------

class _FakeBeneficiaries implements BeneficiariesServiceBase {
  BeneficiaryInput? lastCreated;

  @override
  Future<List<Beneficiary>> list({String? query}) async => const [];

  @override
  Future<Beneficiary> create(
    BeneficiaryInput input, {
    String? idempotencyKey,
  }) async {
    lastCreated = input;
    return Beneficiary(
      id: 'b-new',
      alias: input.alias,
      accountMasked: '•••• 0012',
      bankCode: input.bankCode ?? '',
      holderName: '••••',
      currency: input.currency,
    );
  }

  @override
  Future<Beneficiary> update(String id, BeneficiaryInput input) async =>
      create(input);

  @override
  Future<Beneficiary> remove(String id) async => throw UnimplementedError();
}

class _FakeConsent implements BiometricConsentServiceBase {
  @override
  Future<bool> setConsent({required bool enabled}) async => enabled;
}

class _FakeAccountsForProfile implements AccountsServiceBase {
  @override
  Future<List<Account>> getAccounts() async => const [
        Account(
          id: 'a1',
          accountNumberMasked: '****0013',
          type: 'AHORRO',
          currency: 'PEN',
          availableMinor: 10000,
          heldMinor: 0,
          balanceMinor: 10000,
        ),
      ];

  @override
  Future<AccountsTotals> getAccountsTotals() async => const AccountsTotals(
        asOf: '2026-10-08T12:00:00Z',
        primaryCurrency: 'PEN',
        primaryTotalMinor: 10000,
        totals: [],
      );

  @override
  Future<Account> getAccountDetail(String accountId) async =>
      (await getAccounts()).first;

  @override
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  }) async =>
      MovementsPage(items: const [], page: page, pageSize: pageSize, total: 0);

  @override
  Future<ExportResult> exportMovements(
    String accountId, {
    required String format,
    required DateTime dateFrom,
    required DateTime dateTo,
  }) =>
      throw UnimplementedError();
}

class _FakeAccountsForHome implements AccountsServiceBase {
  @override
  Future<List<Account>> getAccounts() async => const [
        Account(
          id: 'a1',
          accountNumberMasked: '****0013',
          type: 'AHORRO',
          currency: 'PEN',
          availableMinor: 10000,
          heldMinor: 0,
          balanceMinor: 10000,
        ),
      ];

  @override
  Future<AccountsTotals> getAccountsTotals() async => const AccountsTotals(
        asOf: '2026-10-08T12:00:00Z',
        primaryCurrency: 'PEN',
        primaryTotalMinor: 10000,
        totals: [],
      );

  @override
  Future<Account> getAccountDetail(String accountId) async =>
      (await getAccounts()).first;

  @override
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  }) async =>
      // Mezcla como la del reporte: 2 filas tecnicas + 1 de usuario.
      const MovementsPage(
        items: [
          Movement(
            journalEntryId: 'j-hold',
            direction: 'DEBIT',
            description:
                'hold a06e5007-c25d-4e86-976d-fc44ab921ae5',
            valueDate: '2026-10-09',
            amountMinor: 110000,
          ),
          Movement(
            journalEntryId: 'j-settle',
            direction: 'DEBIT',
            description:
                'settle a06e5007-c25d-4e86-976d-fc44ab921ae5',
            valueDate: '2026-10-09',
            amountMinor: 110000,
          ),
          Movement(
            journalEntryId: 'j-user',
            direction: 'CREDIT',
            description: 'Abono sueldo',
            valueDate: '2026-10-08',
            amountMinor: 50000,
          ),
        ],
        page: 1,
        pageSize: 20,
        total: 3,
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

class _FakeProfile implements ProfileServiceBase {
  @override
  Future<Profile> getProfile() async =>
      const Profile(firstName: 'Juan', lastName: 'Perez');
}

void main() {
  group('8-10-2026-2 D1: sin tope de beneficiario', () {
    testWidgets('el formulario no ofrece tope y guarda null', (
      tester,
    ) async {
      final fake = _FakeBeneficiaries();
      final router = GoRouter(
        initialLocation: '/beneficiaries/new',
        routes: [
          GoRoute(
            path: '/beneficiaries/new',
            builder: (context, state) => BeneficiaryFormPage(service: fake),
          ),
          GoRoute(
            path: '/beneficiaries',
            builder: (context, state) =>
                const Scaffold(body: Text('Agenda')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      // Sin el fix existian estos switches/chips de tope.
      expect(find.byKey(const Key('tope-switch')), findsNothing);
      expect(find.byKey(const Key('tope-1000')), findsNothing);
      expect(find.byKey(const Key('tope-2000')), findsNothing);
      expect(find.byKey(const Key('tope-custom')), findsNothing);
      // Nota informativa de la unica regla vigente.
      expect(
        find.textContaining('requieren validación biométrica'),
        findsOneWidget,
      );

      await tester.enterText(
        find.byKey(const Key('beneficiary-alias')),
        'Mamá Lima',
      );
      await tester.enterText(
        find.byKey(const Key('beneficiary-doc-number')),
        '42891044',
      );
      await tester.enterText(
        find.byKey(const Key('beneficiary-account')),
        '00112345678908',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pumpAndSettle();

      expect(fake.lastCreated, isNotNull);
      // E2-T33: sin tope en el contrato; el request no lleva el campo.
      expect(
        fake.lastCreated!.toJson().containsKey('preapproved_limit_minor'),
        isFalse,
      );
    });
  });

  group('8-10-2026-2 D2: perfil con cuenta enmascarada copiable', () {
    testWidgets('muestra ****0013 y el boton copiar avisa', (tester) async {
      tester.view.physicalSize = const Size(390, 2000);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ProfilePage(
              consentService: _FakeConsent(),
              identity: InMemorySessionIdentityStore(),
              reader: FakeBiometricReader(),
              onLogout: () async {},
              accountsService: _FakeAccountsForProfile(),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Solo ultimos 4 visibles, tal como viene del backend.
      expect(find.byKey(const Key('profile-account-masked')), findsOneWidget);
      expect(find.text('****0013'), findsOneWidget);
      expect(find.text('MI CUENTA'), findsOneWidget);

      await tester.scrollUntilVisible(
        find.byKey(const Key('profile-account-copy')),
        300,
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('profile-account-copy')));
      // Sin asentar (el snackbar dura 2s y `pumpAndSettle` lo descartaria).
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));

      // Sin el fix no existia el boton ni el snackbar.
      expect(find.text('Número de cuenta copiado.'), findsOneWidget);
    });

    testWidgets('sin servicio no muestra tarjeta (compat F-T52)', (
      tester,
    ) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ProfilePage(
              consentService: _FakeConsent(),
              identity: InMemorySessionIdentityStore(),
              reader: FakeBiometricReader(),
              onLogout: () async {},
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('profile-account-masked')), findsNothing);
      expect(find.text('MI CUENTA'), findsNothing);
    });
  });

  group('8-10-2026-2 D3: preview sin hold/settle', () {
    testWidgets('oculta UUIDs internos y conserva el movimiento real', (
      tester,
    ) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: HomePage(
              service: _FakeAccountsForHome(),
              profileService: _FakeProfile(),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.text('ÚLTIMOS MOVIMIENTOS'),
        300,
      );
      await tester.pumpAndSettle();

      // Sin el fix estas descripciones tecnicas se mostraban tal cual.
      expect(find.textContaining('a06e5007'), findsNothing);
      expect(find.textContaining('hold '), findsNothing);
      expect(find.textContaining('settle '), findsNothing);
      // El movimiento de usuario sigue visible.
      expect(find.text('Abono sueldo'), findsOneWidget);
    });
  });
}
