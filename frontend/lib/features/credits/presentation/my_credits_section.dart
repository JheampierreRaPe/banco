import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/credit_applications_service.dart';
import '../data/my_credits_service.dart';
import '../models/loan_application.dart';
import '../models/own_application.dart';
import '../utils/format.dart';

/// Sección "Mis créditos" (E3-T23, HU10/HU11; spec
/// `docs/design/creditos-pantallas.md` §7, mockup `creditos-mis-creditos.png`).
///
/// - Header `Mis créditos` + `Tus expedientes. El estado lo decide el
///   servidor.`
/// - Fila por expediente: `{Producto} • {Monto} • {plazo} m` + badge de
///   estado; línea 2 `EXPEDIENTE {id} • {fecha}`; fila `Aprobado` con borde
///   `primary`. Aprobadas sin firmar: CTA `Continuar a la firma`
///   (→ `/credits/contract` con el id) + `Ver estado ›`
///   (→ `/credits/status` con el id); aprobadas ya firmadas
///   (`contract_signed` del servidor): badge `Firmado` y SIN CTA de firma
///   (solo `Ver estado ›`); no aprobadas: solo `Ver estado ›`.
/// - 4 estados (`docs/20` §7): contenido, carga (skeleton), vacío
///   (`Aún no tienes solicitudes` + `Ir al simulador`) y error accionable
///   con Reintentar + pull-to-refresh.
///
/// Cliente delgado (`docs/19` §4): muestra el `status` y el
/// `contract_signed` TAL CUAL responde el servidor (la firma solo se ofrece
/// con `APPROVED` sin firmar del servidor); el `decision_reason` no se
/// muestra en la fila (vive en el estado).
///
/// [service] es inyectable para tests; la ruta lo resuelve desde
/// `creditApplicationsServiceFactory` (el servicio concreto implementa
/// [MyCreditsServiceBase]). `null` degrada a error accionable.
class MyCreditsSection extends StatefulWidget {
  const MyCreditsSection({super.key, this.service, this.onGoSimulator});

  final MyCreditsServiceBase? service;

  /// Acción del vacío (`Ir al simulador`): la home cambia de pestaña.
  final VoidCallback? onGoSimulator;

  @override
  State<MyCreditsSection> createState() => _MyCreditsSectionState();
}

class _MyCreditsSectionState extends State<MyCreditsSection> {
  late Future<OwnApplicationsPage> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
    _future.ignore();
  }

  Future<OwnApplicationsPage> _load() {
    final service = widget.service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de créditos no configurado.',
        ),
      );
    }
    return service.listMine();
  }

  Future<void> _refresh() async {
    final future = _load();
    future.ignore();
    setState(() {
      _future = future;
    });
    await future;
  }

  void _retry() {
    final future = _load();
    future.ignore();
    setState(() {
      _future = future;
    });
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<OwnApplicationsPage>(
      future: _future,
      builder: (context, snapshot) {
        if (snapshot.connectionState == ConnectionState.waiting) {
          return const _MyCreditsSkeleton(
            key: Key('my-credits-loading'),
          );
        }
        if (snapshot.hasError) {
          final error = snapshot.error;
          return ErrorView(
            key: const Key('my-credits-error'),
            message: error is ApiException
                ? creditApplicationUserMessage(error)
                : 'Ocurrió un error al consultar el servidor.',
            onRetry: _retry,
          );
        }
        final page = snapshot.data;
        final items = page?.items ?? const <OwnCreditApplication>[];
        if (items.isEmpty) {
          return _MyCreditsEmpty(
            key: const Key('my-credits-empty'),
            onGoSimulator: widget.onGoSimulator,
          );
        }
        return RefreshIndicator(
          onRefresh: _refresh,
          child: ListView(
            key: const Key('my-credits-list'),
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
            children: [
              _SectionHeader(),
              const SizedBox(height: AppSpacing.stackMd),
              for (final item in items) ...[
                _ApplicationRow(item: item),
                const SizedBox(height: AppSpacing.stackSm),
              ],
              Text(
                'Orden: más reciente primero. Desliza para actualizar.',
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

/// Cabecera de la sección (spec §7).
class _SectionHeader extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          'Mis créditos',
          style: AppTypography.headlineSm.copyWith(
            color: AppColors.primary,
            fontWeight: FontWeight.w600,
          ),
        ),
        const SizedBox(height: AppSpacing.stackSm),
        Text(
          'Tus expedientes. El estado lo decide el servidor.',
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.secondaryText,
          ),
        ),
      ],
    );
  }
}

/// Badge de estado (mismo set/colores que la pantalla de estado, §3/§7).
class CreditStatusBadge extends StatelessWidget {
  const CreditStatusBadge({super.key, required this.status});

  final LoanApplicationStatus status;

  @override
  Widget build(BuildContext context) {
    final (background, foreground) = switch (status) {
      LoanApplicationStatus.approved => (
          AppColors.successContainer,
          AppColors.onSuccessContainer,
        ),
      LoanApplicationStatus.rejected => (
          AppColors.errorContainer,
          AppColors.onErrorContainer,
        ),
      LoanApplicationStatus.inReview => (
          AppColors.warningContainer,
          AppColors.onWarningContainer,
        ),
      _ => (
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
        ownApplicationStatusLabel(status),
        style: AppTypography.labelMd.copyWith(
          color: foreground,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

/// Badge `Firmado` (contrato ya firmado según el servidor,
/// `success-container`; acompaña al badge de estado).
class SignedContractBadge extends StatelessWidget {
  const SignedContractBadge({super.key});

  @override
  Widget build(BuildContext context) {
    return Container(
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
    );
  }
}

/// Fila de expediente (card blanca; borde `primary` si está aprobada).
class _ApplicationRow extends StatelessWidget {
  const _ApplicationRow({required this.item});

  final OwnCreditApplication item;

  bool get _approved => item.status == LoanApplicationStatus.approved;

  /// Contrato ya firmado (el servidor lo deriva de `credits.contracts`).
  bool get _signed => item.contractSigned;

  /// La firma solo se ofrece al aprobado pendiente de firma: un firmado
  /// deja de ser re-firmable (múltiples accesos cerrados).
  bool get _canSign => _approved && !_signed;

  String _secondLine() {
    final id = item.id.length > 12 ? item.id.substring(0, 12) : item.id;
    final date = item.createdAt;
    final shortDate =
        date != null && date.length >= 10 ? date.substring(0, 10) : date;
    if (shortDate == null || shortDate.isEmpty) return 'EXPEDIENTE $id';
    return 'EXPEDIENTE $id • $shortDate';
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      key: Key('my-credits-row-${item.id}'),
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLowest,
        borderRadius: BorderRadius.circular(AppRadii.lg),
        boxShadow: AppShadows.cardList,
        border: _approved
            ? Border.all(color: AppColors.primary, width: 1.5)
            : Border.all(color: AppColors.outlineVariant),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Text(
                  '${item.displayProduct} • '
                  '${formatMinor(item.requestedAmountMinor, 'PEN')} • '
                  '${item.termMonths} m',
                  style: AppTypography.bodyLg.copyWith(
                    color: AppColors.primary,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Column(
                crossAxisAlignment: CrossAxisAlignment.end,
                mainAxisSize: MainAxisSize.min,
                children: [
                  CreditStatusBadge(
                    key: Key('my-credits-badge-${item.id}'),
                    status: item.status,
                  ),
                  if (_signed) ...[
                    const SizedBox(height: 6),
                    SignedContractBadge(
                      key: Key('my-credits-signed-${item.id}'),
                    ),
                  ],
                ],
              ),
            ],
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            _secondLine(),
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          if (_canSign)
            SizedBox(
              width: double.infinity,
              child: FilledButton(
                key: Key('my-credits-sign-${item.id}'),
                onPressed: () =>
                    context.push('/credits/contract', extra: item.id),
                style: FilledButton.styleFrom(
                  minimumSize: const Size(88, 52),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                ),
                child: const Text('Continuar a la firma'),
              ),
            ),
          if (_canSign) const SizedBox(height: AppSpacing.stackSm),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              key: Key('my-credits-status-${item.id}'),
              onPressed: () =>
                  context.push('/credits/status', extra: item.id),
              child: Text(
                'Ver estado ›',
                style: AppTypography.labelMd.copyWith(
                  color: AppColors.accentText,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Estado vacío (spec §7).
class _MyCreditsEmpty extends StatelessWidget {
  const _MyCreditsEmpty({super.key, this.onGoSimulator});

  final VoidCallback? onGoSimulator;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.inbox_outlined, size: 48),
            const SizedBox(height: 12),
            Text(
              'Aún no tienes solicitudes',
              style: AppTypography.titleMd.copyWith(
                color: AppColors.primary,
              ),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(
              'Simula un crédito y envía tu primera solicitud.',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 12),
            OutlinedButton(
              key: const Key('my-credits-empty-cta'),
              onPressed: onGoSimulator,
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(88, 52),
              ),
              child: const Text('Ir al simulador'),
            ),
          ],
        ),
      ),
    );
  }
}

/// Carga: skeleton de filas (barras `surface-container-highest`).
class _MyCreditsSkeleton extends StatelessWidget {
  const _MyCreditsSkeleton({super.key});

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
      children: [
        for (var i = 0; i < 3; i++) ...[
          Container(
            height: 96,
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerHighest,
              borderRadius: BorderRadius.circular(AppRadii.lg),
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
        ],
        const LoadingView(message: 'Cargando tus créditos...'),
      ],
    );
  }
}
