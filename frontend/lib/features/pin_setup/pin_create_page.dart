// Paso "Crea tu PIN" del cierre del registro (F-T39, fig `0:497`).
//
// Captura local del PIN (6 digitos, sin secuencias triviales) y avanza a
// `/pin-setup/confirm` con el PIN en memoria (`extra`, nunca en la ruta ni
// en logs). Cliente delgado (docs/19): no llama al backend ni decide nada.
//
// Estados (docs/20 §7): vacio (casillas pristine), contenido (digitos),
// error (regla incumplida).
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/app_spacing.dart';
import '../../core/theme/app_typography.dart';
import 'pin_flow_widgets.dart';
import 'pin_setup_validators.dart';

/// Crea tu PIN de seguridad (fig `0:497`: "Protege tu cuenta",
/// "Paso 4 de 4 · Seguridad").
class PinCreatePage extends StatefulWidget {
  const PinCreatePage({super.key, required this.userRef});

  /// Referencia del usuario (viaja como query `userRef`; el `user_ref` se
  /// persiste SOLO en el paso success, F-T39/SCR-005).
  final String userRef;

  @override
  State<PinCreatePage> createState() => _PinCreatePageState();
}

class _PinCreatePageState extends State<PinCreatePage> {
  String _pin = '';
  String? _error;
  bool _navigated = false;

  void _onDigit(String digit) {
    if (_pin.length >= kPinLength || _navigated) return;
    setState(() {
      _pin += digit;
      _error = null;
    });
    if (_pin.length == kPinLength) _maybeAdvance();
  }

  void _onBackspace() {
    if (_pin.isEmpty || _navigated) return;
    setState(() {
      _pin = _pin.substring(0, _pin.length - 1);
      _error = null;
    });
  }

  void _maybeAdvance() {
    final error = pinCreateError(_pin);
    if (error != null) {
      setState(() => _error = error);
      return;
    }
    _navigated = true;
    final ref = Uri.encodeComponent(widget.userRef);
    context.go('/pin-setup/confirm?userRef=$ref', extra: _pin);
  }

  @override
  Widget build(BuildContext context) {
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
                title: 'Protege tu cuenta',
                step: 4,
                stageLabel: 'Seguridad',
                onBack: () => context.pop(),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              const PinStepHeading(
                title: 'Crea tu PIN de seguridad',
                subtitle: 'Lo usarás para entrar y para autorizar tus\n'
                    'operaciones.',
              ),
              const SizedBox(height: AppSpacing.stackLg),
              PinCodeBoxes(value: _pin),
              const SizedBox(height: AppSpacing.stackLg),
              _ChecklistRow(
                done: lengthOk,
                label: '6 dígitos',
              ),
              const SizedBox(height: AppSpacing.stackSm),
              _ChecklistRow(
                done: sequenceOk,
                label: 'Sin secuencias como 123456',
              ),
              if (_error != null) ...[
                const SizedBox(height: AppSpacing.stackMd),
                Text(
                  _error!,
                  key: const Key('pin-create-message'),
                  textAlign: TextAlign.center,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.errorCarmine,
                  ),
                ),
              ],
              const SizedBox(height: AppSpacing.stackLg),
              PinKeypad(
                onDigit: _onDigit,
                onBackspace: _onBackspace,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Regla del checklist del fig (pendiente: circulo vacio; cumplida: check
/// en success).
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
          key: Key('pin-create-rule-${done ? 'done' : 'pending'}-$label'),
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
