import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/loan_schedule_service.dart';
import '../models/loan_schedule.dart';
import '../utils/format.dart';

/// Pantalla "Mis préstamos" (E3-T21, HU12; spec
/// `docs/design/creditos-pantallas.md` §8).
///
/// - Lista los préstamos del titular (`GET /credits/loans`): monto
///   desembolsado, saldo de capital, plazo, estado y débito automático.
/// - Cada fila navega al cronograma (`/credits/loans/{id}`).
/// - 4 estados (`docs/20` §7): contenido, carga (skeleton), vacío (sin
///   préstamos) y error accionable con Reintentar + pull-to-refresh.
///
/// Cliente delgado (`docs/19` §4): muestra los montos y estados TAL CUAL
/// responde el servidor (sin calcular ni reinterpretar).
///
/// [service] es inyectable para tests; la ruta lo resuelve desde
/// `loanScheduleServiceFactory`. `null` degrada a error accionable.
class LoanListPage extends StatefulWidget {
  const LoanListPage({super.key, this.service});

  final LoanScheduleServiceBase? service;

  @override
  State<LoanListPage> createState() => _LoanListPageState();
}

class _LoanListPageState extends State<LoanListPage> {
  late Future<List<Loan>> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
    _future.ignore();
  }

  Future<List<Loan>> _load() {
    final service = widget.service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de créditos no configurado.',
        ),
      );
    }
    return service.listLoans();
  }

  Future<void> _refresh() async {
    final future = _load();
    future.ignore();
    setState(() {
      _future = future;
    });
    await future.then<void>((_) {}, onError: (_) {});
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Mis préstamos')),
      body: FutureBuilder<List<Loan>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const LoadingView(
              key: Key('loan-list-loading'),
              message: 'Cargando tus préstamos…',
            );
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            final message = error is ApiException
                ? loanScheduleUserMessage(error)
                : 'Ocurrió un error inesperado. Inténtalo más tarde.';
            return ErrorView(
              key: const Key('loan-list-error'),
              message: message,
              onRetry: _refresh,
            );
          }
          final loans = snapshot.data ?? const [];
          if (loans.isEmpty) {
            return EmptyView(
              key: const Key('loan-list-empty'),
              message: 'Aún no tienes préstamos desembolsados. '
                  'Cuando tu crédito se desembolse lo verás aquí.',
              actionLabel: 'Ir al simulador',
              onAction: () => context.go('/credits'),
            );
          }
          return RefreshIndicator(
            onRefresh: _refresh,
            child: ListView.separated(
              key: const Key('loan-list-content'),
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
              itemCount: loans.length + 1,
              separatorBuilder: (_, i) =>
                  i < loans.length - 1
                      ? const SizedBox(height: AppSpacing.stackMd)
                      : const SizedBox.shrink(),
              itemBuilder: (context, i) {
                if (i >= loans.length) {
                  return Padding(
                    padding: const EdgeInsets.only(top: AppSpacing.stackMd),
                    child: Text(
                      'Montos del servidor. El cliente no calcula.',
                      style: AppTypography.bodyMd.copyWith(
                        color: AppColors.secondaryText,
                      ),
                    ),
                  );
                }
                return _loanCard(loans[i]);
              },
            ),
          );
        },
      ),
    );
  }

  Widget _loanCard(Loan loan) {
    final shortId = loan.id.length > 8
        ? loan.id.substring(0, 8)
        : loan.id;
    return Card(
      key: Key('loan-row-${loan.id}'),
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadii.lg),
        side: BorderSide(
          color: loan.status.toUpperCase() == 'ACTIVE'
              ? AppColors.primary
              : AppColors.outlineVariant,
          width: loan.status.toUpperCase() == 'ACTIVE' ? 1.5 : 1,
        ),
      ),
      child: Padding(
        padding: const EdgeInsets.all(AppSpacing.containerPadding),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    'Préstamo • ${formatMinor(loan.principalMinor, 'PEN')} '
                    '• ${loan.termMonths} m',
                    style: AppTypography.labelMd.copyWith(
                      fontWeight: FontWeight.w700,
                      color: AppColors.primary,
                    ),
                  ),
                ),
                _statusBadge(loan.statusLabel),
              ],
            ),
            const SizedBox(height: AppSpacing.stackSm),
            Text(
              'PRÉSTAMO $shortId',
              style: AppTypography.labelSm.copyWith(
                color: AppColors.secondaryText,
              ),
            ),
            const SizedBox(height: AppSpacing.stackSm),
            Text(
              'Saldo de capital: '
              '${formatMinor(loan.outstandingPrincipalMinor, 'PEN')}',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.onSurface,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 4),
            Row(
              children: [
                Icon(
                  loan.autoDebitEnabled
                      ? Icons.autorenew
                      : Icons.autorenew_outlined,
                  size: 16,
                  color: AppColors.secondaryText,
                ),
                const SizedBox(width: 4),
                Text(
                  loan.autoDebitEnabled
                      ? 'Débito automático activado'
                      : 'Débito automático desactivado',
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
              ],
            ),
            const SizedBox(height: AppSpacing.stackSm),
            SizedBox(
              width: double.infinity,
              child: OutlinedButton(
                key: Key('loan-schedule-${loan.id}'),
                onPressed: () =>
                    context.push('/credits/loans/${loan.id}'),
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size(88, 52),
                ),
                child: const Text('Ver cronograma ›'),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _statusBadge(String label) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: AppColors.successContainer,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        style: AppTypography.labelMd.copyWith(
          color: AppColors.onSuccessContainer,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}
