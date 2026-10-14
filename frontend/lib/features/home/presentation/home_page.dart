import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_bottom_nav.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../../accounts/data/accounts_service.dart';
import '../../accounts/models/account.dart';
import '../../accounts/models/movement_concept.dart';
import '../../accounts/utils/format.dart';
import '../data/profile_service.dart';

/// Pantalla `home` redisenada (F-T54, HU05; diseno `dash` pagina `0:3057`).
///
/// - Topbar eucalipto (`0:3059`) con avatar ocre e iniciales del titular de
///   `GET /me` (E1-T44), saludo real (`Hola, {nombre}`) con fallback generico
///   `Hola` (sin PII) cuando el perfil no esta disponible, campana visual y
///   acceso a `/profile` (F-T52, el avatar navega).
/// - Hero eucalipto (`0:3070`) con el **SALDO TOTAL del servidor**
///   (`GET /accounts/totals`, E1-T43: `primary_total_minor` + `as_of`).
///   **El cliente NUNCA suma saldos** (cliente delgado, `docs/19` §4/§8): el
///   monto se muestra tal cual responde el servidor; el ojo solo oculta el
///   texto en pantalla (estado local, sin recalcular ni llamar al servidor).
/// - Quick-actions (`0:3082`): `Transferir` navega a `/transfers`,
///   `Credito` a `/credits`, `Cobrar QR` a `/wallet/merchant-qr` y
///   `Recargar` a `/wallet/topups` (C1, Sprint 3); `Analizar` es un
///   marcador deshabilitado (no tappable, atenuado + badge/tooltip
///   "Próximamente", sin SnackBar ni navegación) hasta que exista un
///   destino real de análisis; no se apunta a `/movements` para no
///   duplicar "Ver todos los movimientos". Bottom-nav:
///   `Operar` (1) -> `/transfers`, `Tarjetas` (2) -> `/wallet` (C1),
///   `Perfil` (3) -> `/profile`; `Inicio` (0)
///   es no-op (ya estamos en home).
/// - `MIS CUENTAS` (`0:3108`, de `GET /accounts`, pull-to-refresh) +
///   `ULTIMOS MOVIMIENTOS` (`0:3130`, preview de la primera cuenta,
///   `GET /accounts/{id}/movements`, pagina 1, 3 items) + banner
///   `Token Digital activo` (`0:3160`, informativo).
/// - Numero enmascarado TAL COMO viene (`account_number_masked`); el cliente
///   nunca desenmascara (`docs/20` §8).
/// - 4 estados (`docs/20` §7) con `LoadingView`/`EmptyView`/`ErrorView` y
///   reintento accionable.
///
/// [service]/[profileService] son inyectables para tests. Las rutas los
/// resuelven desde `accountsServiceFactory`/`homeProfileServiceFactory`
/// (cableadas por el orquestador); `null` degrada con elegancia (fallback
/// `Hola` para el perfil; error accionable para las cuentas) en vez de romper.
class HomePage extends StatefulWidget {
  const HomePage({super.key, this.service, this.profileService});

  final AccountsServiceBase? service;
  final ProfileServiceBase? profileService;

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  late Future<List<Account>> _accountsFuture;
  late Future<AccountsTotals> _totalsFuture;

  /// Llave del preview de movimientos: permite recargarlo en
  /// *pull-to-refresh* y al re-entrar a `/home` (E2-T35).
  final _previewKey = GlobalKey<_MovementsPreviewCardState>();

  /// Router observado para recargar el preview al volver a `/home`.
  GoRouter? _listenedRouter;

  @override
  void initState() {
    super.initState();
    _accountsFuture = _loadAccounts();
    _totalsFuture = _loadTotals();
    // El hero de totales se suscribe tarde (solo en la rama de contenido):
    // marcar los errores como manejados evita reportes de zona por futuros
    // sin suscriptor; los FutureBuilder siguen recibiendo valores/errores.
    _accountsFuture.ignore();
    _totalsFuture.ignore();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // Suscripcion al delegate (una sola vez por router): cada navegacion lo
    // notifica, este o no este `State` en la ruta superior. Sin `GoRouter`
    // (tests con `MaterialApp` plano) no se observa nada.
    final router = GoRouter.maybeOf(context);
    if (!identical(router, _listenedRouter)) {
      _listenedRouter?.routerDelegate.removeListener(_onRouteChanged);
      _listenedRouter = router;
      router?.routerDelegate.addListener(_onRouteChanged);
    }
  }

  @override
  void dispose() {
    _listenedRouter?.routerDelegate.removeListener(_onRouteChanged);
    _listenedRouter = null;
    super.dispose();
  }

  /// Al volver a `/home` (p. ej. tras operar en `/transfers`, cuyo CTA
  /// vuelve con `context.go('/home')`) se recarga el preview sin reiniciar
  /// la app. La primera carga la hace `initState` (el delegate no notifica
  /// al montar, asi que no hay doble fetch).
  void _onRouteChanged() {
    if (!mounted) return;
    final router = _listenedRouter;
    if (router == null) return;
    String path;
    try {
      path = router.state.uri.path;
    } catch (_) {
      return;
    }
    if (path == '/home') {
      _previewKey.currentState?.reload();
    }
  }

  Future<List<Account>> _loadAccounts() {
    final service = widget.service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de cuentas no configurado.',
        ),
      );
    }
    return service.getAccounts();
  }

  Future<AccountsTotals> _loadTotals() {
    final service = widget.service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de cuentas no configurado.',
        ),
      );
    }
    return service.getAccountsTotals();
  }

  Future<void> _refresh() async {
    final accounts = _loadAccounts();
    final totals = _loadTotals();
    // El preview tiene su propio estado: tambien se recarga (E2-T35).
    final preview = _previewKey.currentState?.reload() ?? Future.value();
    accounts.ignore();
    totals.ignore();
    setState(() {
      _accountsFuture = accounts;
      _totalsFuture = totals;
    });
    try {
      await accounts;
    } catch (_) {
      // El FutureBuilder muestra el error; el refresh solo reintenta.
    }
    try {
      await totals;
    } catch (_) {
      // El hero muestra su estado neutro; no se tumba la pantalla.
    }
    try {
      await preview;
    } catch (_) {
      // El preview muestra su propio error/reintento.
    }
  }

  void _retry() {
    final accounts = _loadAccounts();
    final totals = _loadTotals();
    accounts.ignore();
    totals.ignore();
    setState(() {
      _accountsFuture = accounts;
      _totalsFuture = totals;
    });
  }

  void _onNavSelect(BuildContext context, int index) {
    // `Inicio` (0) es no-op (ya estamos en `/home`); `Operar` (1) ->
    // `/transfers`; `Tarjetas` (2) -> `/wallet` (C1: entrada real a la
    // billetera); `Perfil` (3) -> `/profile`.
    if (index == 0) return;
    if (index == 1) {
      context.push('/transfers');
      return;
    }
    if (index == 2) {
      context.push('/wallet');
      return;
    }
    if (index == 3) {
      context.push('/profile');
      return;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Column(
          children: [
            _HomeHeader(
              profileService: widget.profileService,
              onBell: () => context.push('/notifications'),
            ),
            Expanded(
              child: FutureBuilder<List<Account>>(
                future: _accountsFuture,
                builder: (context, snapshot) {
                  if (snapshot.connectionState == ConnectionState.waiting) {
                    return const LoadingView(
                      message: 'Cargando tu inicio...',
                    );
                  }
                  if (snapshot.hasError) {
                    final error = snapshot.error;
                    final message = error is ApiException
                        ? error.message
                        : 'Ocurrió un error inesperado. Inténtalo más tarde.';
                    return ErrorView(message: message, onRetry: _retry);
                  }
                  final accounts = snapshot.data ?? const <Account>[];
                  if (accounts.isEmpty) {
                    return RefreshIndicator(
                      onRefresh: _refresh,
                      child: ListView(
                        physics: const AlwaysScrollableScrollPhysics(),
                        children: const [
                          SizedBox(height: 120),
                          EmptyView(
                            message: 'Aún no tienes cuentas.',
                          ),
                        ],
                      ),
                    );
                  }
                  return RefreshIndicator(
                    onRefresh: _refresh,
                    child: ListView(
                      physics: const AlwaysScrollableScrollPhysics(),
                      padding: const EdgeInsets.all(AppSpacing.marginMobile),
                      children: [
                        _TotalsHero(
                          totalsFuture: _totalsFuture,
                        ),
                        const SizedBox(height: AppSpacing.stackMd),
                        const _QuickActions(),
                        const SizedBox(height: AppSpacing.stackMd),
                        _AccountsCard(accounts: accounts),
                        const SizedBox(height: AppSpacing.stackMd),
                        const _AgendaCard(),
                        const SizedBox(height: AppSpacing.stackMd),
                        _MovementsPreviewCard(
                          key: _previewKey,
                          service: widget.service!,
                          account: accounts.first,
                        ),
                        const SizedBox(height: AppSpacing.stackMd),
                        // C1: entrada real a `/wallet` (card compacta). Va
                        // DEBAJO del preview de movimientos a proposito: el
                        // preview debe seguir construyendose en el primer
                        // layout (cacheExtent del `ListView`) para que el
                        // re-entrar a `/home` lo recargue (E2-T35).
                        const _WalletCard(),
                        const SizedBox(height: AppSpacing.stackMd),
                        const _SecurityBanner(),
                      ],
                    ),
                  );
                },
              ),
            ),
          ],
        ),
      ),
      bottomNavigationBar: AppBottomNav(
        activeIndex: 0,
        onSelect: (index) => _onNavSelect(context, index),
      ),
    );
  }
}

/// Topbar eucalipto del `dash` (`0:3059`).
///
/// Avatar ocre con las iniciales del titular de `GET /me`; el avatar es el
/// acceso a `/profile` (key `homeProfile`, area tactil 48px, F-T52).
/// Sin perfil (servicio `null` o error): fallback generico `Hola` sin PII
/// (mantiene `router_test.dart` en verde). La campana (key `homeBell`)
/// navega a `/notifications` (C1: centro del titular, ruta real).
class _HomeHeader extends StatefulWidget {
  const _HomeHeader({required this.profileService, required this.onBell});

  final ProfileServiceBase? profileService;
  final VoidCallback onBell;

  @override
  State<_HomeHeader> createState() => _HomeHeaderState();
}

class _HomeHeaderState extends State<_HomeHeader> {
  Future<Profile?>? _profileFuture;

  @override
  void initState() {
    super.initState();
    // H2: memoiza el Future para pedir GET /me una sola vez por montaje;
    // llamar a `getProfile()` en `build` re-disparaba la peticion en cada
    // rebuild (retry/pull-to-refresh del padre). `null` = sin DI: el
    // FutureBuilder muestra el fallback generico "Hola" sin PII.
    _profileFuture = widget.profileService?.getProfile();
  }

  @override
  void didUpdateWidget(covariant _HomeHeader oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.profileService, widget.profileService)) {
      _profileFuture = widget.profileService?.getProfile();
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      width: double.infinity,
      color: AppColors.primary,
      padding: const EdgeInsets.symmetric(
        horizontal: AppSpacing.containerPadding,
        vertical: AppSpacing.stackSm,
      ),
      child: FutureBuilder<Profile?>(
        future: _profileFuture,
        builder: (context, snapshot) {
          final profile = snapshot.data;
          final name = (profile?.shortName ?? '').trim();
          final greeting = name.isEmpty ? 'Hola' : 'Hola, $name';
          final initials = (profile?.initials ?? '').trim();
          return Row(
            children: [
              // Avatar: acceso a "Mi perfil" (F-T52). El circulo visual
              // mantiene 44px del fig; el area tactil es de 48px sin alterar
              // el layout.
              SizedBox(
                width: 48,
                height: 48,
                child: IconButton(
                  key: const Key('homeProfile'),
                  onPressed: () => context.push('/profile'),
                  padding: EdgeInsets.zero,
                  style: IconButton.styleFrom(
                    minimumSize: const Size(48, 48),
                    tapTargetSize: MaterialTapTargetSize.padded,
                  ),
                  icon: Container(
                    width: 44,
                    height: 44,
                    decoration: const BoxDecoration(
                      shape: BoxShape.circle,
                      color: AppColors.secondaryContainer,
                    ),
                    alignment: Alignment.center,
                    child: initials.isEmpty
                        ? Icon(
                            Icons.person_outline,
                            size: 24,
                            color: scheme.primary,
                          )
                        : Text(
                            initials,
                            style: AppTypography.labelMd.copyWith(
                              color: scheme.primary,
                            ),
                          ),
                  ),
                  color: scheme.onPrimary,
                  tooltip: 'Mi perfil',
                ),
              ),
              const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      'BIENVENIDO • SEGURO',
                      style: AppTypography.labelSm.copyWith(
                        color: scheme.onPrimaryContainer,
                      ),
                    ),
                    const SizedBox(height: AppSpacing.unit),
                    Text(
                      greeting,
                      style: AppTypography.headlineSm.copyWith(
                        color: scheme.onPrimary,
                      ),
                    ),
                  ],
                ),
              ),
              SizedBox(
                width: 48,
                height: 48,
                child: IconButton(
                  key: const Key('homeBell'),
                  onPressed: widget.onBell,
                  padding: EdgeInsets.zero,
                  style: IconButton.styleFrom(
                    minimumSize: const Size(48, 48),
                    tapTargetSize: MaterialTapTargetSize.padded,
                  ),
                  icon: Container(
                    width: 36,
                    height: 36,
                    decoration: const BoxDecoration(
                      shape: BoxShape.circle,
                      color: AppColors.primaryContainer,
                    ),
                    alignment: Alignment.center,
                    child: const Icon(
                      Icons.notifications_outlined,
                      size: 18,
                      color: AppColors.surfaceContainerLowest,
                    ),
                  ),
                  color: scheme.onPrimary,
                  tooltip: 'Notificaciones',
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

/// Hero eucalipto del `dash` (`0:3070`) con el SALDO TOTAL del servidor.
///
/// Muestra `primary_total_minor` de `GET /accounts/totals` formateado con
/// `formatMinor` y el `as_of` del servidor (`Actualizado hoy • HH:MM`).
/// **Jamas suma saldos en el cliente** (docs/19 §4/§8). Sin total disponible:
/// estado neutro sin inventar montos. El ojo (`heroEye`) solo oculta/muestra
/// el texto (estado local de UI). `Analizar` (`heroAnalyze`) es un marcador
/// deshabilitado: no es tappable, se muestra atenuado con badge/tooltip
/// "Próximamente" (consistente con los ítems deshabilitados del panel
/// `admin-web`) y no dispara SnackBar ni navegación.
class _TotalsHero extends StatefulWidget {
  const _TotalsHero({required this.totalsFuture});

  final Future<AccountsTotals> totalsFuture;

  @override
  State<_TotalsHero> createState() => _TotalsHeroState();
}

class _TotalsHeroState extends State<_TotalsHero> {
  var _obscured = false;

  /// `HH:MM` local desde el `as_of` ISO del servidor (presentacion).
  String _timeOf(String asOf) {
    final parsed = DateTime.tryParse(asOf)?.toLocal();
    if (parsed == null) return '';
    final hh = parsed.hour.toString().padLeft(2, '0');
    final mm = parsed.minute.toString().padLeft(2, '0');
    return '$hh:$mm';
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      key: const Key('homeHero'),
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: scheme.primary,
        borderRadius: BorderRadius.circular(AppRadii.xl),
        boxShadow: AppShadows.cardList,
      ),
      child: FutureBuilder<AccountsTotals>(
        future: widget.totalsFuture,
        builder: (context, snapshot) {
          final totals = snapshot.data;
          final waiting =
              snapshot.connectionState == ConnectionState.waiting;
          final currency = totals?.primaryCurrency ?? 'PEN';
          final symbol = currency == 'PEN' ? 'S/' : currency;
          final label = 'SALDO TOTAL • $currency ($symbol)';
          final time = totals == null ? '' : _timeOf(totals.asOf);
          final foot = time.isEmpty
              ? 'Actualizado hoy • Datos del servidor'
              : 'Actualizado hoy • $time';
          Widget amount;
          if (totals == null) {
            amount = Text(
              waiting ? 'Cargando total…' : 'Total no disponible por ahora.',
              style: AppTypography.bodyMd.copyWith(
                color: scheme.onPrimaryContainer,
              ),
            );
          } else if (_obscured) {
            amount = Text(
              '$symbol ••••••',
              style: AppTypography.displayLg.copyWith(
                color: scheme.onPrimary,
              ),
            );
          } else {
            // Monto TAL CUAL responde el servidor (sin sumar en cliente).
            amount = Text(
              formatMinor(
                totals.primaryTotalMinor,
                totals.primaryCurrency,
              ),
              style: AppTypography.displayLg.copyWith(
                color: scheme.onPrimary,
              ),
            );
          }
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      label,
                      style: AppTypography.labelSm.copyWith(
                        color: scheme.onPrimaryContainer,
                      ),
                    ),
                  ),
                  SizedBox(
                    width: 40,
                    height: 40,
                    child: IconButton(
                      key: const Key('heroEye'),
                      onPressed: totals == null
                          ? null
                          : () => setState(() => _obscured = !_obscured),
                      padding: EdgeInsets.zero,
                      icon: Icon(
                        _obscured
                            ? Icons.visibility_off_outlined
                            : Icons.visibility_outlined,
                        size: 18,
                        color: scheme.onPrimaryContainer,
                      ),
                      tooltip: _obscured ? 'Mostrar saldo' : 'Ocultar saldo',
                    ),
                  ),
                ],
              ),
              const SizedBox(height: AppSpacing.stackSm),
              amount,
              const SizedBox(height: AppSpacing.stackSm),
              Row(
                children: [
                  Expanded(
                    child: Text(
                      foot,
                      style: AppTypography.bodyMd.copyWith(
                        color: scheme.onPrimaryContainer,
                      ),
                    ),
                  ),
                  // Marcador deshabilitado (sin destino real de análisis):
                  // no tappable, atenuado + badge/tooltip "Próximamente",
                  // sin SnackBar ni navegación (no duplica `/movements`).
                  Tooltip(
                    message: 'Próximamente',
                    child: Opacity(
                      key: const Key('heroAnalyze'),
                      opacity: 0.6,
                      child: ExcludeSemantics(
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(
                              'Analizar',
                              style: AppTypography.labelMd.copyWith(
                                color: scheme.secondaryContainer,
                              ),
                            ),
                            const SizedBox(width: 6),
                            Container(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 6,
                                vertical: 2,
                              ),
                              decoration: BoxDecoration(
                                color: scheme.onPrimary.withValues(alpha: 0.2),
                                borderRadius: BorderRadius.circular(8),
                              ),
                              child: Text(
                                'Próximamente',
                                style: AppTypography.labelSm.copyWith(
                                  color: scheme.onPrimary,
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ],
          );
        },
      ),
    );
  }
}

/// Fila de accesos rapidos del `dash` (`0:3082`).
///
/// `Transferir` navega a `/transfers`, `Credito` a `/credits`,
/// `Cobrar QR` a `/wallet/merchant-qr` y `Recargar` a `/wallet/topups`
/// (C1: entradas reales, sin "Proximamente"). `Cobrar QR` va destacado
/// en ocre.
class _QuickActions extends StatelessWidget {
  const _QuickActions();

  static const _items = [
    (
      Icons.compare_arrows_outlined,
      'Transferir',
      false,
      'qa-transferir',
      '/transfers',
    ),
    (
      Icons.request_quote_outlined,
      'Crédito',
      false,
      'qa-creditos',
      '/credits',
    ),
    (
      Icons.qr_code_2_outlined,
      'Cobrar QR',
      true,
      'qa-qr',
      '/wallet/merchant-qr',
    ),
    (
      Icons.smartphone_outlined,
      'Recargar',
      false,
      'qa-recargar',
      '/wallet/topups',
    ),
  ];

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Row(
      children: [
        for (final item in _items)
          Expanded(
            child: _QuickAction(
              storageKey: item.$4,
              icon: item.$1,
              label: item.$2,
              highlighted: item.$3,
              iconColor:
                  item.$3 ? scheme.primary : AppColors.primaryContainer,
              tileColor: item.$3
                  ? scheme.secondaryContainer
                  : AppColors.surfaceContainerLowest,
              onTap: () => context.push(item.$5),
            ),
          ),
      ],
    );
  }
}

class _QuickAction extends StatelessWidget {
  const _QuickAction({
    required this.storageKey,
    required this.icon,
    required this.label,
    required this.highlighted,
    required this.iconColor,
    required this.tileColor,
    this.onTap,
  });

  final String storageKey;
  final IconData icon;
  final String label;
  final bool highlighted;
  final Color iconColor;
  final Color tileColor;

  /// Accion real de navegacion (las 4 quick-actions la definen; `null` =
  /// reserva defensiva con "Proximamente", sin ruta muerta de
  /// wallet/notificaciones).
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      key: Key(storageKey),
      onTap: onTap ??
          () {
            ScaffoldMessenger.of(context).showSnackBar(
              const SnackBar(
                content: Text('Próximamente'),
                duration: Duration(seconds: 2),
              ),
            );
          },
      borderRadius: BorderRadius.circular(AppRadii.md),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: AppSpacing.stackSm),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 56,
              height: 56,
              decoration: BoxDecoration(
                color: tileColor,
                borderRadius: BorderRadius.circular(AppRadii.md),
                boxShadow: AppShadows.cardList,
              ),
              alignment: Alignment.center,
              child: Icon(icon, size: 24, color: iconColor),
            ),
            const SizedBox(height: AppSpacing.stackSm),
            Text(
              label,
              style: (highlighted
                      ? AppTypography.labelMd
                      : AppTypography.labelSm)
                  .copyWith(color: AppColors.onSurface),
            ),
          ],
        ),
      ),
    );
  }
}

/// Acceso a la agenda de beneficiarios (fix alta múltiple Nivel 2).
///
/// Punto de entrada a `/beneficiaries` (la agenda ya tiene el botón "Nuevo"
/// siempre visible): card compacta que no altera el `dash` (`0:3057`) ni las
/// quick-actions existentes.
class _AgendaCard extends StatelessWidget {
  const _AgendaCard();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return AppCard(
      onTap: () => context.push('/beneficiaries'),
      child: Row(
        children: [
          Container(
            width: 44,
            height: 44,
            decoration: BoxDecoration(
              color: AppColors.successContainer,
              borderRadius: BorderRadius.circular(AppRadii.md),
            ),
            child: Icon(
              Icons.people_outline,
              size: 24,
              color: scheme.primary,
            ),
          ),
          const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'Mis beneficiarios',
                  key: const Key('home-beneficiaries'),
                  style: AppTypography.bodyLg.copyWith(
                    color: scheme.onSurface,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: AppSpacing.unit),
                Text(
                  'Agenda y contactos frecuentes',
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
              ],
            ),
          ),
          const Icon(
            Icons.chevron_right,
            color: AppColors.secondaryText,
          ),
        ],
      ),
    );
  }
}

/// Acceso a la billetera (C1).
///
/// Punto de entrada a `/wallet` (la billetera ya tiene sus flujos QR,
/// servicios y recargas): card compacta con `Key('home-wallet')` que no
/// altera el `dash` (`0:3057`) ni las quick-actions existentes.
class _WalletCard extends StatelessWidget {
  const _WalletCard();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return AppCard(
      onTap: () => context.push('/wallet'),
      child: Row(
        children: [
          Container(
            width: 44,
            height: 44,
            decoration: BoxDecoration(
              color: AppColors.secondaryContainer,
              borderRadius: BorderRadius.circular(AppRadii.md),
            ),
            child: Icon(
              Icons.account_balance_wallet_outlined,
              size: 24,
              color: scheme.primary,
            ),
          ),
          const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'Mi billetera',
                  key: const Key('home-wallet'),
                  style: AppTypography.bodyLg.copyWith(
                    color: scheme.onSurface,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: AppSpacing.unit),
                Text(
                  'Cobros QR, recargas y servicios',
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
              ],
            ),
          ),
          const Icon(
            Icons.chevron_right,
            color: AppColors.secondaryText,
          ),
        ],
      ),
    );
  }
}

/// Card `MIS CUENTAS` (`0:3108`, estilo Ahorros/Billetera del diseno).
class _AccountsCard extends StatelessWidget {
  const _AccountsCard({required this.accounts});

  final List<Account> accounts;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'MIS CUENTAS',
            style: AppTypography.labelSm.copyWith(
              fontWeight: FontWeight.w700,
              color: scheme.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          for (var i = 0; i < accounts.length; i++) ...[
            _AccountRow(account: accounts[i], tinted: i.isOdd),
            if (i < accounts.length - 1) const Divider(height: 1),
          ],
          const SizedBox(height: AppSpacing.stackSm),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              onPressed: () => context.push('/accounts'),
              child: const Text('Ver todas las cuentas'),
            ),
          ),
        ],
      ),
    );
  }
}

class _AccountRow extends StatelessWidget {
  const _AccountRow({required this.account, required this.tinted});

  final Account account;
  final bool tinted;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return InkWell(
      onTap: () => context.push('/accounts/${account.id}'),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: AppSpacing.stackSm + 4),
        child: Row(
          children: [
            Container(
              width: 44,
              height: 44,
              decoration: BoxDecoration(
                color: tinted
                    ? AppColors.warningContainer
                    : AppColors.successContainer,
                borderRadius: BorderRadius.circular(AppRadii.md),
              ),
              child: Icon(
                Icons.account_balance_outlined,
                size: 24,
                color: scheme.primary,
              ),
            ),
            const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    '${account.type} ${account.accountNumberMasked}',
                    style: AppTypography.bodyLg.copyWith(
                      color: scheme.onSurface,
                    ),
                  ),
                  const SizedBox(height: AppSpacing.unit),
                  Text(
                    '${formatMinor(account.availableMinor, account.currency)}'
                    ' disponible',
                    style: AppTypography.bodyMd.copyWith(
                      color: AppColors.secondaryText,
                    ),
                  ),
                ],
              ),
            ),
            Icon(
              Icons.chevron_right,
              color: AppColors.secondaryText,
            ),
          ],
        ),
      ),
    );
  }
}

/// Card `ULTIMOS MOVIMIENTOS` (`0:3130`, preview de la primera cuenta).
class _MovementsPreviewCard extends StatefulWidget {
  const _MovementsPreviewCard({
    super.key,
    required this.service,
    required this.account,
  });

  final AccountsServiceBase service;
  final Account account;

  @override
  State<_MovementsPreviewCard> createState() => _MovementsPreviewCardState();
}

class _MovementsPreviewCardState extends State<_MovementsPreviewCard> {
  final _latestKey = GlobalKey<_LatestMovementsState>();

  /// Recarga el preview (lo usan el *pull-to-refresh* y la vuelta a
  /// `/home` desde `HomePage`).
  Future<void> reload() => _latestKey.currentState?.reload() ?? Future.value();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'ÚLTIMOS MOVIMIENTOS',
            style: AppTypography.labelSm.copyWith(
              fontWeight: FontWeight.w700,
              color: scheme.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          _LatestMovements(
            key: _latestKey,
            service: widget.service,
            account: widget.account,
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              // Historial nuevo (E2-T24, Sprint 2): el detalle por cuenta
              // sigue vivo tocando cada fila (`_AccountRow` -> `/accounts/:id`).
              onPressed: () => context.push('/movements'),
              child: const Text('Ver todos los movimientos'),
            ),
          ),
        ],
      ),
    );
  }
}

/// Preview de movimientos con sus propios mini-estados (carga/error/vacio).
///
/// Filtra por `entry_type` (E2-T35, `movement_concept.dart`): las patas
/// internas (`TRANSFER_HOLD`/`INTERBANK_HOLD`) no son dinero del usuario y
/// se ocultan; cada fila visible muestra su etiqueta de negocio (nunca un
/// UUID). El signo (`+`/`-`) sale de `direction`; no se calcula nada.
class _LatestMovements extends StatefulWidget {
  const _LatestMovements({
    super.key,
    required this.service,
    required this.account,
  });

  final AccountsServiceBase service;
  final Account account;

  @override
  State<_LatestMovements> createState() => _LatestMovementsState();
}

class _LatestMovementsState extends State<_LatestMovements> {
  /// Movimientos visibles que muestra el preview (maximo 3).
  static const int _targetVisible = 3;

  /// Pagina amplia + hasta 5 paginas: si la primera trae solo patas
  /// internas, se sigue pidiendo hasta juntar 3 visibles o agotar `total`.
  static const int _previewPageSize = 100;
  static const int _previewMaxPages = 5;

  late Future<List<Movement>> _future;

  @override
  void initState() {
    super.initState();
    _future = _loadVisible();
  }

  /// Recarga el preview (retorna cuando termina, con error tragado: el
  /// `FutureBuilder` muestra el estado).
  Future<void> reload() async {
    final future = _loadVisible();
    future.ignore();
    if (!mounted) return;
    setState(() {
      _future = future;
    });
    try {
      await future;
    } catch (_) {
      // El FutureBuilder muestra el error con su reintento.
    }
  }

  Future<List<Movement>> _loadVisible() async {
    final visible = <Movement>[];
    var page = 1;
    var total = 1 << 30;
    var fetched = 0;
    while (visible.length < _targetVisible &&
        page <= _previewMaxPages &&
        fetched < total) {
      final result = await widget.service.getMovements(
        widget.account.id,
        page: page,
        pageSize: _previewPageSize,
      );
      total = result.total;
      fetched += result.items.length;
      for (final movement in result.items) {
        if (!isInternalMovement(
          entryType: movement.entryType,
          description: movement.description,
        )) {
          visible.add(movement);
          if (visible.length >= _targetVisible) break;
        }
      }
      if (result.items.isEmpty) break;
      page++;
    }
    return visible.take(_targetVisible).toList();
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<List<Movement>>(
      future: _future,
      builder: (context, snapshot) {
        if (snapshot.connectionState == ConnectionState.waiting) {
          return const Padding(
            padding: EdgeInsets.symmetric(vertical: AppSpacing.stackMd),
            child: Row(
              children: [
                SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
                SizedBox(width: AppSpacing.stackSm),
                Text('Cargando movimientos...'),
              ],
            ),
          );
        }
        if (snapshot.hasError) {
          final error = snapshot.error;
          final message = error is ApiException
              ? error.message
              : 'No se pudieron cargar los movimientos.';
          return Row(
            children: [
              Expanded(child: Text(message)),
              TextButton(
                onPressed: () {
                  reload();
                },
                child: const Text('Reintentar'),
              ),
            ],
          );
        }
        final visible = snapshot.data ?? const <Movement>[];
        if (visible.isEmpty) {
          return Text(
            'Aún no tienes movimientos.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          );
        }
        return Column(
          children: [
            for (final movement in visible)
              _MovementRow(
                movement: movement,
                accountMasked: widget.account.accountNumberMasked,
              ),
          ],
        );
      },
    );
  }
}

class _MovementRow extends StatelessWidget {
  const _MovementRow({required this.movement, required this.accountMasked});

  final Movement movement;
  final String accountMasked;

  @override
  Widget build(BuildContext context) {
    // DECISION m4 (auditoria, mismo criterio que m3): la fila NO navega al
    // detalle. El preview usa el `Movement` de cuentas (`journalEntryId`
    // del asiento) y `/movements/:id` solo resuelve `transaction_id`
    // (`GET /transactions/{id}`): sin mapeo en el cliente, navegar caeria
    // en `NOT_FOUND`. La via real al historial es "Ver todos los
    // movimientos" (`/movements`); no se inventa un id.
    final scheme = Theme.of(context).colorScheme;
    final credit = movement.isCredit;
    final color = credit ? AppColors.success : scheme.error;
    final signed = '${credit ? '+' : '-'}'
        '${formatMinor(movement.amountMinor, movement.currency)}';
    // Etiqueta de negocio por `entry_type` (E2-T35): si llega texto tecnico
    // o un UUID, el helper lo sustituye por un titulo neutro de usuario.
    final title = conceptForMovement(
      entryType: movement.entryType,
      description: movement.description,
    ).label;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: AppSpacing.stackSm),
      child: Row(
        children: [
          Icon(
            credit ? Icons.arrow_downward : Icons.arrow_upward,
            size: 24,
            color: color,
          ),
          const SizedBox(width: AppSpacing.stackSm),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  title,
                  style: AppTypography.bodyLg.copyWith(
                    color: scheme.onSurface,
                  ),
                ),
                const SizedBox(height: AppSpacing.unit),
                Text(
                  '$accountMasked • '
                  '${formatValueDate(movement.valueDate)} • '
                  '${credit ? 'Abono' : 'Cargo'}',
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(width: AppSpacing.stackSm),
          Text(
            signed,
            style: AppTypography.labelMd.copyWith(color: color),
          ),
        ],
      ),
    );
  }
}

/// Banner informativo `Token Digital activo` (`0:3160`, visual).
class _SecurityBanner extends StatelessWidget {
  const _SecurityBanner();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      key: const Key('homeSecurityBanner'),
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.stackMd),
      decoration: BoxDecoration(
        color: AppColors.primaryFixed,
        borderRadius: BorderRadius.circular(AppRadii.md),
      ),
      child: Row(
        children: [
          Container(
            width: 40,
            height: 40,
            decoration: BoxDecoration(
              color: scheme.primary,
              borderRadius: BorderRadius.circular(AppRadii.md),
            ),
            alignment: Alignment.center,
            child: const Icon(
              Icons.shield_outlined,
              size: 20,
              color: AppColors.surfaceContainerLowest,
            ),
          ),
          const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'Token Digital activo',
                  style: AppTypography.labelMd.copyWith(
                    color: AppColors.onPrimaryFixed,
                  ),
                ),
                const SizedBox(height: AppSpacing.unit),
                Text(
                  'Tus compras se validan con tu Clave Digital',
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.onPrimaryFixedVariant,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
