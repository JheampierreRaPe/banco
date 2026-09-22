// Paso inicial `/pin-reset`: captura email + DNI y emite el OTP `RECOVERY`.
//
// Diseño "Eucalipto y Ocre" (mockup `docs/design/pin-reset.png`: tarjeta con
// "Correo electrónico" + "Número de DNI" + franja "Nunca compartas tu PIN ni
// tu código con nadie"): solo tokens de F-T34 (`AppColors`, `AppTypography`,
// `AppSpacing`) y componentes base (`AppCard`, `AppPrimaryButton`,
// `AppGhostButton`).
//
// - Valida el formato en la UI (no decide existencia: eso lo hace el
//   servidor; cliente delgado, docs/19). El DNI se envía SOLO en el request
//   final de `pin-reset` (aquí viaja en memoria por `extra`, nunca en la
//   ruta); se muestra enmascarado.
// - Con 200 navega a `/pin-reset/otp?email=<urlencoded>&ttl=&resendWait=`
//   con el MISMO mensaje neutro exista o no el email (anti-oráculo).
// - Estados (docs/20 §7): cargando (`LoadingView`), vacío (feature no
//   disponible, `EmptyView`), error (`ErrorView` con reintento) y contenido.
// - Email/DNI nunca se loguean (docs/16 reglas 7 y 9); el OTP no usa
//   autofill.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_button.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../pin_reset_controllers.dart';

/// Paso inicial del restablecimiento de PIN (F-T43).
class PinResetIdentityPage extends StatefulWidget {
  const PinResetIdentityPage({super.key, required this.controller});

  /// Controlador delgado (propiedad del llamador; ver `pin_reset_routes.dart`).
  /// `null` = feature no disponible (estado vacío).
  final PinResetIdentityController? controller;

  @override
  State<PinResetIdentityPage> createState() => _PinResetIdentityPageState();
}

class _PinResetIdentityPageState extends State<PinResetIdentityPage> {
  late final TextEditingController _email;
  late final TextEditingController _doc;

  @override
  void initState() {
    super.initState();
    _email = TextEditingController();
    _doc = TextEditingController();
    widget.controller?.addListener(_onControllerChanged);
  }

  void _onControllerChanged() {
    if (!mounted) return;
    final controller = widget.controller;
    if (controller != null && controller.succeeded) {
      final email = Uri.encodeComponent(_email.text.trim());
      final doc = _doc.text.trim();
      final result = controller.lastResult;
      var location = '/pin-reset/otp?email=$email';
      if (result != null) {
        location += '&ttl=${result.ttlSeconds}'
            '&resendWait=${result.resendWaitSeconds}';
      }
      // El DNI viaja SOLO en memoria (`extra`): nunca en la ruta ni en logs.
      context.go(location, extra: doc);
      return;
    }
    setState(() {});
  }

  @override
  void dispose() {
    widget.controller?.removeListener(_onControllerChanged);
    _email.dispose();
    _doc.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    await widget.controller
            ?.submit(email: _email.text, docNumber: _doc.text);
  }

  @override
  Widget build(BuildContext context) {
    final controller = widget.controller;
    if (controller == null) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        appBar: AppBar(title: const Text('Restablecer PIN')),
        body: SafeArea(
          child: EmptyView(
            message: 'Restablecer PIN no está disponible en este momento.',
            actionLabel: 'Volver a iniciar sesión',
            onAction: () => context.go('/login'),
          ),
        ),
      );
    }
    if (controller.status == PinResetIdentityStatus.submitting) {
      return const Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: LoadingView(message: 'Enviando código…'),
        ),
      );
    }
    if (controller.status == PinResetIdentityStatus.error &&
        controller.errorMessage != null) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        appBar: AppBar(title: const Text('Restablecer PIN')),
        body: SafeArea(
          child: ErrorView(
            message: controller.errorMessage!,
            // Reintento: vuelve al formulario con lo capturado intacto para
            // corregir y reenviar (nunca se pierde el DNI/email).
            onRetry: () => controller.retry(),
          ),
        ),
      );
    }
    final canSubmit = !controller.isBusy && !controller.succeeded;
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Restablecer PIN')),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(AppSpacing.marginMobile),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'Crea un nuevo PIN',
                style: AppTypography.headlineMd.copyWith(
                  color: AppColors.onSurface,
                ),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                'Confirma tu identidad con correo, DNI y el código que te '
                'enviamos.',
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              // Contenido: tarjeta identidad (mockup `pin-reset`: correo +
              // DNI en una sola card).
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
                      key: const Key('pin-reset-email-field'),
                      controller: _email,
                      keyboardType: TextInputType.emailAddress,
                      style: AppTypography.bodyLg.copyWith(
                        color: AppColors.onSurface,
                      ),
                      // Dato sensible: sin sugerencias ni autocorrección;
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
                    ),
                    const SizedBox(height: AppSpacing.stackMd),
                    Text(
                      'Número de DNI',
                      style: AppTypography.labelMd.copyWith(
                        color: AppColors.primary,
                      ),
                    ),
                    const SizedBox(height: AppSpacing.stackSm),
                    TextField(
                      key: const Key('pin-reset-doc-field'),
                      controller: _doc,
                      keyboardType: TextInputType.number,
                      style: AppTypography.bodyLg.copyWith(
                        color: AppColors.onSurface,
                      ),
                      // DNI sensible: sin sugerencias ni autofill; nunca se
                      // loguea ni viaja en la ruta.
                      enableSuggestions: false,
                      autocorrect: false,
                      decoration: InputDecoration(
                        hintText: '12345678',
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
                    ),
                    const SizedBox(height: AppSpacing.stackSm),
                    Text(
                      '8 dígitos, como figura en tu DNI.',
                      style: AppTypography.bodyMd.copyWith(
                        color: AppColors.secondaryText,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              // Franja de seguridad (mockup `pin-reset`).
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
                        'Nunca compartas tu PIN ni tu código con nadie.',
                        style: AppTypography.bodyMd.copyWith(
                          color: AppColors.onPrimaryFixed,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              // Estado de contenido con mensaje neutro (mismo exista o no).
              if (controller.succeeded)
                Padding(
                  padding: const EdgeInsets.only(top: AppSpacing.stackMd),
                  child: Text(
                    PinResetIdentityController.neutralMessage,
                    key: const Key('pin-reset-identity-info'),
                    style: AppTypography.bodyMd.copyWith(
                      color: AppColors.primary,
                    ),
                  ),
                ),
              const SizedBox(height: AppSpacing.stackLg),
              AppPrimaryButton(
                key: const Key('pin-reset-identity-submit'),
                label: 'Enviar código',
                onPressed: canSubmit ? _submit : null,
              ),
              AppGhostButton(
                key: const Key('pin-reset-identity-back'),
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
