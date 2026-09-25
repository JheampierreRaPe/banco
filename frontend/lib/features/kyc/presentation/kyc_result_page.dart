import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/session/session_identity_store.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_button.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../kyc_dependencies.dart';
import '../kyc_error_handler.dart';
import '../kyc_flow_controller.dart';
import '../kyc_models.dart';
import 'kyc_fig_widgets.dart';

/// Paso 3 del KYC: resultado y motivo de fallo si aplica.
///
/// Restyle F-T38 con los tokens de F-T34 (sin estilos hardcodeados). La
/// lógica NO cambia: en exito el alta continua a crear el PIN
/// (`/pin-setup?userRef=`); el backend valida el OTP y activa; el cliente
/// solo navega (cliente delgado, docs/19).
/// El orden PIN -> OTP -> success y `saveUserRef` SOLO en success son F-T39
/// (SCR-005): esta pagina ya NO persiste el `user_ref`.
class KycResultPage extends StatelessWidget {
  const KycResultPage({super.key, this.controller, this.identity});

  /// Controlador inyectable (tests). Por defecto, el compartido de
  /// [KycDependencies].
  final KycFlowController? controller;

  /// Store de identidad (F-T39/SCR-005: reservado; el `user_ref` se persiste
  /// SOLO en el paso success, ya no aqui). Se conserva el parametro para no
  /// romper la API inyectable en tests.
  final SessionIdentityStore? identity;

  KycFlowController _resolve(BuildContext context) =>
      controller ?? KycDependencies.controller;

  @override
  Widget build(BuildContext context) {
    final c = _resolve(context);
    // F-T47: el retroceso VOLUNTARIO (sistema) pide confirmación porque
    // reinicia TODO el registro. El reinicio por fallo final usa `go('/kyc')`
    // y NO pasa por aquí (silencioso, sin popup).
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        unawaited(requestKycBackRestart(context, c));
      },
      child: Scaffold(
        backgroundColor: AppColors.surface,
        appBar: const PreferredSize(
          preferredSize:
              Size.fromHeight(AppSpacing.stackXl + AppSpacing.stackMd),
          child: KycTopBar(title: 'Resultado de verificación'),
        ),
        body: ListenableBuilder(
        listenable: c,
        builder: (context, _) {
          if (c.busy) {
            return const LoadingView(message: 'Enviando verificacion...');
          }
          final result = c.result;
          if (result == null) {
            if (c.errorMessage != null) {
              return Padding(
                padding: const EdgeInsets.all(AppSpacing.stackMd),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const KycFormErrorBanner(
                      message: 'No pudimos enviar tu verificación',
                    ),
                    const SizedBox(height: AppSpacing.stackMd),
                    ErrorView(
                      message: c.errorMessage!,
                      onRetry: () => _retry(context, c),
                    ),
                  ],
                ),
              );
            }
            return EmptyView(
              message: 'Aun no hay un resultado de verificacion.',
              actionLabel: 'Volver a las tareas',
              onAction: () => context.go('/kyc/task'),
            );
          }
          if (result.overallResult) {
            return SingleChildScrollView(
              padding: const EdgeInsets.all(AppSpacing.stackMd),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Container(
                    padding: const EdgeInsets.all(AppSpacing.containerPadding),
                    decoration: BoxDecoration(
                      color: AppColors.successContainer.withValues(alpha: 0.45),
                      borderRadius: BorderRadius.circular(AppRadii.lg),
                    ),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        const Icon(
                          Icons.check_circle_outline,
                          size: 72,
                          color: AppColors.success,
                        ),
                        const SizedBox(height: AppSpacing.stackMd),
                        Text(
                          'Verificacion exitosa',
                          textAlign: TextAlign.center,
                          style: AppTypography.headlineSm.copyWith(
                            color: AppColors.primary,
                          ),
                        ),
                        const SizedBox(height: AppSpacing.stackSm),
                        Text(
                          'Tu identidad fue verificada. Continúa para crear '
                          'tu PIN de acceso.',
                          textAlign: TextAlign.center,
                          style: AppTypography.bodyMd.copyWith(
                            color: AppColors.secondaryText,
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: AppSpacing.stackLg),
                  AppPrimaryButton(
                    key: const Key('kyc-result-continue'),
                    label: 'Crear mi PIN',
                    onPressed: () => _startPinSetup(context, result),
                  ),
                ],
              ),
            );
          }
          return SingleChildScrollView(
            padding: const EdgeInsets.all(AppSpacing.stackMd),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Container(
                  padding: const EdgeInsets.all(AppSpacing.containerPadding),
                  decoration: BoxDecoration(
                    color: AppColors.errorContainer.withValues(alpha: 0.45),
                    borderRadius: BorderRadius.circular(AppRadii.lg),
                    border: Border.all(
                      color: AppColors.errorCarmine.withValues(alpha: 0.5),
                    ),
                  ),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(
                        Icons.cancel_outlined,
                        size: 72,
                        color: AppColors.errorCarmine,
                      ),
                      const SizedBox(height: AppSpacing.stackMd),
                      Text(
                        'No se pudo verificar tu identidad.',
                        textAlign: TextAlign.center,
                        style: AppTypography.headlineSm.copyWith(
                          color: AppColors.primary,
                        ),
                      ),
                      // F-T23: se indica QUE paso fallo y el motivo traducido del
                      // servidor (E1-T29).
                      // F-T48: el motivo documento-registrado no se muestra
                      // (la linea `Motivo: ...` se oculta en ese caso).
                      if (result.failedStep != null) ...[
                        const SizedBox(height: AppSpacing.stackSm),
                        Text(
                          'Paso fallido: ${result.failedStep}',
                          key: const Key('kycResultFailedStep'),
                          textAlign: TextAlign.center,
                          style: AppTypography.bodyMd.copyWith(
                            color: AppColors.onSurface,
                          ),
                        ),
                      ],
                      if (!isDocumentAlreadyRegisteredReason(
                        result.failureReason,
                      )) ...[
                        const SizedBox(height: AppSpacing.stackSm),
                        Text(
                          'Motivo: ${kycReasonMessage(result.failureReason)}',
                          key: const Key('kycResultReason'),
                          textAlign: TextAlign.center,
                          style: AppTypography.bodyMd.copyWith(
                            color: AppColors.onSurfaceVariant,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(height: AppSpacing.stackLg),
                AppPrimaryButton(
                  label: 'Reintentar verificacion',
                  onPressed: () => _retry(context, c),
                ),
              ],
            ),
          );
        },
        ),
      ),
    );
  }

  /// F-T47: el fallo final ya no reintenta con el estado anterior: reinicia
  /// TODO el registro ([KycFlowController.resetFull], incluye email/teléfono
  /// y documento/titular) y vuelve a `/kyc` para empezar de nuevo.
  void _retry(BuildContext context, KycFlowController c) {
    c.resetFull();
    context.go('/kyc');
  }

  /// Navega a crear el PIN con el `userId` del alta.
  ///
  /// F-T19 garantiza `user_id` en el exito; si faltara (backend legacy) no
  /// se inventa una referencia: se avisa y no se navega. El `user_ref` se
  /// persiste SOLO en el paso success (F-T39/SCR-005), nunca aqui.
  void _startPinSetup(
    BuildContext context,
    KycSubmitResult result,
  ) {
    final userId = result.userId;
    if (userId == null || userId.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            'No pudimos obtener tu identificador. Vuelve a intentarlo.',
          ),
        ),
      );
      return;
    }
    context.go('/pin-setup?userRef=${Uri.encodeComponent(userId)}');
  }
}
