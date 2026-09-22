// Pantalla `/recovery`: captura el email registrado y pide el OTP.
//
// Diseño canónico "Eucalipto y Ocre" (F-T42, página `recovery (email)` +
// `docs/design/recovery-email.png`): solo tokens de F-T34 (`AppColors`,
// `AppTypography`, `AppSpacing`) y componentes base (`AppCard`,
// `AppPrimaryButton`, `AppGhostButton`).
//
// - Valida el formato en la UI (no decide existencia: eso lo hace el
//   servidor; cliente delgado, docs/19).
// - Con 200 navega a `/recovery/otp?email=<urlencoded>` con el MISMO mensaje
//   neutro exista o no el email (anti-oráculo).
// - Error de red -> mensaje accionable + "Reintentar".
// - El email nunca se loguea (docs/16 reglas 7 y 9).
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_button.dart';
import '../../../core/widgets/app_card.dart';
import '../recovery_controller.dart';

/// Pantalla de solicitud del código de recuperación (F-T40).
class RecoveryEmailPage extends StatefulWidget {
  const RecoveryEmailPage({super.key, required this.controller});

  /// Controlador delgado (propiedad del llamador; ver `recovery_routes.dart`).
  final RecoveryEmailController controller;

  @override
  State<RecoveryEmailPage> createState() => _RecoveryEmailPageState();
}

class _RecoveryEmailPageState extends State<RecoveryEmailPage> {
  late final TextEditingController _email;

  @override
  void initState() {
    super.initState();
    _email = TextEditingController();
    widget.controller.addListener(_onControllerChanged);
  }

  void _onControllerChanged() {
    if (!mounted) return;
    if (widget.controller.succeeded) {
      final email = Uri.encodeComponent(_email.text.trim());
      final result = widget.controller.lastResult;
      var location = '/recovery/otp?email=$email';
      if (result != null) {
        location += '&ttl=${result.ttlSeconds}'
            '&resendWait=${result.resendWaitSeconds}';
      }
      context.go(location);
      return;
    }
    setState(() {});
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onControllerChanged);
    _email.dispose();
    super.dispose();
  }

  Future<void> _submit() => widget.controller.submit(_email.text);

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    final submitting = c.status == RecoveryEmailStatus.submitting;
    final canSubmit = !c.isBusy && !c.succeeded;

    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Recuperar acceso')),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(AppSpacing.marginMobile),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'Recupera tu acceso',
                style: AppTypography.headlineMd.copyWith(
                  color: AppColors.onSurface,
                ),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                'Ingresa tu correo. Si está registrado, '
                'te enviaremos un código.',
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              // Contenido: tarjeta del correo (docs/20 §6).
              AppCard(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Correo electrónico',
                      style: AppTypography.labelMd.copyWith(
                        color: AppColors.primary,
                      ),
                    ),
                    const SizedBox(height: AppSpacing.stackSm),
                    TextField(
                      key: const Key('recovery-email-field'),
                      controller: _email,
                      keyboardType: TextInputType.emailAddress,
                      style: AppTypography.bodyLg.copyWith(
                        color: AppColors.onSurface,
                      ),
                      // Correo sensible: sin sugerencias ni autocorrección;
                      // nunca se loguea (docs/16 reglas 7 y 10).
                      enableSuggestions: false,
                      autocorrect: false,
                      decoration: InputDecoration(
                        hintText: 'ejemplo@correo.com',
                        hintStyle: AppTypography.bodyMd.copyWith(
                          color: AppColors.secondaryText,
                        ),
                        filled: true,
                        fillColor: AppColors.surfaceContainerLowest,
                        contentPadding: const EdgeInsets.symmetric(
                          horizontal: AppSpacing.stackMd,
                          vertical: AppSpacing.stackSm + AppSpacing.unit,
                        ),
                        enabledBorder: OutlineInputBorder(
                          borderRadius:
                              BorderRadius.circular(AppRadii.md),
                          borderSide: const BorderSide(
                            color: AppColors.outlineVariant,
                          ),
                        ),
                        focusedBorder: OutlineInputBorder(
                          borderRadius:
                              BorderRadius.circular(AppRadii.md),
                          borderSide: const BorderSide(
                            color: AppColors.primary,
                            width: 2,
                          ),
                        ),
                      ),
                      onChanged: (_) {
                        if (c.status == RecoveryEmailStatus.error) c.retry();
                      },
                      onSubmitted: (_) {
                        if (canSubmit) _submit();
                      },
                    ),
                    const SizedBox(height: AppSpacing.stackSm),
                    Text(
                      'Usa el correo con el que creaste tu cuenta.',
                      style: AppTypography.bodyMd.copyWith(
                        color: AppColors.secondaryText,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              // Franja informativa anti-oráculo.
              Container(
                padding: const EdgeInsets.all(AppSpacing.stackMd),
                decoration: BoxDecoration(
                  color: AppColors.primaryFixed,
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Icon(
                      Icons.info_outline,
                      size: 20,
                      color: AppColors.onPrimaryFixed,
                    ),
                    const SizedBox(width: AppSpacing.stackSm),
                    Expanded(
                      child: Text(
                        'Por seguridad mostramos siempre el mismo mensaje, '
                        'exista o no la cuenta.',
                        style: AppTypography.bodyMd.copyWith(
                          color: AppColors.onPrimaryFixed,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              // Estado de error (docs/20 §7) con reintento.
              if (c.errorMessage != null) ...[
                const SizedBox(height: AppSpacing.stackMd),
                Container(
                  padding: const EdgeInsets.all(AppSpacing.stackMd),
                  decoration: BoxDecoration(
                    color: AppColors.errorContainer,
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                  child: Text(
                    c.errorMessage!,
                    key: const Key('recovery-email-message'),
                    style: AppTypography.bodyMd.copyWith(
                      color: AppColors.onErrorContainer,
                    ),
                  ),
                ),
              ],
              // Estado de contenido con mensaje neutro (mismo exista o no).
              if (c.succeeded)
                Padding(
                  padding: const EdgeInsets.only(top: AppSpacing.stackMd),
                  child: Text(
                    RecoveryEmailController.neutralMessage,
                    key: const Key('recovery-email-info'),
                    style: AppTypography.bodyMd.copyWith(
                      color: AppColors.primary,
                    ),
                  ),
                ),
              const SizedBox(height: AppSpacing.stackLg),
              // Estado de carga: el botón primario muestra spinner.
              AppPrimaryButton(
                key: const Key('recovery-email-submit'),
                label: 'Enviar código',
                loading: submitting,
                onPressed: canSubmit ? _submit : null,
              ),
              if (c.status == RecoveryEmailStatus.error)
                Padding(
                  padding: const EdgeInsets.only(top: AppSpacing.stackSm),
                  child: AppGhostButton(
                    key: const Key('recovery-email-retry'),
                    label: 'Reintentar',
                    onPressed: canSubmit ? _submit : null,
                  ),
                ),
              AppGhostButton(
                key: const Key('recovery-email-back'),
                label: 'Volver a iniciar sesión',
                onPressed: () => context.go('/login'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
