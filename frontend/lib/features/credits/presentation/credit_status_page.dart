import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/credit_applications_service.dart';
import '../models/loan_application.dart';
import '../utils/format.dart';

/// Pantalla de estado del expediente (E3-T09, HU10; spec
/// `docs/design/creditos-pantallas.md` §3, mockup `creditos-estado.png`).
///
/// - Fila expediente + badge por estado (`Evaluando` / `En revisión` /
///   `Aprobado` / `Rechazado` / `Vencido`).
/// - Hero con el dictamen TAL CUAL responde el servidor (sin interpretar ni
///   recalcular) + card Motivo con el `decision_reason` exacto.
/// - Card Progreso (`Solicitud enviada → Evaluación automática → Decisión
///   lista → Firma y desembolso`) + CTAs por estado + pull-to-refresh.
/// - `BUREAU_UNAVAILABLE` se muestra como `En revisión` (no es error).
/// - 4 estados (`docs/20` §7) con `LoadingView`/`EmptyView`/`ErrorView`.
///
/// La pagina recibe el [initialResult] del envio (sin fetch inicial) o el
/// [applicationId] para consultar `GET /credits/applications/{id}`. Sin
/// ninguno (navegacion directa) muestra el estado vacio.
///
/// [service] es inyectable para tests; la ruta lo resuelve desde
/// `creditApplicationsServiceFactory`.
class CreditStatusPage extends StatefulWidget {
  const CreditStatusPage({
    super.key,
    this.service,
    this.applicationId,
    this.initialResult,
  });

  final CreditApplicationsServiceBase? service;
  final String? applicationId;
  final LoanApplicationResult? initialResult;

  @override
  State<CreditStatusPage> createState() => _CreditStatusPageState();
}

class _CreditStatusPageState extends State<CreditStatusPage> {
  LoanApplicationResult? _result;
  ApiException? _error;
  bool _loading = false;

  String get _applicationId => widget.initialResult?.id.isNotEmpty ?? false
      ? widget.initialResult!.id
      : (widget.applicationId ?? '');

  bool get _canFetch =>
      _applicationId.isNotEmpty && widget.initialResult == null;

  @override
  void initState() {
    super.initState();
    _result = widget.initialResult;
    if (_canFetch) {
      _fetch();
    } else if (_result == null && _applicationId.isEmpty) {
      // Sin expediente (navegacion directa): estado vacio, sin fetch.
    } else if (_result == null) {
      _error = ApiException(
        code: 'UNKNOWN',
        message: 'Servicio de créditos no configurado.',
      );
    }
  }

  Future<void> _fetch() async {
    final service = widget.service;
    final id = _applicationId;
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
      final result = await service.getApplication(id);
      if (!mounted) return;
      setState(() {
        _result = result;
        _error = null;
        _loading = false;
      });
    } on ApiException catch (error) {
      if (!mounted) return;
      if (error.code == 'BUREAU_UNAVAILABLE') {
        // El bureau caido NO es error visible: se muestra `En revisión`.
        setState(() {
          _result = _result ??
              LoanApplicationResult.inReviewNotice(
                reason: creditApplicationUserMessage(error),
              );
          _error = null;
          _loading = false;
        });
        return;
      }
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

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Estado del expediente')),
      body: _buildBody(),
    );
  }

  Widget _buildBody() {
    final result = _result;
    final error = _error;
    if (_loading && result == null) {
      return const LoadingView(
        key: Key('credit-status-loading'),
        message: 'Consultando el estado del expediente…',
      );
    }
    if (error != null && result == null) {
      return ErrorView(
        key: const Key('credit-status-error'),
        message: creditApplicationUserMessage(error),
        onRetry: _fetch,
      );
    }
    if (result == null) {
      return EmptyView(
        key: const Key('credit-status-empty'),
        message: 'Sin expediente para mostrar. '
            'Vuelve e inicia el flujo de nuevo.',
        actionLabel: 'Volver',
        onAction: () => context.pop(),
      );
    }
    return RefreshIndicator(
      onRefresh: _fetch,
      child: ListView(
        key: const Key('credit-status-content'),
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          if (_loading) const LinearProgressIndicator(),
          _expedientRow(result),
          const SizedBox(height: AppSpacing.stackMd),
          _hero(result),
          const SizedBox(height: AppSpacing.stackMd),
          _reasonCard(result),
          const SizedBox(height: AppSpacing.stackMd),
          _progressCard(result.status),
          const SizedBox(height: AppSpacing.stackMd),
          ..._ctas(result),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Cargando (skeleton), vacío (sin expediente) y error con '
            'Reintentar. Desliza para actualizar.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
        ],
      ),
    );
  }

  // --fila_expediente_+_badge----------------------------------------------------

  Widget _expedientRow(LoanApplicationResult result) {
    final signed =
        result.status == LoanApplicationStatus.approved &&
        result.contractSigned;
    return Row(
      children: [
        Expanded(
          child: Text(
            'EXPEDIENTE ${result.id.isEmpty ? 'EN CURSO' : result.id}',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.onSurface,
            ),
          ),
        ),
        _badge(result.status),
        if (signed) ...[
          const SizedBox(width: 8),
          Container(
            key: const Key('credit-status-signed-badge'),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            decoration: BoxDecoration(
              color: AppColors.successContainer,
              borderRadius: BorderRadius.circular(999),
            ),
            child: Text(
              'Firmado',
              style: AppTypography.labelMd.copyWith(
                color: AppColors.onSuccessContainer,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ],
    );
  }

  Widget _badge(LoanApplicationStatus status) {
    final (label, background, foreground) = switch (status) {
      LoanApplicationStatus.approved => (
          'Aprobado',
          AppColors.successContainer,
          AppColors.onSuccessContainer,
        ),
      LoanApplicationStatus.rejected => (
          'Rechazado',
          AppColors.errorContainer,
          AppColors.onErrorContainer,
        ),
      LoanApplicationStatus.inReview => (
          'En revisión',
          AppColors.warningContainer,
          AppColors.onWarningContainer,
        ),
      LoanApplicationStatus.expired => (
          'Vencido',
          AppColors.surfaceContainerHighest,
          AppColors.onSurfaceVariant,
        ),
      _ => (
          'Evaluando',
          AppColors.surfaceContainerHighest,
          AppColors.onSurfaceVariant,
        ),
    };
    return Container(
      key: const Key('credit-status-badge'),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        style: AppTypography.labelMd.copyWith(
          color: foreground,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }

  // --hero_del_dictamen_(tal_cual_del_servidor)-----------------------------------

  Widget _hero(LoanApplicationResult result) {
    final (title, subtitle, background, foreground) =
        _heroContent(result);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'DICTAMEN DEL SERVIDOR',
            style: AppTypography.labelSm.copyWith(
              fontWeight: FontWeight.w700,
              color: foreground,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            key: const Key('credit-status-hero-title'),
            title,
            style: AppTypography.titleMd.copyWith(
              color: foreground,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            key: const Key('credit-status-hero-subtitle'),
            subtitle,
            style: AppTypography.bodyMd.copyWith(color: foreground),
          ),
        ],
      ),
    );
  }

  (String, String, Color, Color) _heroContent(
    LoanApplicationResult result,
  ) {
    final amountsLine = _amountsLine(result);
    switch (result.status) {
      case LoanApplicationStatus.approved:
        return (
          '¡Crédito aprobado!',
          amountsLine,
          AppColors.successContainer,
          AppColors.onSuccessContainer,
        );
      case LoanApplicationStatus.rejected:
        return (
          'No pudimos aprobarlo esta vez.',
          'Revisa el motivo registrado por el servidor.',
          AppColors.errorContainer,
          AppColors.onErrorContainer,
        );
      case LoanApplicationStatus.inReview:
        return (
          'Estamos revisando tu solicitud.',
          'Un analista la verificará. Te avisaremos.',
          AppColors.warningContainer,
          AppColors.onWarningContainer,
        );
      case LoanApplicationStatus.expired:
        return (
          'El expediente venció.',
          'Vuelve al simulador para iniciar uno nuevo.',
          AppColors.surfaceContainerHighest,
          AppColors.onSurfaceVariant,
        );
      case LoanApplicationStatus.draft:
      case LoanApplicationStatus.submitted:
      case LoanApplicationStatus.inEvaluation:
        return (
          'Evaluando tu solicitud…',
          'Pre-calificación en curso (máx. 5 min).',
          AppColors.surfaceContainerHighest,
          AppColors.onSurfaceVariant,
        );
    }
  }

  /// Linea de montos con valores del servidor (sin recalcular).
  ///
  /// El expediente no trae cuota/TCEA (solo monto y plazo): se muestran esos
  /// dos; el motivo exacto vive en la card Motivo.
  String _amountsLine(LoanApplicationResult result) {
    final amount = result.requestedAmountMinor;
    final term = result.termMonths;
    if (amount == null || term == null) {
      return 'Revisa el motivo registrado por el servidor.';
    }
    return '${formatMinor(amount, 'PEN')} • '
        '$term ${term == 1 ? 'mes' : 'meses'}';
  }

  // --motivo_exact o--------------------------------------------------------------

  Widget _reasonCard(LoanApplicationResult result) {
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Motivo / justificación',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            key: const Key('credit-status-reason'),
            (result.decisionReason?.isNotEmpty ?? false)
                ? result.decisionReason!
                : 'Sin motivo registrado por el servidor.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSurface,
            ),
          ),
        ],
      ),
    );
  }

  // --progreso---------------------------------------------------------------------

  Widget _progressCard(LoanApplicationStatus status) {
    const steps = [
      'Solicitud enviada',
      'Evaluación automática',
      'Decisión lista',
      'Firma y desembolso (siguiente)',
    ];
    final current = switch (status) {
      LoanApplicationStatus.draft ||
      LoanApplicationStatus.submitted =>
        0,
      LoanApplicationStatus.inEvaluation ||
      LoanApplicationStatus.inReview =>
        1,
      _ => 2,
    };
    final decided = status == LoanApplicationStatus.approved ||
        status == LoanApplicationStatus.rejected ||
        status == LoanApplicationStatus.expired;
    return AppCard(
      child: Column(
        key: const Key('credit-status-steps'),
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Progreso',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          for (var i = 0; i < steps.length; i++)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Row(
                children: [
                  Icon(
                    i < current || (decided && i == current)
                        ? Icons.check_circle
                        : (i == current
                            ? Icons.pending
                            : Icons.radio_button_unchecked),
                    size: 20,
                    color: i <= current
                        ? AppColors.primary
                        : AppColors.secondaryText,
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      steps[i],
                      style: AppTypography.bodyMd.copyWith(
                        color: i <= current
                            ? AppColors.onSurface
                            : AppColors.secondaryText,
                        fontWeight: i == current
                            ? FontWeight.w600
                            : FontWeight.w400,
                      ),
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }

  // --CTAs_por_estado---------------------------------------------------------------

  List<Widget> _ctas(LoanApplicationResult result) {
    final status = result.status;
    // Contrato ya firmado (el servidor lo deriva de `credits.contracts`):
    // un `APPROVED` firmado deja de ser re-firmable (mismo criterio que
    // "Mis créditos": sin CTA de firma, solo lectura).

    final signed =
        status == LoanApplicationStatus.approved && result.contractSigned;

    switch (status) {
      case LoanApplicationStatus.approved:
        if (signed) {
          return [
            SizedBox(
              width: double.infinity,
              child: FilledButton(
                key: const Key('credit-status-contract-cta'),
                // Solo lectura: el juego ya está firmado, no se re-firma.
                onPressed: () =>
                    context.push('/credits/contract', extra: result.id),
                style: FilledButton.styleFrom(
                  minimumSize: const Size(88, 52),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                ),
                child: const Text('Ver contrato'),
              ),
            ),
            const SizedBox(height: AppSpacing.stackSm),
            SizedBox(
              width: double.infinity,
              child: OutlinedButton(
                key: const Key('credit-status-home-cta'),
                onPressed: () => context.go('/home'),
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size(88, 52),
                ),
                child: const Text('Volver al inicio'),
              ),
            ),
          ];
        }
        final current = _result;
        return [
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              key: const Key('credit-status-sign-cta'),
              // Expediente aprobado -> lectura del contrato (E3-T15, HU11):
              // se pasa el expediente para el hero de condiciones.
              onPressed: current == null
                  ? null
                  : () => context.push('/credits/contract', extra: current),
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Continuar a la firma'),
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(
              key: const Key('credit-status-schedule-cta'),
              // C3: el cronograma definitivo vive en "Mis préstamos"
              // (`GET /credits/loans`, E3-T21): el expediente no trae
              // `loanId`, asi que se navega a la lista (sin `comingSoon`).
              onPressed: () => context.push('/credits/loans'),
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(88, 52),
              ),
              child: const Text('Ver cronograma'),
            ),
          ),
        ];
      case LoanApplicationStatus.rejected:
      case LoanApplicationStatus.expired:
        return [
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              key: const Key('credit-status-simulator-cta'),
              onPressed: () => context.go('/credits'),
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Simular otro monto'),
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(
              key: const Key('credit-status-home-cta'),
              onPressed: () => context.go('/home'),
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(88, 52),
              ),
              child: const Text('Volver al inicio'),
            ),
          ),
        ];
      case LoanApplicationStatus.draft:
      case LoanApplicationStatus.submitted:
      case LoanApplicationStatus.inEvaluation:
      case LoanApplicationStatus.inReview:
        return [
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(
              key: const Key('credit-status-simulator-cta'),
              onPressed: () => context.go('/credits'),
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(88, 52),
              ),
              child: const Text('Volver al simulador'),
            ),
          ),
        ];
    }
  }
}
