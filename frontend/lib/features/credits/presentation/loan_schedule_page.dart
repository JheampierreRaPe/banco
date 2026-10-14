import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/loan_schedule_service.dart';
import '../models/loan_schedule.dart';
import '../utils/format.dart';

/// Pantalla del cronograma definitivo + débito automático (E3-T21, HU12;
/// spec `docs/design/creditos-pantallas.md` §8).
///
/// - Hero con el saldo de capital y el estado del préstamo (del servidor).
/// - Toggle de **débito automático** (`PUT .../auto-debit`): refleja y
///   persiste `auto_debit_enabled` del servidor (sin `Idempotency-Key`).
/// - CTA **Pagar próxima cuota** (`POST .../pay` con
///   `ApiClient.postWithIdempotency`): el servidor cobra el `total_minor` de
///   la cuota exigible (MVP por cuota total; las filas del cronograma no
///   exponen `schedule_id`, así que no se paga por fila).
/// - Filas por cuota (N, vencimiento, capital/interés/seguro/total, estado
///   `Pendiente`/`Parcial`/`Pagada`/`Vencida` TAL CUAL).
/// - 4 estados (`docs/20` §7): contenido, carga (skeleton), vacío (sin
///   cuotas) y error accionable con Reintentar + pull-to-refresh.
///
/// Cliente delgado (`docs/19` §4): no calcula cuotas/saldos/intereses; todo
/// viene del backend. Sin PII ni claves en logs.
///
/// [service] y [loanId] son inyectables para tests; la ruta los resuelve
/// desde `loanScheduleServiceFactory` y el path param. `null`/vacío degrada
/// a error accionable.
class LoanSchedulePage extends StatefulWidget {
  const LoanSchedulePage({super.key, this.service, this.loanId});

  final LoanScheduleServiceBase? service;
  final String? loanId;

  @override
  State<LoanSchedulePage> createState() => _LoanSchedulePageState();
}

class _LoanSchedulePageState extends State<LoanSchedulePage> {
  LoanSchedule? _schedule;
  ApiException? _error;
  bool _loading = false;
  bool _togglingDebit = false;
  bool _paying = false;
  String? _payNotice;

  String get _loanId => (widget.loanId ?? '').trim();

  @override
  void initState() {
    super.initState();
    if (_loanId.isEmpty) {
      _error = ApiException(
        code: 'UNKNOWN',
        message: 'Sin préstamo para mostrar. '
            'Vuelve e inicia el flujo de nuevo.',
      );
    } else {
      _fetch();
    }
  }

  Future<void> _fetch() async {
    final service = widget.service;
    final id = _loanId;
    if (id.isEmpty) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    if (service == null) {
      setState(() {
        _loading = false;
        _error = ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de créditos no configurado.',
        );
      });
      return;
    }
    try {
      final schedule = await service.getSchedule(id);
      if (!mounted) return;
      setState(() {
        _schedule = schedule;
        _error = null;
        _loading = false;
      });
    } on ApiException catch (error) {
      if (!mounted) return;
      setState(() {
        _error = error;
        _loading = false;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _error = ApiException(
          code: 'UNKNOWN',
          message: 'Ocurrió un error inesperado. Inténtalo más tarde.',
        );
        _loading = false;
      });
    }
  }

  Future<void> _toggleAutoDebit(bool enabled) async {
    final service = widget.service;
    if (service == null || _togglingDebit) return;
    setState(() {
      _togglingDebit = true;
      _payNotice = null;
    });
    try {
      final confirmed = await service.setAutoDebit(_loanId, enabled: enabled);
      if (!mounted) return;
      setState(() {
        _togglingDebit = false;
        final current = _schedule;
        if (current != null) {
          _schedule = LoanSchedule(
            loan: Loan(
              id: current.loan.id,
              applicationId: current.loan.applicationId,
              principalMinor: current.loan.principalMinor,
              outstandingPrincipalMinor:
                  current.loan.outstandingPrincipalMinor,
              termMonths: current.loan.termMonths,
              status: current.loan.status,
              autoDebitEnabled: confirmed,
              tcea: current.loan.tcea,
              disbursedAt: current.loan.disbursedAt,
              createdAt: current.loan.createdAt,
            ),
            installments: current.installments,
          );
        }
      });
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            confirmed
                ? 'Débito automático activado.'
                : 'Débito automático desactivado.',
          ),
        ),
      );
    } on ApiException catch (error) {
      if (!mounted) return;
      setState(() {
        _togglingDebit = false;
      });
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(loanScheduleUserMessage(error))),
      );
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _togglingDebit = false;
      });
    }
  }

  Future<void> _payNext() async {
    final service = widget.service;
    if (service == null || _paying) return;
    setState(() {
      _paying = true;
      _payNotice = null;
    });
    // Clave del intento en curso: el reintento del MISMO intento la
    // reutiliza (patrón `postWithIdempotency`).
    final attemptKey = const Uuid().v4();
    try {
      final result = await service.payNextInstallment(
        _loanId,
        idempotencyKey: attemptKey,
      );
      if (!mounted) return;
      if (result.isSettled) {
        setState(() {
          _paying = false;
          _payNotice = 'Cuota pagada. Actualizando el cronograma…';
        });
        await _fetch();
        return;
      }
      setState(() {
        _paying = false;
        _payNotice = result.isInsufficientFunds
            ? loanScheduleUserMessage(
                ApiException(
                  code: 'INSUFFICIENT_FUNDS',
                  message: 'Saldo insuficiente para pagar la cuota.',
                ),
              )
            : 'No pudimos cobrar la cuota. Inténtalo de nuevo.';
      });
    } on ApiException catch (error) {
      if (!mounted) return;
      setState(() {
        _paying = false;
        _payNotice = loanScheduleUserMessage(error);
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _paying = false;
        _payNotice = 'Ocurrió un error inesperado. Inténtalo más tarde.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Cronograma del préstamo')),
      body: _buildBody(),
    );
  }

  Widget _buildBody() {
    final schedule = _schedule;
    final error = _error;
    if (_loading && schedule == null) {
      return const LoadingView(
        key: Key('loan-schedule-loading'),
        message: 'Cargando el cronograma definitivo…',
      );
    }
    if (error != null && schedule == null) {
      return ErrorView(
        key: const Key('loan-schedule-error'),
        message: loanScheduleUserMessage(error),
        onRetry: _fetch,
      );
    }
    if (schedule == null) {
      return EmptyView(
        key: const Key('loan-schedule-empty'),
        message: 'Sin préstamo para mostrar. '
            'Vuelve e inicia el flujo de nuevo.',
        actionLabel: 'Volver',
        onAction: () => Navigator.of(context).maybePop(),
      );
    }
    if (schedule.installments.isEmpty) {
      return EmptyView(
        key: const Key('loan-schedule-empty-rows'),
        message: 'Este préstamo aún no tiene cuotas programadas. '
            'Desliza para actualizar.',
        actionLabel: 'Reintentar',
        onAction: _fetch,
      );
    }
    return RefreshIndicator(
      onRefresh: _fetch,
      child: ListView(
        key: const Key('loan-schedule-content'),
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          if (_loading) const LinearProgressIndicator(),
          _hero(schedule),
          const SizedBox(height: AppSpacing.stackMd),
          _autoDebitCard(schedule),
          const SizedBox(height: AppSpacing.stackMd),
          if (schedule.hasPayable) ...[
            SizedBox(
              width: double.infinity,
              child: FilledButton(
                key: const Key('loan-schedule-pay-cta'),
                onPressed: _paying ? null : _payNext,
                style: FilledButton.styleFrom(
                  minimumSize: const Size(88, 52),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                ),
                child: Text(
                  _paying ? 'Pagando…' : 'Pagar próxima cuota',
                ),
              ),
            ),
            const SizedBox(height: AppSpacing.stackSm),
          ],
          if (_payNotice != null)
            Container(
              key: const Key('loan-schedule-pay-notice'),
              width: double.infinity,
              padding: const EdgeInsets.all(AppSpacing.containerPadding),
              decoration: BoxDecoration(
                color: AppColors.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(AppRadii.lg),
              ),
              child: Text(
                _payNotice!,
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.onSurface,
                ),
              ),
            ),
          if (_payNotice != null) const SizedBox(height: AppSpacing.stackSm),
          _installmentsCard(schedule),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Montos y fechas del servidor. El cliente no calcula.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
        ],
      ),
    );
  }

  Widget _hero(LoanSchedule schedule) {
    final loan = schedule.loan;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLow,
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'CRONOGRAMA DEFINITIVO (servidor)',
            style: AppTypography.labelSm.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.secondaryText,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            key: const Key('loan-schedule-balance'),
            'Saldo de capital: '
            '${formatMinor(loan.outstandingPrincipalMinor, 'PEN')}',
            style: AppTypography.titleMd.copyWith(
              color: AppColors.primary,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            '${formatMinor(loan.principalMinor, 'PEN')} • '
            '${loan.termMonths} cuotas • ${loan.statusLabel}',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSurface,
            ),
          ),
        ],
      ),
    );
  }

  Widget _autoDebitCard(LoanSchedule schedule) {
    final enabled = schedule.loan.autoDebitEnabled;
    return AppCard(
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Débito automático',
                  style: AppTypography.labelMd.copyWith(
                    fontWeight: FontWeight.w700,
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  enabled
                      ? 'Tus cuotas se cobran solas en su vencimiento.'
                      : 'Actívalo para no olvidar tus vencimientos.',
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
              ],
            ),
          ),
          Switch(
            key: const Key('loan-schedule-auto-debit'),
            value: enabled,
            onChanged: _togglingDebit ? null : _toggleAutoDebit,
          ),
        ],
      ),
    );
  }

  Widget _installmentsCard(LoanSchedule schedule) {
    return AppCard(
      padding: EdgeInsets.zero,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
            child: Text(
              'Cuotas (${schedule.installments.length})',
              style: AppTypography.labelMd.copyWith(
                fontWeight: FontWeight.w700,
                color: AppColors.primary,
              ),
            ),
          ),
          for (var i = 0; i < schedule.installments.length; i++) ...[
            if (i > 0)
              const Divider(height: 1, color: AppColors.divider),
            _installmentRow(schedule.installments[i]),
          ],
        ],
      ),
    );
  }

  Widget _installmentRow(LoanInstallment row) {
    return Padding(
      key: Key('loan-installment-${row.installmentNo}'),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  'Cuota ${row.installmentNo} • Vence ${row.dueDate}',
                  style: AppTypography.labelMd.copyWith(
                    fontWeight: FontWeight.w700,
                    color: AppColors.onSurface,
                  ),
                ),
              ),
              _installmentBadge(row.status),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            'Total: ${formatMinor(row.totalMinor, 'PEN')}',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSurface,
              fontWeight: FontWeight.w600,
            ),
          ),
          Text(
            'Capital ${formatMinor(row.principalMinor, 'PEN')} • '
            'Interés ${formatMinor(row.interestMinor, 'PEN')} • '
            'Seguro ${formatMinor(row.insuranceMinor, 'PEN')}',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
          if (row.status == LoanInstallmentStatus.paid &&
              (row.paidAt ?? '').isNotEmpty)
            Text(
              'Pagada el ${row.paidAt}',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
            ),
        ],
      ),
    );
  }

  Widget _installmentBadge(LoanInstallmentStatus status) {
    final (background, foreground) = switch (status) {
      LoanInstallmentStatus.paid => (
          AppColors.successContainer,
          AppColors.onSuccessContainer,
        ),
      LoanInstallmentStatus.overdue => (
          AppColors.errorContainer,
          AppColors.onErrorContainer,
        ),
      LoanInstallmentStatus.partial => (
          AppColors.warningContainer,
          AppColors.onWarningContainer,
        ),
      LoanInstallmentStatus.pending => (
          AppColors.surfaceContainerHighest,
          AppColors.onSurfaceVariant,
        ),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        status.label,
        style: AppTypography.labelMd.copyWith(
          color: foreground,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}
