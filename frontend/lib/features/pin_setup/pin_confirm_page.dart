// Paso "Confirma tu PIN" del cierre del registro (F-T39, fig `0:604`).
//
// Compara localmente contra el PIN creado (llega en memoria, nunca en la
// ruta ni en logs) y avanza al paso biometrico. El desajuste bloquea sin
// llamar al backend (validacion de UI, no de negocio).
//
// Estados (docs/20 §7): vacio (casillas pristine), contenido (digitos),
// error (no coincide).
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/app_spacing.dart';
import '../../core/theme/app_typography.dart';
import 'pin_flow_widgets.dart';
import 'pin_setup_draft.dart';
import 'pin_setup_validators.dart';

/// Confirma tu PIN (fig `0:604`: "Confírmalo",
/// "Vuelve a escribir los 6 dígitos.").
class PinConfirmPage extends StatefulWidget {
  const PinConfirmPage({
    super.key,
    required this.userRef,
    required this.pin,
  });

  /// Referencia del usuario (query `userRef`).
  final String userRef;

  /// PIN creado en el paso anterior (solo memoria, jamas en ruta/logs).
  final String pin;

  @override
  State<PinConfirmPage> createState() => _PinConfirmPageState();
}

class _PinConfirmPageState extends State<PinConfirmPage> {
  String _confirm = '';
  String? _error;
  bool _navigated = false;

  void _onDigit(String digit) {
    if (_confirm.length >= kPinLength || _navigated) return;
    setState(() {
      _confirm += digit;
      _error = null;
    });
    if (_confirm.length == kPinLength) _maybeAdvance();
  }

  void _onBackspace() {
    if (_confirm.isEmpty || _navigated) return;
    setState(() {
      _confirm = _confirm.substring(0, _confirm.length - 1);
      _error = null;
    });
  }

  void _maybeAdvance() {
    // El desajuste no avanza (se conserva el error en pantalla); solo el PIN
    // identico llega a la oferta biometrica, con el borrador en memoria.
    if (_navigated) return;
    final error = pinConfirmError(widget.pin, _confirm);
    if (error != null) {
      setState(() => _error = error);
      return;
    }
    _navigated = true;
    final ref = Uri.encodeComponent(widget.userRef);
    context.go(
      '/pin-setup/biometrics?userRef=$ref',
      extra: PinSetupDraft(pin: widget.pin),
    );
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      setState(() {
        _confirm = '';
        _error = null;
        _navigated = false;
      });
    });
  }

  /// Vuelve a crear descartando el borrador: `go` reconstruye crear con el
  /// estado limpio (PIN vacio, editable, sin error). No se usa `pop()` porque
  /// `go` reemplaza la pila y no habria a donde volver.
  void _goBackToCreate() {
    if (_navigated) return;
    final ref = Uri.encodeComponent(widget.userRef);
    context.go('/pin-setup?userRef=$ref');
  }

  @override
  Widget build(BuildContext context) {
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
                onBack: _goBackToCreate,
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
              if (_error != null) ...[
                const SizedBox(height: AppSpacing.stackMd),
                Text(
                  _error!,
                  key: const Key('pin-confirm-message'),
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
