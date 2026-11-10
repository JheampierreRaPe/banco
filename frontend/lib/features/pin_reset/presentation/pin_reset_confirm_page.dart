// Paso "Confirma tu PIN nuevo" del restablecimiento (F-T43).
//
// Compara localmente contra el PIN creado (llega en memoria, nunca en la
// ruta ni en logs); el desajuste bloquea sin llamar al backend (validación
// de UI, no de negocio). Al coincidir fija el PIN con
// `POST /auth/pin-reset` y navega al success.
//
// Reutiliza las piezas del flujo PIN de F-T39 (`PinFlowHeader`,
// `PinStepHeading`, `PinCodeBoxes`, `PinKeypad`, `pinConfirmError`) en solo
// lectura. `401 INVALID_PIN_RESET` se muestra con el copy genérico único
// (anti-enumeración); `429` muestra espera; `422`/red permiten reintentar.
//
// Estados (docs/20 §7): cargando (`LoadingView` al fijar el PIN), vacío (sin
// borrador, lo resuelve la ruta con `EmptyView`), error (`ErrorView` con
// reintento) y contenido.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/session/session_identity_store.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../../pin_setup/pin_flow_widgets.dart';
import '../../pin_setup/pin_setup_validators.dart';
import '../pin_reset_controllers.dart';
import '../pin_reset_service.dart';

/// Confirma tu PIN nuevo y lo fija en el servidor (paso 4 de 4).
class PinResetConfirmPage extends StatefulWidget {
  const PinResetConfirmPage({
    super.key,
    required this.draft,
    required this.service,
    this.identity,
  });

  /// Borrador en memoria (email + DNI + código + PIN); `null` = vacío.
  final PinResetDraft? draft;

  /// Servicio del feature (inyectado; en tests se pasa un fake). `null` =
  /// vacío.
  final PinResetService? service;

  /// Store de identidad inyectable (tests). Por defecto, el fijado por el
  /// orquestador vía [sessionIdentityStoreFactory].
  final SessionIdentityStore? identity;

  @override
  State<PinResetConfirmPage> createState() => _PinResetConfirmPageState();
}

class _PinResetConfirmPageState extends State<PinResetConfirmPage> {
  String _confirm = '';
  String? _localError;
  bool _navigated = false;
  PinResetConfirmController? _controller;

  @override
  void initState() {
    super.initState();
    final draft = widget.draft;
    final service = widget.service;
    if (draft != null && draft.pin.isNotEmpty && service != null) {
      _controller = PinResetConfirmController(
        service: service,
        draft: draft,
        identity: widget.identity,
      )..addListener(_onControllerChanged);
    }
  }

  void _onControllerChanged() {
    if (!mounted) return;
    if (_controller?.succeeded ?? false) {
      if (_navigated) return;
      _navigated = true;
      final ref = Uri.encodeComponent(_controller?.resetUserRef ?? '');
      context.go('/pin-reset/success?userRef=$ref');
      return;
    }
    setState(() {});
  }

  @override
  void dispose() {
    _controller
      ?..removeListener(_onControllerChanged)
      ..dispose();
    super.dispose();
  }

  void _onDigit(String digit) {
    final controller = _controller;
    if (_confirm.length >= kPinLength ||
        _navigated ||
        (controller?.isBusy ?? false)) {
      return;
    }
    setState(() {
      _confirm += digit;
      _localError = null;
    });
    if (_confirm.length == kPinLength) _maybeSubmit();
  }

  void _onBackspace() {
    final controller = _controller;
    if (_confirm.isEmpty ||
        _navigated ||
        (controller?.isBusy ?? false)) {
      return;
    }
    setState(() {
      _confirm = _confirm.substring(0, _confirm.length - 1);
      _localError = null;
    });
  }

  Future<void> _maybeSubmit() async {
    final draft = widget.draft;
    final controller = _controller;
    if (draft == null || controller == null) return;
    final error = pinConfirmError(draft.pin, _confirm);
    if (error != null) {
      // Desajuste local: bloquea el avance sin disparar el request final.
      setState(() => _localError = error);
      return;
    }
    await controller.submit();
  }

  void _retry() {
    _controller?.retry();
    setState(() {
      _localError = null;
      _confirm = '';
    });
  }

  @override
  Widget build(BuildContext context) {
    final draft = widget.draft;
    final controller = _controller;
    if (draft == null || draft.pin.isEmpty || controller == null) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: EmptyView(
            message: 'Primero crea tu PIN nuevo para poder confirmarlo.',
            actionLabel: 'Crear mi PIN',
            onAction: () => context.go('/pin-reset'),
          ),
        ),
      );
    }
    if (controller.isBusy) {
      return const Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: LoadingView(message: 'Fijando tu PIN…'),
        ),
      );
    }
    final backendError = controller.errorMessage;
    if (_localError != null || backendError != null) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: ErrorView(
            message: _localError ?? backendError!,
            onRetry: _retry,
          ),
        ),
      );
    }
    return Scaffold(
      backgroundColor: AppColors.surface,
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(AppSpacing.stackMd),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              PinFlowHeader(
                title: 'Restablecer PIN',
                step: 4,
                stageLabel: 'Confirmación',
                onBack: () => context.pop(),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              const PinStepHeading(
                title: 'Confírmalo',
                subtitle: 'Vuelve a escribir los 6 dígitos.',
              ),
              const SizedBox(height: AppSpacing.stackLg),
              PinCodeBoxes(value: _confirm),
              const SizedBox(height: AppSpacing.stackMd),
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  const Icon(
                    Icons.lock_outline,
                    size: AppSpacing.stackMd,
                    color: AppColors.secondaryText,
                  ),
                  const SizedBox(width: AppSpacing.stackSm),
                  Text(
                    'Asegúrate de que sea idéntico al anterior',
                    style: AppTypography.bodyMd.copyWith(
                      color: AppColors.secondaryText,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: AppSpacing.stackLg),
              PinKeypad(
                onDigit: _onDigit,
                onBackspace: _onBackspace,
                enabled: !controller.isBusy && !_navigated,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
