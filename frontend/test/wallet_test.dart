import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/wallet/data/wallet_service.dart';
import 'package:banca_online/features/wallet/models/wallet.dart';
import 'package:banca_online/features/wallet/presentation/wallet_link_page.dart';
import 'package:banca_online/features/wallet/presentation/wallet_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del servicio (seam `WalletServiceBase`): sin red, con datos que
/// replican los shapes E4-T01 (`GET /wallet` -> `data` + `meta`;
/// vinculos solo enmascarados, jamas PAN ni `token_ref`).
class FakeWalletService implements WalletServiceBase {
  FakeWalletService({Wallet? seed, this.hangWallet = false})
      : wallet = seed ?? _defaultSeed;

  static const _defaultSeed = Wallet(
    walletId: 'w-1',
    status: 'ACTIVE',
    links: [
      WalletLink(
        id: 'l-1',
        type: 'ACCOUNT',
        accountMasked: '****1234',
        status: 'ACTIVE',
      ),
      WalletLink(
        id: 'l-2',
        type: 'CARD',
        last4: '5678',
        brand: 'VISA',
        status: 'ACTIVE',
      ),
    ],
  );

  Wallet wallet;
  final bool hangWallet;

  int walletCalls = 0;

  /// Cuerpos enviados a `POST /wallet/links` (el PAN crudo solo existe
  /// aqui como lo tecleado en el request, jamas en respuestas).
  final List<Map<String, dynamic>> linkBodies = [];

  /// `Idempotency-Key` recibidas por intento (para probar el reuso en el
  /// Reintentar).
  final List<String?> linkKeys = [];

  /// Si no es `null`, `getWallet` lanza este error (estado error).
  ApiException? walletError;

  /// Si no es `null`, `link*` lanzan este error.
  ApiException? linkError;

  /// Ids recibidos en `DELETE /wallet/links/{id}` (baja logica).
  final List<String> unlinkIds = [];

  /// Si no es `null`, `unlink` lanza este error (`404` neutro, red, ...).
  ApiException? unlinkError;

  /// Si es `true`, el proximo `link*` falla y los siguientes tienen exito
  /// (para probar que el Reintentar reusa la misma clave).
  bool failNextLink = false;

  @override
  Future<Wallet> getWallet() async {
    walletCalls++;
    if (walletError != null) throw walletError!;
    if (hangWallet) {
      await Completer<void>().future;
    }
    return wallet;
  }

  WalletLink _created(Map<String, dynamic> body) {
    final type = (body['type'] as String?) ?? '';
    if (type == 'ACCOUNT') {
      return const WalletLink(
        id: 'l-new',
        type: 'ACCOUNT',
        accountMasked: '****4321',
        status: 'ACTIVE',
      );
    }
    // Tarjeta: el "servidor" tokeniza y devuelve solo `last4`/`brand`
    // (el PAN nunca sale en respuestas).
    final card = body['card'];
    final pan = card is Map ? (card['pan']?.toString() ?? '') : '';
    final digits = pan.replaceAll(RegExp(r'\D'), '');
    final tail = digits.length >= 4
        ? digits.substring(digits.length - 4)
        : '0000';
    return WalletLink(
      id: 'l-new-card',
      type: 'CARD',
      last4: tail,
      brand: 'VISA',
      status: 'ACTIVE',
    );
  }

  Future<WalletLink> _link(Map<String, dynamic> body, String? key) async {
    linkBodies.add(body);
    linkKeys.add(key);
    if (linkError != null) throw linkError!;
    if (failNextLink) {
      failNextLink = false;
      throw ApiException.network();
    }
    final created = _created(body);
    wallet = Wallet(
      walletId: wallet.walletId ?? 'w-1',
      status: 'ACTIVE',
      links: [...wallet.links, created],
    );
    return created;
  }

  @override
  Future<WalletLink> linkAccount(
    AccountLinkInput input, {
    String? idempotencyKey,
  }) =>
      _link(input.toJson(), idempotencyKey);

  @override
  Future<WalletLink> linkCard(
    CardLinkInput input, {
    String? idempotencyKey,
  }) =>
      _link(input.toJson(), idempotencyKey);

  @override
  Future<WalletLink> unlink(String id) async {
    unlinkIds.add(id);
    if (unlinkError != null) throw unlinkError!;
    final found = wallet.links.where((l) => l.id == id).toList();
    if (found.isEmpty) {
      throw ApiException(code: 'NOT_FOUND', message: 'No encontramos.');
    }
    final target = found.single;
    final updated = WalletLink(
      id: target.id,
      type: target.type,
      accountMasked: target.accountMasked,
      last4: target.last4,
      brand: target.brand,
      status: 'INACTIVE',
      linkedAt: target.linkedAt,
    );
    wallet = Wallet(
      walletId: wallet.walletId,
      status: wallet.status,
      links: [for (final l in wallet.links) l.id == id ? updated : l],
    );
    return updated;
  }
}

/// Fake de cuentas del titular (`GET /accounts`).
class FakeAccountsService implements AccountsServiceBase {
  FakeAccountsService({List<Account>? seed})
      : accounts = seed ?? _defaultSeed;

  static const _defaultSeed = [
    Account(
      id: 'acc-1',
      accountNumberMasked: '****1234',
      type: 'SAVINGS',
      currency: 'PEN',
      availableMinor: 10000,
      heldMinor: 0,
      balanceMinor: 10000,
      status: 'ACTIVE',
    ),
    Account(
      id: 'acc-2',
      accountNumberMasked: '****5678',
      type: 'SAVINGS',
      currency: 'USD',
      availableMinor: 5000,
      heldMinor: 0,
      balanceMinor: 5000,
      status: 'ACTIVE',
    ),
  ];

  final List<Account> accounts;
  ApiException? accountsError;

  @override
  Future<List<Account>> getAccounts() async {
    if (accountsError != null) throw accountsError!;
    return List<Account>.from(accounts);
  }

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

/// Harness con `GoRouter` real (las paginas usan `context.push/pop`).
Widget _harness(
  FakeWalletService wallet,
  FakeAccountsService accounts, {
  String initial = '/wallet',
}) {
  final router = GoRouter(
    initialLocation: initial,
    routes: [
      GoRoute(
        path: '/wallet',
        builder: (context, state) => WalletPage(service: wallet),
      ),
      GoRoute(
        path: '/wallet/link',
        builder: (context, state) => WalletLinkPage(
          service: wallet,
          accountsService: accounts,
        ),
      ),
    ],
  );
  return MaterialApp.router(routerConfig: router);
}

Future<void> _pumpWallet(
  WidgetTester tester,
  FakeWalletService wallet,
  FakeAccountsService accounts,
) async {
  await tester.pumpWidget(_harness(wallet, accounts));
  await tester.pumpAndSettle();
}

void main() {
  group('WalletPage', () {
    testWidgets('lista muestra vinculos enmascarados con chip Activa', (
      tester,
    ) async {
      await _pumpWallet(
        tester,
        FakeWalletService(),
        FakeAccountsService(),
      );

      expect(find.text('Billetera'), findsOneWidget);
      // Cuenta enmascarada tal cual del backend (sin recalculo).
      expect(find.text('Cuenta ****1234'), findsOneWidget);
      // Tarjeta: marca + solo ultimos 4.
      expect(find.text('Tarjeta VISA •••• 5678'), findsOneWidget);
      expect(find.text('Activa'), findsWidgets);
      // El numero completo jamas se muestra.
      expect(find.textContaining('12345678'), findsNothing);
    });

    testWidgets('muestra skeleton durante la carga', (tester) async {
      final wallet = FakeWalletService(hangWallet: true);
      await tester.pumpWidget(_harness(wallet, FakeAccountsService()));
      await tester.pump();

      expect(find.byKey(const Key('wallet-skeleton')), findsOneWidget);
    });

    testWidgets('vacio ofrece vincular y navega al formulario', (
      tester,
    ) async {
      await _pumpWallet(
        tester,
        FakeWalletService(seed: Wallet.empty),
        FakeAccountsService(),
      );

      expect(
        find.textContaining('Aún no tienes métodos vinculados'),
        findsOneWidget,
      );
      await tester.tap(find.text('Vincular fuente'));
      await tester.pumpAndSettle();

      expect(find.text('Vincular fuente'), findsOneWidget);
      expect(find.text('Cuenta del banco'), findsOneWidget);
      expect(find.text('Tarjeta'), findsOneWidget);
    });

    testWidgets('error muestra Reintentar y reintenta', (tester) async {
      final wallet = FakeWalletService()
        ..walletError = ApiException.network();
      await tester.pumpWidget(_harness(wallet, FakeAccountsService()));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('Ocurrió un error al consultar el servidor'),
        findsOneWidget,
      );
      expect(find.text('Reintentar'), findsOneWidget);

      wallet.walletError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();

      expect(find.text('Cuenta ****1234'), findsOneWidget);
    });

    testWidgets('accion Vincular abre el formulario', (tester) async {
      await _pumpWallet(
        tester,
        FakeWalletService(),
        FakeAccountsService(),
      );

      await tester.tap(find.byKey(const Key('wallet-link-action')));
      await tester.pumpAndSettle();

      expect(find.text('Vincular fuente'), findsWidgets);
    });

    testWidgets('desvincular feliz llama al endpoint y refresca', (
      tester,
    ) async {
      final wallet = FakeWalletService();
      await _pumpWallet(tester, wallet, FakeAccountsService());
      final callsBefore = wallet.walletCalls;

      await tester.tap(find.byKey(const Key('wallet-unlink-l-1')));
      await tester.pumpAndSettle();
      expect(find.text('¿Desvincular fuente?'), findsOneWidget);

      await tester.tap(find.byKey(const Key('unlink-confirm')));
      await tester.pumpAndSettle();

      expect(wallet.unlinkIds, ['l-1']);
      expect(find.text('Fuente desvinculada.'), findsOneWidget);
      // Refresca la lista tras el exito.
      expect(wallet.walletCalls, greaterThan(callsBefore));
      // El vinculo deja de estar activo (pasa a INACTIVE).
      expect(find.byKey(const Key('wallet-unlink-l-1')), findsNothing);
      expect(find.text('Inactiva'), findsOneWidget);
    });

    testWidgets('confirmacion cancelada no llama al endpoint', (
      tester,
    ) async {
      final wallet = FakeWalletService();
      await _pumpWallet(tester, wallet, FakeAccountsService());

      await tester.tap(find.byKey(const Key('wallet-unlink-l-1')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('unlink-cancel')));
      await tester.pumpAndSettle();

      expect(wallet.unlinkIds, isEmpty);
      expect(find.byKey(const Key('wallet-unlink-l-1')), findsOneWidget);
    });

    testWidgets('404 neutro recarga y avisa sin oraculo', (tester) async {
      final wallet = FakeWalletService()
        ..unlinkError = ApiException(
          code: 'NOT_FOUND',
          message: 'No encontramos lo que buscas.',
          statusCode: 404,
        );
      final callsBefore = wallet.walletCalls;
      await tester.pumpWidget(_harness(wallet, FakeAccountsService()));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('wallet-unlink-l-1')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('unlink-confirm')));
      await tester.pumpAndSettle();

      expect(wallet.unlinkIds, ['l-1']);
      expect(
        find.text('Ese vínculo ya no está activo. Actualizamos tu lista.'),
        findsOneWidget,
      );
      expect(wallet.walletCalls, greaterThan(callsBefore));
    });

    testWidgets('error de red muestra Reintentar y reintenta', (
      tester,
    ) async {
      final wallet = FakeWalletService()..unlinkError = ApiException.network();
      await _pumpWallet(tester, wallet, FakeAccountsService());

      await tester.tap(find.byKey(const Key('wallet-unlink-l-1')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('unlink-confirm')));
      await tester.pumpAndSettle();

      expect(find.text('Reintentar'), findsOneWidget);

      wallet.unlinkError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();

      expect(wallet.unlinkIds, ['l-1', 'l-1']);
      expect(find.text('Fuente desvinculada.'), findsOneWidget);
      expect(find.byKey(const Key('wallet-unlink-l-1')), findsNothing);
    });

    testWidgets('vinculo inactivo no ofrece desvincular', (tester) async {
      const seed = Wallet(
        walletId: 'w-1',
        status: 'ACTIVE',
        links: [
          WalletLink(id: 'l-9', type: 'CARD', last4: '9999', status: 'INACTIVE'),
        ],
      );
      final wallet = FakeWalletService(seed: seed);
      await _pumpWallet(tester, wallet, FakeAccountsService());

      expect(find.text('Inactiva'), findsOneWidget);
      expect(find.byKey(const Key('wallet-unlink-l-9')), findsNothing);
    });

    testWidgets('cuenta personal no ve la entrada de comercio (E4-T23)',
        (tester) async {
      // Seed por defecto: `isMerchant: false` (fail-closed).
      await _pumpWallet(
        tester,
        FakeWalletService(),
        FakeAccountsService(),
      );

      expect(find.byKey(const Key('wallet-merchant-qr-action')), findsNothing);
      expect(find.byKey(const Key('wallet-merchant-qr-empty')), findsNothing);
      // "Mi QR personal" (E4-T21) y las demas acciones siguen visibles.
      expect(find.byKey(const Key('wallet-personal-qr-action')), findsOneWidget);
      expect(find.byKey(const Key('wallet-pay-qr-action')), findsOneWidget);
      expect(find.byKey(const Key('wallet-services-action')), findsOneWidget);
      expect(find.byKey(const Key('wallet-topups-action')), findsOneWidget);
    });

    testWidgets('cuenta de comercio si ve la entrada de comercio (E4-T23)',
        (tester) async {
      const seed = Wallet(
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
        isMerchant: true,
        merchantStatus: 'ACTIVE',
      );
      await _pumpWallet(
        tester,
        FakeWalletService(seed: seed),
        FakeAccountsService(),
      );

      expect(find.byKey(const Key('wallet-merchant-qr-action')), findsOneWidget);
      expect(find.text('Cobrar como comercio'), findsOneWidget);
      expect(find.byKey(const Key('wallet-personal-qr-action')), findsOneWidget);
    });

    testWidgets('vacio personal no muestra entrada de comercio (E4-T23)',
        (tester) async {
      await _pumpWallet(
        tester,
        FakeWalletService(seed: Wallet.empty),
        FakeAccountsService(),
      );

      expect(find.byKey(const Key('wallet-merchant-qr-empty')), findsNothing);
      expect(find.byKey(const Key('wallet-personal-qr-empty')), findsOneWidget);
      expect(find.byKey(const Key('wallet-pay-qr-empty')), findsOneWidget);
    });

    testWidgets('vacio de comercio si muestra la entrada (E4-T23)',
        (tester) async {
      const seed = Wallet(
        walletId: null,
        status: 'NONE',
        links: [],
        isMerchant: true,
        merchantStatus: 'ACTIVE',
      );
      await _pumpWallet(
        tester,
        FakeWalletService(seed: seed),
        FakeAccountsService(),
      );

      expect(find.byKey(const Key('wallet-merchant-qr-empty')), findsOneWidget);
      expect(find.byKey(const Key('wallet-personal-qr-empty')), findsOneWidget);
    });
  });

  group('Wallet model (E4-T23)', () {
    test('fromJson parsea is_merchant true y merchant_status', () {
      final wallet = Wallet.fromJson({
        'wallet_id': 'w-1',
        'status': 'ACTIVE',
        'links': [],
        'is_merchant': true,
        'merchant_status': 'ACTIVE',
      });

      expect(wallet.isMerchant, isTrue);
      expect(wallet.merchantStatus, 'ACTIVE');
    });

    test('fromJson con is_merchant false es cuenta personal', () {
      final wallet = Wallet.fromJson({
        'wallet_id': 'w-1',
        'status': 'ACTIVE',
        'links': [],
        'is_merchant': false,
        'merchant_status': null,
      });

      expect(wallet.isMerchant, isFalse);
      expect(wallet.merchantStatus, isNull);
    });

    test('fromJson sin flag degrada a personal (fail-closed)', () {
      final wallet = Wallet.fromJson({
        'wallet_id': 'w-1',
        'status': 'ACTIVE',
        'links': [],
      });

      expect(wallet.isMerchant, isFalse);
      expect(wallet.merchantStatus, isNull);
    });

    test('fromJson con flag no booleano degrada a personal', () {
      final wallet = Wallet.fromJson({
        'wallet_id': 'w-1',
        'status': 'ACTIVE',
        'links': [],
        'is_merchant': 'yes',
      });

      expect(wallet.isMerchant, isFalse);
    });
  });

  group('WalletLinkPage', () {
    Future<void> pumpLink(
      WidgetTester tester,
      FakeWalletService wallet,
      FakeAccountsService accounts,
    ) async {
      await tester.pumpWidget(
        _harness(wallet, accounts, initial: '/wallet/link'),
      );
      await tester.pumpAndSettle();
    }

    testWidgets('vincular cuenta envia account_id y muestra enmascarado', (
      tester,
    ) async {
      final wallet = FakeWalletService(seed: Wallet.empty);
      await pumpLink(tester, wallet, FakeAccountsService());

      // Elige la primera cuenta del titular (de `GET /accounts`).
      await tester.tap(find.byKey(const Key('wallet-link-account-field')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('****1234 · PEN').last);
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('wallet-link-save')));
      await tester.pumpAndSettle();

      expect(wallet.linkBodies, hasLength(1));
      expect(wallet.linkBodies.single['type'], 'ACCOUNT');
      expect(wallet.linkBodies.single['account_id'], 'acc-1');
      expect(wallet.linkBodies.single.containsKey('card'), isFalse);
      // La `Idempotency-Key` viaja (generada por intento).
      expect(wallet.linkKeys.single, isNotNull);
      expect(wallet.linkKeys.single, isNotEmpty);

      // Exito: vinculo enmascarado ACTIVE, sin numero completo.
      expect(find.byKey(const Key('wallet-link-success')), findsOneWidget);
      expect(find.text('Fuente vinculada'), findsOneWidget);
      expect(find.text('Cuenta ****4321'), findsOneWidget);
      expect(find.text('Activa'), findsOneWidget);

      await tester.tap(find.byKey(const Key('wallet-link-view-wallet')));
      await tester.pumpAndSettle();
      expect(find.text('Billetera'), findsOneWidget);
    });

    testWidgets('vincular tarjeta envia PAN crudo y muestra solo last4', (
      tester,
    ) async {
      const pan = '4111111111111111';
      final wallet = FakeWalletService(seed: Wallet.empty);
      await pumpLink(tester, wallet, FakeAccountsService());

      await tester.tap(find.byKey(const Key('wallet-link-kind-card')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('wallet-link-pan')),
        pan,
      );
      await tester.pump();

      await tester.tap(find.byKey(const Key('wallet-link-save')));
      await tester.pumpAndSettle();

      // El request lleva el PAN crudo (el backend lo tokeniza); la
      // respuesta y la UI solo exponen `last4`.
      expect(wallet.linkBodies, hasLength(1));
      final body = wallet.linkBodies.single;
      expect(body['type'], 'CARD');
      expect((body['card'] as Map)['pan'], pan);
      expect(body.containsKey('account_id'), isFalse);

      expect(find.byKey(const Key('wallet-link-success')), findsOneWidget);
      expect(find.text('Tarjeta VISA •••• 1111'), findsOneWidget);
      // El PAN completo nunca se muestra en pantalla.
      expect(find.textContaining(pan), findsNothing);
    });

    testWidgets('reintento tras fallo reusa la misma Idempotency-Key', (
      tester,
    ) async {
      final wallet = FakeWalletService(seed: Wallet.empty)
        ..failNextLink = true;
      await pumpLink(tester, wallet, FakeAccountsService());

      await tester.tap(find.byKey(const Key('wallet-link-account-field')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('****1234 · PEN').last);
      await tester.pumpAndSettle();

      // Primer intento: falla por red.
      await tester.tap(find.byKey(const Key('wallet-link-save')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('wallet-link-error')), findsOneWidget);

      // Reintentar: misma clave, sin duplicar el intento.
      await tester.tap(find.byKey(const Key('wallet-link-save')));
      await tester.pumpAndSettle();

      expect(wallet.linkBodies, hasLength(2));
      expect(wallet.linkKeys, hasLength(2));
      expect(wallet.linkKeys[0], isNotNull);
      expect(wallet.linkKeys[1], wallet.linkKeys[0]);
      expect(find.byKey(const Key('wallet-link-success')), findsOneWidget);
    });

    testWidgets('duplicado muestra banner con codigo y mensaje', (
      tester,
    ) async {
      final wallet = FakeWalletService()
        ..linkError = ApiException(
          code: 'DUPLICATE_LINK',
          message: 'Duplicado.',
        );
      await pumpLink(tester, wallet, FakeAccountsService());

      await tester.tap(find.byKey(const Key('wallet-link-account-field')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('****1234 · PEN').last);
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('wallet-link-save')));
      await tester.pumpAndSettle();

      expect(
        find.byKey(const Key('wallet-duplicate-banner')),
        findsOneWidget,
      );
      expect(find.text('DUPLICATE_LINK'), findsOneWidget);
      expect(
        find.text('Esa fuente ya está vinculada a tu billetera.'),
        findsOneWidget,
      );
    });

    testWidgets('422 muestra error accionable bajo el formulario', (
      tester,
    ) async {
      final wallet = FakeWalletService()
        ..linkError = ApiException(
          code: 'VALIDATION_ERROR',
          message: 'Invalido.',
        );
      await pumpLink(tester, wallet, FakeAccountsService());

      await tester.tap(find.byKey(const Key('wallet-link-kind-card')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('wallet-link-pan')),
        '4111111111111111',
      );
      await tester.tap(find.byKey(const Key('wallet-link-save')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('wallet-link-error')), findsOneWidget);
      expect(
        find.text('Revisa los datos ingresados e inténtalo de nuevo.'),
        findsOneWidget,
      );
    });

    testWidgets('PAN corto muestra error de formato sin llamar al servidor',
        (tester) async {
      final wallet = FakeWalletService();
      await pumpLink(tester, wallet, FakeAccountsService());

      await tester.tap(find.byKey(const Key('wallet-link-kind-card')));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const Key('wallet-link-pan')), '123');
      await tester.tap(find.byKey(const Key('wallet-link-save')));
      await tester.pump();

      expect(
        find.text('Ingresa los dígitos de tu tarjeta (13 a 19).'),
        findsOneWidget,
      );
      expect(wallet.linkBodies, isEmpty);
    });

    testWidgets('sin cuenta elegida pide elegir sin llamar al servidor',
        (tester) async {
      final wallet = FakeWalletService();
      await pumpLink(tester, wallet, FakeAccountsService());

      await tester.tap(find.byKey(const Key('wallet-link-save')));
      await tester.pump();

      expect(
        find.text('Elige una de tus cuentas para vincularla.'),
        findsOneWidget,
      );
      expect(wallet.linkBodies, isEmpty);
    });
  });

  group('WalletService (HTTP)', () {
    ApiClient stubClient(
      Response<dynamic> Function(RequestOptions options) handler, {
      Map<String, dynamic>? Function(RequestOptions options)? onRequest,
    }) {
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
      final api = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 'token'),
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, h) {
            onRequest?.call(options);
            h.resolve(handler(options));
          },
        ),
      );
      return api;
    }

    const walletBody = {
      'data': {
        'wallet_id': 'w-1',
        'status': 'ACTIVE',
        'links': [
          {
            'id': 'l-1',
            'type': 'ACCOUNT',
            'account_masked': '****1234',
            'last4': null,
            'brand': null,
            'status': 'ACTIVE',
            'linked_at': '2026-10-13T00:00:00Z',
          },
        ],
      },
      'meta': {'request_id': 'r-1'},
    };

    test('getWallet parsea billetera vacia (200 sin billetera)', () async {
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': {'wallet_id': null, 'status': 'NONE', 'links': []},
            'meta': {'request_id': 'r-1'},
          },
        ),
      );

      final wallet = await WalletService(api: api).getWallet();

      expect(wallet.walletId, isNull);
      expect(wallet.status, 'NONE');
      expect(wallet.links, isEmpty);
    });

    test('getWallet parsea vinculos enmascarados sin recalculo', () async {
      final api = stubClient(
        (o) => Response(requestOptions: o, statusCode: 200, data: walletBody),
      );

      final wallet = await WalletService(api: api).getWallet();

      expect(wallet.walletId, 'w-1');
      expect(wallet.links, hasLength(1));
      expect(wallet.links.single.accountMasked, '****1234');
      expect(wallet.links.single.maskedLabel, 'Cuenta ****1234');
    });

    test('getWallet parsea is_merchant del servidor (E4-T23)', () async {
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': {
              'wallet_id': 'w-1',
              'status': 'ACTIVE',
              'links': [],
              'is_merchant': true,
              'merchant_status': 'ACTIVE',
            },
            'meta': {'request_id': 'r-1'},
          },
        ),
      );

      final wallet = await WalletService(api: api).getWallet();

      expect(wallet.isMerchant, isTrue);
      expect(wallet.merchantStatus, 'ACTIVE');
    });

    test('getWallet sin flag degrada a personal (fail-closed, E4-T23)',
        () async {
      final api = stubClient(
        (o) => Response(requestOptions: o, statusCode: 200, data: walletBody),
      );

      final wallet = await WalletService(api: api).getWallet();

      expect(wallet.isMerchant, isFalse);
      expect(wallet.merchantStatus, isNull);
    });

    test('linkAccount envia account_id con Idempotency-Key reutilizable',
        () async {
      String? idempotencyKey;
      Map<String, dynamic>? capturedBody;
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: {
            'data': {
              'id': 'l-new',
              'type': 'ACCOUNT',
              'account_masked': '****4321',
              'status': 'ACTIVE',
              'linked_at': '2026-10-13T00:00:00Z',
            },
            'meta': {'request_id': 'r-1', 'replayed': false},
          },
        ),
        onRequest: (o) {
          idempotencyKey = o.headers['Idempotency-Key']?.toString();
          if (o.data is Map) {
            capturedBody = Map<String, dynamic>.from(o.data as Map);
          }
          return null;
        },
      );

      final link = await WalletService(api: api).linkAccount(
        const AccountLinkInput(accountId: 'acc-1'),
        idempotencyKey: 'key-1',
      );

      expect(idempotencyKey, 'key-1');
      expect(capturedBody!['type'], 'ACCOUNT');
      expect(capturedBody!['account_id'], 'acc-1');
      expect(capturedBody!.containsKey('card'), isFalse);
      // La respuesta no trae PAN ni token_ref (el modelo ni los tiene).
      expect(link.accountMasked, '****4321');
      expect(link.status, 'ACTIVE');
    });

    test('linkCard envia PAN crudo y recibe solo last4', () async {
      Map<String, dynamic>? capturedBody;
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 201,
          data: {
            'data': {
              'id': 'l-new-card',
              'type': 'CARD',
              'last4': '1111',
              'brand': 'VISA',
              'status': 'ACTIVE',
              'linked_at': '2026-10-13T00:00:00Z',
            },
            'meta': {'request_id': 'r-1', 'replayed': false},
          },
        ),
        onRequest: (o) {
          if (o.data is Map) {
            capturedBody = Map<String, dynamic>.from(o.data as Map);
          }
          return null;
        },
      );

      final link = await WalletService(api: api).linkCard(
        const CardLinkInput(pan: '4111111111111111'),
      );

      final card = capturedBody!['card'] as Map;
      expect(card['pan'], '4111111111111111');
      expect(capturedBody!.containsKey('account_id'), isFalse);
      expect(link.last4, '1111');
      expect(link.maskedLabel, 'Tarjeta VISA •••• 1111');
    });

    test('unlink envia DELETE y parsea el vinculo INACTIVE', () async {
      String? path;
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': {
              'id': 'l-1',
              'type': 'ACCOUNT',
              'account_masked': '****1234',
              'status': 'INACTIVE',
              'linked_at': '2026-10-13T00:00:00Z',
            },
            'meta': {'request_id': 'r-1'},
          },
        ),
        onRequest: (o) {
          path = o.path;
          return null;
        },
      );

      final link = await WalletService(api: api).unlink('l-1');

      expect(path, endsWith('/wallet/links/l-1'));
      expect(link.id, 'l-1');
      expect(link.status, 'INACTIVE');
      expect(link.isActive, isFalse);
    });

    test('unlink 404 se mapea a NOT_FOUND neutro', () async {
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
      final api = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 'token'),
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) => handler.reject(
            DioException(
              requestOptions: options,
              response: Response(
                requestOptions: options,
                statusCode: 404,
                data: {
                  'error': {'code': 'NOT_FOUND', 'message': 'No existe.'},
                },
              ),
              type: DioExceptionType.badResponse,
            ),
          ),
        ),
      );

      try {
        await WalletService(api: api).unlink('ajeno-o-inexistente');
        fail('debió lanzar ApiException');
      } on ApiException catch (e) {
        expect(e.code, 'NOT_FOUND');
        expect(e.statusCode, 404);
      }
    });

    test('409 DUPLICATE_LINK se mapea al codigo de negocio', () async {
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
      final api = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 'token'),
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) => handler.reject(
            DioException(
              requestOptions: options,
              response: Response(
                requestOptions: options,
                statusCode: 409,
                data: {
                  'error': {
                    'code': 'DUPLICATE_LINK',
                    'message': 'Duplicado.',
                  },
                },
              ),
              type: DioExceptionType.badResponse,
            ),
          ),
        ),
      );

      try {
        await WalletService(api: api).linkAccount(
          const AccountLinkInput(accountId: 'acc-1'),
        );
        fail('debió lanzar ApiException');
      } on ApiException catch (e) {
        expect(e.code, 'DUPLICATE_LINK');
      }
    });
  });
}
