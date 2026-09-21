// Pantalla `/recovery`: captura el email registrado y pide el OTP.
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

import '../recovery_controller.dart';

/// Pantalla de solicitud del código de recuperación (F-T29).
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
      appBar: AppBar(title: const Text('Recuperar acceso')),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                'Escribe el correo con el que registraste tu cuenta. '
                'Te enviaremos un código para recuperar el acceso.',
              ),
              const SizedBox(height: 24),
              TextField(
                key: const Key('recovery-email-field'),
                controller: _email,
                keyboardType: TextInputType.emailAddress,
                // Correo sensible: sin sugerencias ni autocorrección; nunca
                // se loguea (docs/16 reglas 7 y 10).
                enableSuggestions: false,
                autocorrect: false,
                decoration: const InputDecoration(
                  labelText: 'Correo electrónico',
                  border: OutlineInputBorder(),
                ),
                onChanged: (_) {
                  if (c.status == RecoveryEmailStatus.error) c.retry();
                },
                onSubmitted: (_) {
                  if (canSubmit) _submit();
                },
              ),
              const SizedBox(height: 16),
              if (c.errorMessage != null)
                Text(
                  c.errorMessage!,
                  key: const Key('recovery-email-message'),
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.error,
                  ),
                ),
              if (c.succeeded)
                Text(
                  RecoveryEmailController.neutralMessage,
                  key: const Key('recovery-email-info'),
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.primary,
                  ),
                ),
              const SizedBox(height: 24),
              FilledButton(
                key: const Key('recovery-email-submit'),
                onPressed: canSubmit ? _submit : null,
                child: submitting
                    ? const Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          ),
                          SizedBox(width: 10),
                          Text('Enviando…'),
                        ],
                      )
                    : const Text('Enviar código'),
              ),
              if (c.status == RecoveryEmailStatus.error)
                TextButton(
                  key: const Key('recovery-email-retry'),
                  onPressed: canSubmit ? _submit : null,
                  child: const Text('Reintentar'),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
