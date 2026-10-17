import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/accounts/data/accounts_service.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/home/data/profile_service.dart';
import 'package:banca_online/features/home/presentation/home_page.dart';
import 'package:banca_online/features/wallet/data/wallet_service.dart';
import 'package:banca_online/features/wallet/models/wallet.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fakes del servicio (seam `AccountsServiceBase`): sin red, con datos fijos
/// que replican los shapes del backend (solo shapes).
///
/// El total del servidor (`S/ 999.99`) es **distinto** a la suma de las
/// cuentas (`S/ 420.00 + S/ 1,200.00 = S/ 1,620.00`): asi el guard convertido
/// prueba que el hero muestra el monto del servidor y jamas la suma calculada
/// en el cliente (cliente delgado, `docs/19` §4/§8).
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

/// Total del servidor distinto (para probar que el monto viene del servidor).
class _AltTotalsFake extends _OkFake {
  @override
  Future<AccountsTotals> getAccountsTotals() async => const AccountsTotals(
        asOf: '2026-08-24T09:05:00Z',
        primaryCurrency: 'PEN',
        primaryTotalMinor: 125000,
        totals: [
          CurrencyTotal(
            currency: 'PEN',
            availableMinor: 117000,
            heldMinor: 8000,
            totalMinor: 125000,
          ),
        ],
      );
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

/// El total falla pero las cuentas cargan: el hero degrada sin tumbar la home.
class _TotalsErrorFake extends _OkFake {
  @override
  Future<AccountsTotals> getAccountsTotals() async => throw ApiException(
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

/// Cuentas listas pero el total colgado: el hero muestra su carga parcial.
class _HangingTotalsFake extends _OkFake {
  final completer = Completer<AccountsTotals>();

  @override
  Future<AccountsTotals> getAccountsTotals() => completer.future;
}

/// Fake de perfil (`GET /me`): titular persona natural.
class _OkProfileFake implements ProfileServiceBase {
  @override
  Future<Profile> getProfile() async => const Profile(
        firstName: 'Ana',
        lastName: 'Lopez',
      );
}

/// Fake de perfil con razon social (RUC juridica: nombres vacios).
class _BusinessProfileFake implements ProfileServiceBase {
  @override
  Future<Profile> getProfile() async => const Profile(
        firstName: '',
        lastName: '',
        businessName: 'ACME SAC',
      );
}

/// Fake contador de perfil (H2): cuenta llamadas a `GET /me` para probar
/// que el header memoiza el Future y no re-pide en cada rebuild.
class _CountingProfileFake implements ProfileServiceBase {
  var calls = 0;

  @override
  Future<Profile> getProfile() async {
    calls++;
    return const Profile(firstName: 'Ana', lastName: 'Lopez');
  }
}

Widget _harness(Widget child) => MaterialApp(home: Scaffold(body: child));

/// Fake de billetera con rol configurable (E4-T23): `GET /wallet`
/// `is_merchant` decide si el inicio muestra "Cobrar QR" (`qa-qr`).
class _RoleWalletFake implements WalletServiceBase {
  _RoleWalletFake.merchant()
      : wallet = const Wallet(
          walletId: 'w-1',
          status: 'ACTIVE',
          links: [],
          isMerchant: true,
          merchantStatus: 'ACTIVE',
        ),
        error = null;

  _RoleWalletFake.personal()
      : wallet = const Wallet(
          walletId: 'w-1',
          status: 'ACTIVE',
          links: [],
        ),
        error = null;

  _RoleWalletFake({required this.wallet, this.error});

  final Wallet wallet;
  final ApiException? error;

  @override
  Future<Wallet> getWallet() async {
    if (error != null) throw error!;
    return wallet;
  }

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

/// Home con fakes sanos (cuentas + total del servidor + perfil + rol de
/// comercio: el dash de referencia es el de un comercio, que ve "Cobrar QR").
Widget _okHarness({ProfileServiceBase? profile}) => _harness(
      HomePage(
        service: _OkFake(),
        profileService: profile ?? _OkProfileFake(),
        walletService: _RoleWalletFake.merchant(),
      ),
    );

void main() {
  testWidgets('home redisenado renderiza el dash con datos reales',
      (tester) async {
    await tester.pumpWidget(_okHarness());
    await tester.pumpAndSettle();

    expect(
      find.text('Pantalla de inicio (placeholder F-T01).'),
      findsNothing,
    );
    // Topbar con saludo real + hero del servidor + cuentas (tramo visible).
    expect(find.text('Hola, Ana'), findsOneWidget);
    expect(find.text('AL'), findsOneWidget);
    expect(find.text('SALDO TOTAL • PEN (S/)'), findsOneWidget);
    expect(find.text('S/ 999.99'), findsOneWidget);
    // El `as_of` del servidor se refleja (hora local, sin fecha fija).
    expect(find.textContaining('Actualizado hoy •'), findsOneWidget);
    expect(find.text('MIS CUENTAS'), findsOneWidget);
    expect(find.text('AHORRO ****1234'), findsOneWidget);
    expect(find.text('CORRIENTE ****5678'), findsOneWidget);
    expect(find.text('S/ 420.00 disponible'), findsOneWidget);
    expect(find.text('Ver todas las cuentas'), findsOneWidget);
    // Quick-actions del `dash` (0:3082) y bottom-nav (F-T55).
    expect(find.text('Transferir'), findsOneWidget);
    expect(find.text('Crédito'), findsOneWidget);
    expect(find.text('Cobrar QR'), findsOneWidget);
    expect(find.text('Recargar'), findsOneWidget);
    expect(find.byKey(const Key('bottomNav')), findsOneWidget);
    // La lista construye por demanda y recicla lo que sale de pantalla:
    // se baja hasta el banner y se asevera el tramo inferior.
    await tester.scrollUntilVisible(
      find.text('Token Digital activo'),
      300,
    );
    await tester.pumpAndSettle();
    expect(find.text('ÚLTIMOS MOVIMIENTOS'), findsOneWidget);
    expect(find.text('Abono enero'), findsOneWidget);
    expect(find.text('Pago febrero'), findsOneWidget);
    expect(find.text('Ver todos los movimientos'), findsOneWidget);
    expect(find.text('Token Digital activo'), findsOneWidget);
  });

  testWidgets('home muestra numeros enmascarados tal como llegan',
      (tester) async {
    await tester.pumpWidget(_okHarness());
    await tester.pumpAndSettle();

    // El enmascarado viaja del backend y se muestra sin tocar.
    expect(find.textContaining('****1234'), findsWidgets);
    expect(find.textContaining('****5678'), findsWidgets);
  });

  testWidgets(
      'GUARD cliente delgado: el monto viene del servidor, no se suma '
      '(F-T54 convierte el guard F-T41)', (tester) async {
    await tester.pumpWidget(_okHarness());
    await tester.pumpAndSettle();

    // Las cuentas suman S/ 1,620.00 pero el servidor dice S/ 999.99:
    // la UI muestra el del servidor y jamas la suma del cliente.
    expect(find.text('SALDO TOTAL • PEN (S/)'), findsOneWidget);
    expect(find.text('S/ 999.99'), findsOneWidget);
    expect(find.text('S/ 1,620.00'), findsNothing);
  });

  testWidgets('el hero refleja el total que responde el servidor',
      (tester) async {
    await tester.pumpWidget(
      _harness(
        HomePage(
          service: _AltTotalsFake(),
          profileService: _OkProfileFake(),
        ),
      ),
    );
    await tester.pumpAndSettle();

    // Cambiar el total del servidor cambia lo mostrado (viene del servidor).
    expect(find.text('S/ 1,250.00'), findsOneWidget);
    expect(find.text('S/ 999.99'), findsNothing);
    expect(find.text('S/ 1,620.00'), findsNothing);
  });

  testWidgets('el ojo oculta/muestra el monto sin recalcular', (tester) async {
    await tester.pumpWidget(_okHarness());
    await tester.pumpAndSettle();

    expect(find.text('S/ 999.99'), findsOneWidget);
    await tester.tap(find.byKey(const Key('heroEye')));
    await tester.pumpAndSettle();

    expect(find.text('S/ 999.99'), findsNothing);
    expect(find.text('S/ ••••••'), findsOneWidget);

    await tester.tap(find.byKey(const Key('heroEye')));
    await tester.pumpAndSettle();

    expect(find.text('S/ 999.99'), findsOneWidget);
  });

  testWidgets('Analizar es marcador deshabilitado sin SnackBar ni navegación',
      (tester) async {
    final router = GoRouter(
      initialLocation: '/home',
      routes: [
        GoRoute(
          path: '/home',
          builder: (context, state) => HomePage(
            service: _OkFake(),
            profileService: _OkProfileFake(),
          ),
        ),
        GoRoute(
          path: '/movements',
          builder: (context, state) =>
              const Scaffold(body: Text('Historial (fake)')),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(MaterialApp.router(routerConfig: router));
    await tester.pumpAndSettle();

    // Affordance clara de no disponible (badge + tooltip, atenuado).
    expect(find.byKey(const Key('heroAnalyze')), findsOneWidget);
    expect(find.text('Analizar'), findsOneWidget);
    expect(find.text('Próximamente'), findsOneWidget);
    expect(
      find.byWidgetPredicate(
        (w) => w is Tooltip && w.message == 'Próximamente',
      ),
      findsOneWidget,
    );
    // No es tappable: tocarlo no dispara SnackBar ni navega.
    await tester.tap(
      find.byKey(const Key('heroAnalyze')),
      warnIfMissed: false,
    );
    await tester.pumpAndSettle();
    expect(find.byType(SnackBar), findsNothing);
    expect(find.text('Historial (fake)'), findsNothing);
    expect(find.byKey(const Key('homeHero')), findsOneWidget);
  });

  testWidgets('quick-actions QR navegan a wallet (C1, sin Proximamente)',
      (tester) async {
    final router = GoRouter(
      initialLocation: '/home',
      routes: [
        GoRoute(
          path: '/home',
          builder: (context, state) => HomePage(
            service: _OkFake(),
            profileService: _OkProfileFake(),
            walletService: _RoleWalletFake.merchant(),
          ),
        ),
        GoRoute(
          path: '/wallet/merchant-qr',
          builder: (context, state) =>
              const Scaffold(body: Text('Cobro QR (fake)')),
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

    // `Cobrar QR` -> `/wallet/merchant-qr` (C1).
    await tester.tap(find.byKey(const Key('qa-qr')));
    await tester.pumpAndSettle();
    expect(find.text('Cobro QR (fake)'), findsOneWidget);
    expect(find.byType(SnackBar), findsNothing);
  });

  testWidgets('quick-action Recargar navega a /wallet/topups (C1)',
      (tester) async {
    final router = GoRouter(
      initialLocation: '/home',
      routes: [
        GoRoute(
          path: '/home',
          builder: (context, state) => HomePage(
            service: _OkFake(),
            profileService: _OkProfileFake(),
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
    expect(find.byType(SnackBar), findsNothing);
  });

  testWidgets('quick-action Transferir navega a /transfers', (tester) async {
    final router = GoRouter(
      initialLocation: '/home',
      routes: [
        GoRoute(
          path: '/home',
          builder: (context, state) => HomePage(
            service: _OkFake(),
            profileService: _OkProfileFake(),
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

    await tester.tap(find.byKey(const Key('qa-transferir')));
    await tester.pumpAndSettle();

    expect(find.text('Transferencias (fake)'), findsOneWidget);
    expect(find.byType(SnackBar), findsNothing);
  });

  testWidgets('quick-action Crédito navega a /credits', (tester) async {
    final router = GoRouter(
      initialLocation: '/home',
      routes: [
        GoRoute(
          path: '/home',
          builder: (context, state) => HomePage(
            service: _OkFake(),
            profileService: _OkProfileFake(),
          ),
        ),
        GoRoute(
          path: '/credits',
          builder: (context, state) =>
              const Scaffold(body: Text('Créditos (fake)')),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(MaterialApp.router(routerConfig: router));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('qa-creditos')));
    await tester.pumpAndSettle();

    expect(find.text('Créditos (fake)'), findsOneWidget);
    expect(find.byType(SnackBar), findsNothing);
  });

  testWidgets('bottom-nav: Tarjetas navega a /wallet (C1)', (tester) async {
    final router = GoRouter(
      initialLocation: '/home',
      routes: [
        GoRoute(
          path: '/home',
          builder: (context, state) => HomePage(
            service: _OkFake(),
            profileService: _OkProfileFake(),
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

    // C1: Tarjetas (2) es entrada real a `/wallet` (sin SnackBar).
    await tester.tap(find.byKey(const Key('bottomNav-tarjetas')));
    await tester.pumpAndSettle();
    expect(find.byType(SnackBar), findsNothing);
    expect(find.text('Billetera (fake)'), findsOneWidget);
  });

  testWidgets('bottom-nav: Inicio es no-op sin SnackBar', (tester) async {
    await tester.pumpWidget(_okHarness());
    await tester.pumpAndSettle();

    // Auditoria M5: Inicio (0) no dispara "Próximamente": se sigue en home.
    await tester.tap(find.byKey(const Key('bottomNav-inicio')));
    await tester.pumpAndSettle();
    expect(find.byType(SnackBar), findsNothing);
    expect(find.byKey(const Key('homeHero')), findsOneWidget);
  });

  testWidgets('bottom-nav Operar navega a /transfers', (tester) async {
    final router = GoRouter(
      initialLocation: '/home',
      routes: [
        GoRoute(
          path: '/home',
          builder: (context, state) => HomePage(
            service: _OkFake(),
            profileService: _OkProfileFake(),
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

    await tester.tap(find.byKey(const Key('bottomNav-operar')));
    await tester.pumpAndSettle();

    expect(find.text('Transferencias (fake)'), findsOneWidget);
    expect(find.byType(SnackBar), findsNothing);
  });

  testWidgets('Ver todos los movimientos abre /movements', (tester) async {
    final router = GoRouter(
      initialLocation: '/home',
      routes: [
        GoRoute(
          path: '/home',
          builder: (context, state) => HomePage(
            service: _OkFake(),
            profileService: _OkProfileFake(),
          ),
        ),
        GoRoute(
          path: '/movements',
          builder: (context, state) =>
              const Scaffold(body: Text('Historial (fake)')),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(MaterialApp.router(routerConfig: router));
    await tester.pumpAndSettle();

    await tester.scrollUntilVisible(
      find.text('Ver todos los movimientos'),
      300,
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('Ver todos los movimientos'));
    await tester.pumpAndSettle();

    expect(find.text('Historial (fake)'), findsOneWidget);
  });

  testWidgets('bottom-nav Perfil navega a /profile', (tester) async {
    final router = GoRouter(
      initialLocation: '/home',
      routes: [
        GoRoute(
          path: '/home',
          builder: (context, state) => HomePage(
            service: _OkFake(),
            profileService: _OkProfileFake(),
          ),
        ),
        GoRoute(
          path: '/profile',
          builder: (context, state) =>
              const Scaffold(body: Text('Mi perfil (fake)')),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(MaterialApp.router(routerConfig: router));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('bottomNav-perfil')));
    await tester.pumpAndSettle();

    expect(find.text('Mi perfil (fake)'), findsOneWidget);
  });

  testWidgets('topbar con razon social usa business_name', (tester) async {
    await tester.pumpWidget(_harness(
      HomePage(service: _OkFake(), profileService: _BusinessProfileFake()),
    ));
    await tester.pumpAndSettle();

    expect(find.text('Hola, ACME SAC'), findsOneWidget);
    expect(find.text('AS'), findsOneWidget);
  });

  testWidgets('topbar sin perfil usa el fallback Hola (router intacto)',
      (tester) async {
    await tester.pumpWidget(_harness(const HomePage()));
    await tester.pumpAndSettle();

    // Sin servicios: cabecera generica + error accionable de cuentas.
    expect(find.text('Hola'), findsOneWidget);
    expect(
      find.text('Servicio de cuentas no configurado.'),
      findsOneWidget,
    );
  });

  testWidgets('hero sin total degrada sin inventar montos', (tester) async {
    await tester.pumpWidget(
      _harness(
        HomePage(
          service: _TotalsErrorFake(),
          profileService: _OkProfileFake(),
        ),
      ),
    );
    await tester.pumpAndSettle();

    // El resto de la home se conserva; el hero no inventa ningun monto.
    expect(find.text('MIS CUENTAS'), findsOneWidget);
    expect(find.text('Total no disponible por ahora.'), findsOneWidget);
    expect(find.text('S/ 999.99'), findsNothing);
    expect(find.text('S/ 1,620.00'), findsNothing);
  });

  testWidgets('hero muestra carga parcial del total', (tester) async {
    await tester.pumpWidget(
      _harness(
        HomePage(
          service: _HangingTotalsFake(),
          profileService: _OkProfileFake(),
        ),
      ),
    );
    await tester.pump();

    expect(find.text('Cargando total…'), findsOneWidget);
    expect(find.text('MIS CUENTAS'), findsOneWidget);
  });

  testWidgets('home cubre el estado cargando', (tester) async {
    await tester.pumpWidget(_harness(HomePage(service: _HangingFake())));
    await tester.pump();

    expect(find.text('Cargando tu inicio...'), findsOneWidget);
    expect(find.text('Hola'), findsOneWidget);
  });

  testWidgets('home cubre el estado vacio', (tester) async {
    await tester.pumpWidget(
      _harness(
        HomePage(
          service: _EmptyFake(),
          profileService: _OkProfileFake(),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Aún no tienes cuentas.'), findsOneWidget);
    expect(find.text('MIS CUENTAS'), findsNothing);
  });

  testWidgets('home cubre error con reintento accionable', (tester) async {
    final fake = _FlakyFake();
    await tester.pumpWidget(
      _harness(
        HomePage(service: fake, profileService: _OkProfileFake()),
      ),
    );
    await tester.pumpAndSettle();

    expect(
      find.text('Sin conexion. Revisa tu red e intentalo de nuevo.'),
      findsOneWidget,
    );
    await tester.tap(find.text('Reintentar'));
    await tester.pumpAndSettle();

    expect(fake.calls, 2);
    expect(find.text('MIS CUENTAS'), findsOneWidget);
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
    await tester.pumpWidget(
      _harness(
        HomePage(service: _ErrorFake(), profileService: _OkProfileFake()),
      ),
    );
    await tester.pumpAndSettle();

    expect(
      find.text('Sin conexion. Revisa tu red e intentalo de nuevo.'),
      findsOneWidget,
    );
  });

  testWidgets('H2: el header pide GET /me una sola vez ante rebuilds',
      (tester) async {
    final profile = _CountingProfileFake();
    Widget build() => _harness(
          HomePage(service: _OkFake(), profileService: profile),
        );
    await tester.pumpWidget(build());
    await tester.pumpAndSettle();
    expect(find.text('Hola, Ana'), findsOneWidget);
    expect(profile.calls, 1);

    // Rebuild del padre con la misma instancia (p. ej. retry/pull-to-refresh
    // que hace setState en HomePage): sin el fix cada `build` llamaba a
    // `service?.getProfile()` y el contador subia a 2+; memoizado sigue en 1.
    await tester.pumpWidget(build());
    await tester.pump();
    await tester.pumpWidget(build());
    await tester.pumpAndSettle();

    expect(find.text('Hola, Ana'), findsOneWidget);
    expect(profile.calls, 1);
  });

  group('rol de comercio en el inicio (E4-T23)', () {
    Widget homeWithWallet(WalletServiceBase? wallet) => _harness(
          HomePage(
            service: _OkFake(),
            profileService: _OkProfileFake(),
            walletService: wallet,
          ),
        );

    testWidgets('comercio ve la accion rapida Cobrar QR', (tester) async {
      await tester.pumpWidget(homeWithWallet(_RoleWalletFake.merchant()));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qa-qr')), findsOneWidget);
      expect(find.text('Cobrar QR'), findsOneWidget);
    });

    testWidgets('cuenta personal no ve Cobrar QR (otras intactas)',
        (tester) async {
      await tester.pumpWidget(homeWithWallet(_RoleWalletFake.personal()));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qa-qr')), findsNothing);
      expect(find.text('Cobrar QR'), findsNothing);
      // Las demas quick-actions siguen visibles.
      expect(find.byKey(const Key('qa-transferir')), findsOneWidget);
      expect(find.byKey(const Key('qa-creditos')), findsOneWidget);
      expect(find.byKey(const Key('qa-recargar')), findsOneWidget);
    });

    testWidgets('sin servicio de billetera no se muestra (fail-closed)',
        (tester) async {
      await tester.pumpWidget(homeWithWallet(null));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qa-qr')), findsNothing);
      expect(find.byKey(const Key('qa-transferir')), findsOneWidget);
    });

    testWidgets('error de red oculta Cobrar QR (fail-closed)', (tester) async {
      final failing = _RoleWalletFake(
        wallet: _RoleWalletFake.merchant().wallet,
        error: ApiException.network(),
      );
      await tester.pumpWidget(homeWithWallet(failing));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qa-qr')), findsNothing);
      expect(find.byKey(const Key('qa-recargar')), findsOneWidget);
    });
  });
}
