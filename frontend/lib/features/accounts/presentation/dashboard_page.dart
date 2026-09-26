import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/accounts_service.dart';
import '../models/account.dart';
import '../utils/format.dart';

/// Dashboard de cuentas (HU05 CA-01/CA-02, docs/13 §1.3 `Inicio`).
///
/// - Lista el consolidado (`GET /accounts`) con pull-to-refresh.
/// - El numero se muestra enmascarado TAL COMO viene del backend
///   (`account_number_masked`); el cliente nunca desenmascara (docs/16 §2.9).
/// - Disponible (success) / retenido (warning) / contable (primary)
///   diferenciados con los tokens de `docs/20` §3.4 (F-T34); cards `AppCard`
///   (`docs/20` §6).
/// - Estados `LoadingView` / `EmptyView` / `ErrorView` de `core/widgets`.
///
/// [service] es inyectable para tests. Las rutas lo resuelven desde
/// `accountsServiceFactory` (cableada por el orquestador); `null` muestra un
/// error accionable en vez de romper.
class DashboardPage extends StatefulWidget {
  const DashboardPage({super.key, this.service});

  final AccountsServiceBase? service;

  @override
  State<DashboardPage> createState() => _DashboardPageState();
}

class _DashboardPageState extends State<DashboardPage> {
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
      appBar: AppBar(title: const Text('Mis cuentas')),
      body: FutureBuilder<List<Account>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const LoadingView(message: 'Cargando cuentas...');
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            final message = error is ApiException
                ? error.message
                : 'Ocurrio un error inesperado. Intentalo mas tarde.';
            return ErrorView(
              message: message,
              onRetry: _retry,
            );
          }
          final accounts = snapshot.data ?? const <Account>[];
          if (accounts.isEmpty) {
            return RefreshIndicator(
              onRefresh: _refresh,
              child: ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                children: const [
                  SizedBox(height: 120),
                  EmptyView(message: 'Aun no tienes cuentas.'),
                ],
              ),
            );
          }
          return RefreshIndicator(
            onRefresh: _refresh,
            child: ListView.separated(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.all(16),
              itemCount: accounts.length,
              separatorBuilder: (_, _) => const SizedBox(height: 12),
              itemBuilder: (context, index) {
                final account = accounts[index];
                return _AccountCard(
                  account: account,
                  onTap: () => context.push('/accounts/${account.id}'),
                );
              },
            ),
          );
        },
      ),
    );
  }
}

class _AccountCard extends StatelessWidget {
  const _AccountCard({required this.account, required this.onTap});

  final Account account;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return AppCard(
      onTap: onTap,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      '${account.type} ${account.accountNumberMasked}',
                      style: theme.textTheme.titleMedium,
                    ),
                  ),
                  const Icon(Icons.chevron_right),
                ],
              ),
              const SizedBox(height: 4),
              Text(
                account.currency,
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 12),
              _BalanceRow(
                label: 'Disponible',
                value: formatMinor(account.availableMinor, account.currency),
                color: AppColors.success,
              ),
              const SizedBox(height: 4),
              _BalanceRow(
                label: 'Retenido',
                value: formatMinor(account.heldMinor, account.currency),
                color: AppColors.warning,
              ),
              const Divider(height: 16),
              _BalanceRow(
                label: 'Contable',
                value: formatMinor(account.balanceMinor, account.currency),
                color: theme.colorScheme.primary,
                bold: true,
              ),
            ],
          ),
    );
  }
}

class _BalanceRow extends StatelessWidget {
  const _BalanceRow({
    required this.label,
    required this.value,
    required this.color,
    this.bold = false,
  });

  final String label;
  final String value;
  final Color color;
  final bool bold;

  @override
  Widget build(BuildContext context) {
    final style = TextStyle(
      color: color,
      fontWeight: bold ? FontWeight.bold : FontWeight.normal,
      fontSize: bold ? 16 : 14,
    );
    return Row(
      children: [
        Expanded(child: Text(label)),
        Text(value, style: style),
      ],
    );
  }
}
