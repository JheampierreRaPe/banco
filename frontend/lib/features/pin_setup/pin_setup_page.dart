import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';

import 'package:banca_online/features/activation/activation_service.dart';

import 'pin_setup_controller.dart';
import 'pin_setup_service.dart';

/// Pantalla de creación de PIN (standalone, ruta `/pin-setup?userRef=`).
///
/// - Explica que el código de activación ya se usó y ofrece
///   "Enviarme un código nuevo" (mismo endpoint de reenvío de `activation`).
/// - Campos: código (6) + PIN (4-6, obscurecido con toggle) + confirmar PIN.
/// - En éxito navega a `/login`. No modifica `ActivationPage`.
class PinSetupPage extends StatefulWidget {
  const PinSetupPage({
    super.key,
    required this.userRef,
    required this.setupService,
    required this.resendService,
  });

  /// Referencia del usuario (viaja como `user_ref` al backend).
  final String userRef;

  /// Servicio de creación de PIN (inyectado; en tests se pasa un fake).
  final PinSetupService setupService;

  /// Servicio de reenvío de `activation` (MISMO endpoint, sin duplicar).
  final ActivationService resendService;

  @override
  State<PinSetupPage> createState() => _PinSetupPageState();
}

class _PinSetupPageState extends State<PinSetupPage> {
  late final PinSetupController _controller;
  final _codeController = TextEditingController();
  final _pinController = TextEditingController();
  final _confirmController = TextEditingController();
  bool _obscurePin = true;
  bool _obscureConfirm = true;

  @override
  void initState() {
    super.initState();
    _controller = PinSetupController(
      setupService: widget.setupService,
      resendService: widget.resendService,
      userRef: widget.userRef,
    );
    _controller.addListener(_onControllerChanged);
  }

  void _onControllerChanged() {
    if (!mounted) return;
    if (_controller.status == PinSetupStatus.success) {
      context.go('/login');
      return;
    }
    setState(() {});
  }

  @override
  void dispose() {
    _controller
      ..removeListener(_onControllerChanged)
      ..dispose();
    _codeController.dispose();
    _pinController.dispose();
    _confirmController.dispose();
    super.dispose();
  }

  bool get _canSubmit {
    final c = _controller;
    return !c.isBusy &&
        c.status != PinSetupStatus.success &&
        c.status != PinSetupStatus.pinAlreadySet &&
        _codeController.text.trim().length == 6 &&
        _pinController.text.length >= 4 &&
        _confirmController.text.isNotEmpty;
  }

  Future<void> _onSubmit() async {
    _controller
      ..setCode(_codeController.text)
      ..setPin(_pinController.text)
      ..setConfirmPin(_confirmController.text);
    await _controller.submit();
  }

  @override
  Widget build(BuildContext context) {
    final c = _controller;
    final submitting = c.status == PinSetupStatus.submitting;

    return Scaffold(
      appBar: AppBar(title: const Text('Crea tu PIN')),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(PinSetupController.codeHint),
              const SizedBox(height: 16),
              TextButton(
                key: const Key('pin-setup-resend'),
                onPressed: c.isBusy ? null : () => _controller.resend(),
                child: c.isResending
                    ? const Text('Enviando…')
                    : const Text('Enviarme un código nuevo'),
              ),
              const SizedBox(height: 8),
              TextField(
                key: const Key('pin-setup-code'),
                controller: _codeController,
                keyboardType: TextInputType.number,
                enableSuggestions: false,
                autocorrect: false,
                inputFormatters: <TextInputFormatter>[
                  FilteringTextInputFormatter.digitsOnly,
                  LengthLimitingTextInputFormatter(6),
                ],
                decoration: const InputDecoration(
                  labelText: 'Código de 6 dígitos',
                ),
                onChanged: (_) => setState(() {}),
              ),
              const SizedBox(height: 12),
              TextField(
                key: const Key('pin-setup-pin'),
                controller: _pinController,
                keyboardType: TextInputType.number,
                obscureText: _obscurePin,
                enableSuggestions: false,
                autocorrect: false,
                inputFormatters: <TextInputFormatter>[
                  FilteringTextInputFormatter.digitsOnly,
                  LengthLimitingTextInputFormatter(6),
                ],
                decoration: InputDecoration(
                  labelText: 'PIN (4 a 6 dígitos)',
                  suffixIcon: IconButton(
                    key: const Key('pin-setup-pin-toggle'),
                    icon: Icon(
                      _obscurePin
                          ? Icons.visibility_off
                          : Icons.visibility,
                    ),
                    onPressed: () =>
                        setState(() => _obscurePin = !_obscurePin),
                  ),
                ),
                onChanged: (_) => setState(() {}),
              ),
              const SizedBox(height: 12),
              TextField(
                key: const Key('pin-setup-confirm'),
                controller: _confirmController,
                keyboardType: TextInputType.number,
                obscureText: _obscureConfirm,
                enableSuggestions: false,
                autocorrect: false,
                inputFormatters: <TextInputFormatter>[
                  FilteringTextInputFormatter.digitsOnly,
                  LengthLimitingTextInputFormatter(6),
                ],
                decoration: InputDecoration(
                  labelText: 'Confirma tu PIN',
                  suffixIcon: IconButton(
                    key: const Key('pin-setup-confirm-toggle'),
                    icon: Icon(
                      _obscureConfirm
                          ? Icons.visibility_off
                          : Icons.visibility,
                    ),
                    onPressed: () => setState(
                      () => _obscureConfirm = !_obscureConfirm,
                    ),
                  ),
                ),
                onChanged: (_) => setState(() {}),
              ),
              const SizedBox(height: 16),
              if (c.errorMessage != null)
                Text(
                  c.errorMessage!,
                  key: const Key('pin-setup-message'),
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.error,
                  ),
                ),
              if (c.infoMessage != null)
                Text(
                  c.infoMessage!,
                  key: const Key('pin-setup-info'),
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.primary,
                  ),
                ),
              const SizedBox(height: 16),
              if (c.isPinAlreadySet)
                OutlinedButton(
                  key: const Key('pin-setup-goto-login'),
                  onPressed: () => context.go('/login'),
                  child: const Text('Ir a iniciar sesión'),
                )
              else
                FilledButton(
                  key: const Key('pin-setup-submit'),
                  onPressed: _canSubmit ? _onSubmit : null,
                  child: submitting
                      ? const Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            SizedBox(
                              width: 18,
                              height: 18,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                              ),
                            ),
                            SizedBox(width: 10),
                            Text('Guardando…'),
                          ],
                        )
                      : const Text('Crear PIN'),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
