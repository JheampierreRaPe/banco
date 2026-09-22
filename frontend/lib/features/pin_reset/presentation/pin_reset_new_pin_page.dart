// Paso "Crea tu PIN nuevo" del restablecimiento (F-T43).
//
// Captura local del PIN (6 dígitos, sin secuencias triviales; validadores de
// `pin_setup` en solo lectura) y avanza a `/pin-reset/confirm` con el
// borrador en memoria (`extra`, nunca en la ruta ni en logs). Cliente
// delgado (docs/19): no llama al backend ni decide nada.
//
// Reutiliza las piezas del flujo PIN de F-T39 (`PinFlowHeader`,
// `PinStepHeading`, `PinCodeBoxes`, `PinKeypad`): teclado propio sin
// autofill; objetivos táctiles >= 44px (docs/20 §8).
//
// Estados (docs/20 §7): cargando (`LoadingView` al avanzar), vacío (sin
// borrador, lo resuelve la ruta con `EmptyView`), error (`ErrorView` con
// reintento que limpia) y contenido.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../../pin_setup/pin_flow_widgets.dart';
import '../../pin_setup/pin_setup_validators.dart';
import '../pin_reset_controllers.dart';
import '../pin_reset_validators.dart';

/// Crea tu PIN nuevo (paso 3 de 4 del restablecimiento).
class PinResetNewPinPage extends StatefulWidget {
  const PinResetNewPinPage({super.key, required this.draft});

  /// Borrador en memoria (email + DNI + código); `null` = estado vacío.
  final PinResetDraft? draft;

  @override
  State<PinResetNewPinPage> createState() => _PinResetNewPinPageState();
}

class _PinResetNewPinPageState extends State<PinResetNewPinPage> {
  String _pin = '';
  String? _error;
  bool _advancing = false;
  bool _navigated = false;

  void _onDigit(String digit) {
    if (_pin.length >= kPinLength || _advancing || _navigated) return;
    setState(() {
      _pin += digit;
      _error = null;
    });
    if (_pin.length == kPinLength) _maybeAdvance();
  }

  void _onBackspace() {
    if (_pin.isEmpty || _advancing || _navigated) return;
    setState(() {
      _pin = _pin.substring(0, _pin.length - 1);
      _error = null;
    });
  }

  Future<void> _maybeAdvance() async {
    final error = pinCreateError(_pin);
    if (error != null) {
      setState(() => _error = error);
      return;
    }
    setState(() => _advancing = true);
    // Cede un frame para mostrar el estado de carga antes de navegar.
    await Future<void>.delayed(Duration.zero);
    if (!mounted || _navigated) return;
    _navigated = true;
    final draft = widget.draft;
    final email = Uri.encodeComponent(draft?.email ?? '');
    context.go(
      '/pin-reset/confirm?email=$email',
      extra: draft?.copyWith(pin: _pin),
    );
  }

  void _retry() {
    setState(() {
      _error = null;
      _pin = '';
    });
  }

  @override
  Widget build(BuildContext context) {
    final draft = widget.draft;
    if (draft == null || draft.code.isEmpty) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: EmptyView(
            message: 'Primero verifica el código que te enviamos por correo.',
            actionLabel: 'Volver al inicio',
            onAction: () => context.go('/pin-reset'),
          ),
        ),
      );
    }
    if (_advancing && !_navigated) {
      return const Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: LoadingView(message: 'Preparando la confirmación…'),
        ),
      );
    }
    if (_error != null) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: ErrorView(message: _error!, onRetry: _retry),
        ),
      );
    }
    final lengthOk = _pin.length == kPinLength;
    final sequenceOk = _pin.isNotEmpty && !isWeakPin(_pin);
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
                step: 3,
                stageLabel: 'PIN nuevo',
                onBack: () => context.pop(),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              const PinStepHeading(
                title: 'Crea un nuevo PIN',
                subtitle: 'Lo usarás para entrar y para autorizar tus\n'
                    'operaciones.',
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                'Para ${maskPinResetEmail(draft.email)} · '
                'DNI ${maskDocNumber(draft.docNumber)}',
                key: const Key('pin-reset-newpin-identity'),
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: AppSpacing.stackLg),
              PinCodeBoxes(value: _pin),
              const SizedBox(height: AppSpacing.stackLg),
              _ChecklistRow(done: lengthOk, label: '6 dígitos'),
              const SizedBox(height: AppSpacing.stackSm),
              _ChecklistRow(
                done: sequenceOk,
                label: 'Sin secuencias como 123456',
              ),
              const SizedBox(height: AppSpacing.stackLg),
              PinKeypad(
                onDigit: _onDigit,
                onBackspace: _onBackspace,
                enabled: !_advancing && !_navigated,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Regla del checklist (pendiente: círculo vacío; cumplida: check en
/// success).
class _ChecklistRow extends StatelessWidget {
  const _ChecklistRow({required this.done, required this.label});

  final bool done;
  final String label;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Icon(
          done ? Icons.check_circle : Icons.radio_button_unchecked,
          key: Key('pin-reset-newpin-rule-${done ? 'done' : 'pending'}-$label'),
          size: AppSpacing.stackMd,
          color: done ? AppColors.success : AppColors.outline,
        ),
        const SizedBox(width: AppSpacing.stackSm),
        Text(
          label,
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.secondaryText,
          ),
        ),
      ],
    );
  }
}
