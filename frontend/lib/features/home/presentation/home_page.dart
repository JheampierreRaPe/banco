import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_list_item.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../../accounts/data/accounts_service.dart';
import '../../accounts/models/account.dart';
import '../../accounts/utils/format.dart';

/// Pantalla `home` real (F-T41, HU05; diseno F-T42 `home-dashboard.png`).
///
/// - Cabecera fija `primary` con saludo generico (sin PII: el nombre no viene
///   de ningun endpoint del alcance, `docs/05` §6.2).
/// - Hero `secondary-container` con el consolidado (N.° de cuentas).
///   **No muestra "saldo total"**: sumarlo en el cliente violaria la regla de
///   cliente delgado (`docs/19` §4/§8: el cliente no calcula saldos; muestra
///   lo que responde el servidor). Cada saldo se muestra tal cual llega.
/// - `Mis cuentas` (de `GET /accounts`, pull-to-refresh) + `Últimos
///   movimientos` de la primera cuenta (`GET /accounts/{id}/movements`,
///   pagina 1, 3 items, solo lectura).
/// - Numero enmascarado TAL COMO viene (`account_number_masked`); el cliente
///   nunca desenmascara (`docs/20` §8).
/// - 4 estados (`docs/20` §7) con `LoadingView`/`EmptyView`/`ErrorView` y
///   reintento accionable. Sin bottom nav: Enviar/QR/Mas quedan fuera del
///   bloque identidad/onboarding (SCR-005 d4).
///
/// [service] es inyectable para tests. Las rutas lo resuelven desde
/// `accountsServiceFactory` (cableada por el orquestador); `null` muestra un
/// error accionable en vez de romper.
class HomePage extends StatefulWidget {
  const HomePage({super.key, this.service});

  final AccountsServiceBase? service;

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  late Future<List<Account>> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<List<Account>> _load() {
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

  Future<void> _refresh() async {
    final future = _load();
    setState(() {
      _future = future;
    });
    try {
      await future;
    } catch (_) {
      // El FutureBuilder muestra el error; el refresh solo reintenta.
    }
  }

  void _retry() {
    setState(() {
      _future = _load();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Column(
          children: [
            const _HomeHeader(),
            Expanded(
              child: FutureBuilder<List<Account>>(
                future: _future,
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
                        : 'Ocurrio un error inesperado. Intentalo mas tarde.';
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
                            message: 'Aun no tienes cuentas.',
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
                        _SummaryHero(count: accounts.length),
                        const SizedBox(height: AppSpacing.stackMd),
                        _AccountsCard(accounts: accounts),
                        const SizedBox(height: AppSpacing.stackMd),
                        _MovementsPreviewCard(
                          service: widget.service!,
                          account: accounts.first,
                        ),
                      ],
                    ),
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Franja superior fija `primary` con saludo generico (sin PII).
class _HomeHeader extends StatelessWidget {
  const _HomeHeader();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      width: double.infinity,
      color: AppColors.primary,
      padding: const EdgeInsets.symmetric(
        horizontal: AppSpacing.containerPadding,
        vertical: AppSpacing.stackMd,
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'Hola',
                  style: AppTypography.headlineSm.copyWith(
                    color: scheme.onPrimary,
                  ),
                ),
                const SizedBox(height: AppSpacing.unit),
                Text(
                  'Bienvenido a tu banca',
                  style: AppTypography.bodyMd.copyWith(
                    color: scheme.onPrimaryContainer,
                  ),
                ),
              ],
            ),
          ),
          Container(
            width: AppListItem.leadingCircleDiameter,
            height: AppListItem.leadingCircleDiameter,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              border: Border.all(color: scheme.onPrimary, width: 1.5),
            ),
            child: Icon(
              Icons.person_outline,
              size: 24,
              color: scheme.onPrimary,
            ),
          ),
        ],
      ),
    );
  }
}

/// Hero ocre con el consolidado (conteo, sin sumar saldos: docs/19 §8).
class _SummaryHero extends StatelessWidget {
  const _SummaryHero({required this.count});

  final int count;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      key: const Key('homeSummary'),
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: scheme.secondaryContainer,
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'RESUMEN',
            style: AppTypography.labelMd.copyWith(
              color: scheme.onSecondaryContainer,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            count == 1 ? '1 cuenta' : '$count cuentas',
            style: AppTypography.headlineLgMobile.copyWith(
              color: scheme.onSecondaryContainer,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Actualizado hoy • Datos del servidor',
            style: AppTypography.bodyMd.copyWith(
              color: scheme.onSecondaryContainer,
            ),
          ),
        ],
      ),
    );
  }
}

/// Card `Mis cuentas` (mockup `home-dashboard.png`).
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
            'Mis cuentas',
            style: AppTypography.titleMd.copyWith(color: scheme.primary),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          for (var i = 0; i < accounts.length; i++) ...[
            _AccountRow(account: accounts[i]),
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
  const _AccountRow({required this.account});

  final Account account;

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
              width: AppListItem.leadingCircleDiameter,
              height: AppListItem.leadingCircleDiameter,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: scheme.primary.withValues(alpha: 0.05),
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

/// Card `Últimos movimientos` (preview de la primera cuenta).
class _MovementsPreviewCard extends StatelessWidget {
  const _MovementsPreviewCard({
    required this.service,
    required this.account,
  });

  final AccountsServiceBase service;
  final Account account;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Últimos movimientos',
            style: AppTypography.titleMd.copyWith(color: scheme.primary),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          _LatestMovements(service: service, account: account),
          const SizedBox(height: AppSpacing.stackSm),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              onPressed: () => context.push('/accounts/${account.id}'),
              child: const Text('Ver todos los movimientos'),
            ),
          ),
        ],
      ),
    );
  }
}

/// Preview de movimientos con sus propios mini-estados (carga/error/vacio).
class _LatestMovements extends StatefulWidget {
  const _LatestMovements({required this.service, required this.account});

  final AccountsServiceBase service;
  final Account account;

  @override
  State<_LatestMovements> createState() => _LatestMovementsState();
}

class _LatestMovementsState extends State<_LatestMovements> {
  late Future<MovementsPage> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<MovementsPage> _load() => widget.service.getMovements(
        widget.account.id,
        page: 1,
        pageSize: 3,
      );

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<MovementsPage>(
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
                  setState(() {
                    _future = _load();
                  });
                },
                child: const Text('Reintentar'),
              ),
            ],
          );
        }
        final items = snapshot.data?.items ?? const <Movement>[];
        if (items.isEmpty) {
          return Text(
            'Aun no tienes movimientos.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          );
        }
        return Column(
          children: [
            for (final movement in items)
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
    final scheme = Theme.of(context).colorScheme;
    final credit = movement.isCredit;
    final color = credit ? AppColors.success : scheme.error;
    final signed = '${credit ? '+' : '-'}'
        '${formatMinor(movement.amountMinor, movement.currency)}';
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
                  movement.description.isEmpty
                      ? '(Sin descripcion)'
                      : movement.description,
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
