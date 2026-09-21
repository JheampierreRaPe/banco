// Pantalla `/recovery/otp`: ingresa el código de 6 dígitos y abre sesión.
//
// Reusa el patrón de `features/activation/activation_page.dart` sin acoplarse
// a ese feature:
// - 6 casillas (`recovery-otp-0..5`), autofill/sugerencias desactivados,
//   pegado repartido, avance automático.
// - Cuenta atrás de vigencia (`ttl_seconds`) y reenvío con cooldown
//   (`resend_wait_seconds`) que vuelve a llamar a `request`.
// - Submit deshabilitado hasta completar el código, sin doble envío.
// - `401 INVALID_RECOVERY_CODE` (único error genérico: incorrecto / vencido /
//   agotados) -> mismo mensaje accionable + reintentar/reenviar.
// - `429 RATE_LIMITED` -> mensaje de espera.
// - Al éxito la página navega a `/home` (ya hay sesión).
// - El OTP nunca se loguea ni queda en el autofill del SO (docs/16 reglas
//   7 y 10).
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';

import '../recovery_controller.dart';

/// Pantalla de verificación del código de recuperación (F-T29).
class RecoveryOtpPage extends StatefulWidget {
  const RecoveryOtpPage({
    super.key,
    required this.email,
    required this.controller,
    this.autoTick = true,
  });

  /// Email al que se envió el código (solo se muestra enmascarado).
  final String email;

  /// Controlador delgado (propiedad del llamador; ver `recovery_routes.dart`).
  final RecoveryOtpController controller;

  /// Si es `false` no se inicia el `Timer` real (seam para tests: el contador
  /// se avanza manualmente con `RecoveryOtpController.tick`).
  final bool autoTick;

  @override
  State<RecoveryOtpPage> createState() => _RecoveryOtpPageState();
}

class _RecoveryOtpPageState extends State<RecoveryOtpPage> {
  static const int _codeLength = 6;

  late final List<TextEditingController> _boxes;
  late final List<FocusNode> _nodes;
  bool _syncingBoxes = false;

  RecoveryOtpController get _controller => widget.controller;

  @override
  void initState() {
    super.initState();
    _boxes = List<TextEditingController>.generate(
      _codeLength,
      (_) => TextEditingController(),
    );
    _nodes = List<FocusNode>.generate(_codeLength, (_) => FocusNode());
    _controller.addListener(_onControllerChanged);
    if (widget.autoTick) _controller.startAutoTick();
  }

  void _onControllerChanged() {
    if (!mounted) return;
    if (_controller.succeeded) {
      // Sesión concedida y persistida: la guarda del router confirma `/home`.
      context.go('/home');
      return;
    }
    setState(() {});
  }

  @override
  void dispose() {
    _controller.removeListener(_onControllerChanged);
    for (final c in _boxes) {
      c.dispose();
    }
    for (final n in _nodes) {
      n.dispose();
    }
    super.dispose();
  }

  void _onDigitChanged(int index, String value) {
    if (_syncingBoxes) return;
    _syncingBoxes = true;
    try {
      final digits = value.replaceAll(RegExp('[^0-9]'), '');
      if (digits.length > 1) {
        // Pegado: repartir desde esta casilla.
        for (var i = 0; i < digits.length && index + i < _codeLength; i++) {
          _boxes[index + i].text = digits[i];
        }
        final next = index + digits.length;
        if (next < _codeLength) {
          _nodes[next].requestFocus();
        } else {
          _nodes[_codeLength - 1].unfocus();
        }
      } else if (digits.length == 1) {
        if (_boxes[index].text != digits) _boxes[index].text = digits;
        if (index < _codeLength - 1) {
          _nodes[index + 1].requestFocus();
        } else {
          _nodes[index].unfocus();
        }
      } else {
        // Borrado: limpiar y retroceder.
        if (_boxes[index].text.isNotEmpty) {
          _boxes[index].clear();
        } else if (index > 0) {
          _boxes[index - 1].clear();
          _nodes[index - 1].requestFocus();
        }
      }
    } finally {
      _syncingBoxes = false;
    }
    _pushCode();
  }

  void _pushCode() {
    _controller.setCode(_boxes.map((c) => c.text).join());
    if (mounted) setState(() {});
  }

  /// Enmascara el email para la UI (sin filtrar existencia ni exponer PII
  /// completa): `j***@dominio.com`.
  static String maskEmail(String email) {
    final clean = email.trim();
    final at = clean.indexOf('@');
    if (at <= 0) return 'tu correo';
    final local = clean.substring(0, at);
    final domain = clean.substring(at + 1);
    if (domain.isEmpty) return 'tu correo';
    final head = local.isEmpty ? '*' : local[0];
    return '$head***@$domain';
  }

  @override
  Widget build(BuildContext context) {
    final c = _controller;
    final submitting = c.status == RecoveryOtpStatus.submitting;

    return Scaffold(
      appBar: AppBar(title: const Text('Recuperar acceso')),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                RecoveryOtpController.neutralMessage,
                key: const Key('recovery-otp-info'),
              ),
              const SizedBox(height: 8),
              Text(
                'Enviamos un código de 6 dígitos a '
                '${maskEmail(widget.email)}.',
              ),
              const SizedBox(height: 8),
              Row(
                children: [
                  const Icon(Icons.timer_outlined, size: 18),
                  const SizedBox(width: 6),
                  Text(
                    'El código vence en '
                    '${formatRecoveryCountdown(c.remainingSeconds)}',
                    key: const Key('recovery-otp-countdown'),
                  ),
                ],
              ),
              const SizedBox(height: 24),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: [
                  for (var i = 0; i < _codeLength; i++) _otpBox(i),
                ],
              ),
              const SizedBox(height: 16),
              if (c.errorMessage != null)
                Text(
                  c.errorMessage!,
                  key: const Key('recovery-otp-message'),
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.error,
                  ),
                ),
              const SizedBox(height: 24),
              FilledButton(
                key: const Key('recovery-otp-submit'),
                onPressed: c.canSubmit
                    ? () async {
                        await _controller.submit(_controller.code);
                      }
                    : null,
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
                          Text('Verificando…'),
                        ],
                      )
                    : const Text('Verificar código'),
              ),
              const SizedBox(height: 8),
              TextButton(
                key: const Key('recovery-otp-resend'),
                onPressed: c.canResend
                    ? () async {
                        await _controller.resend();
                      }
                    : null,
                child: Text(
                  c.resendCooldownSeconds > 0
                      ? 'Reenviar en '
                          '${formatRecoveryCountdown(c.resendCooldownSeconds)}'
                      : 'Reenviar código',
                ),
              ),
              if (c.isResending)
                const Center(
                  key: Key('recovery-otp-resending'),
                  child: Padding(
                    padding: EdgeInsets.only(top: 8),
                    child: SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _otpBox(int index) {
    return SizedBox(
      width: 48,
      child: TextField(
        key: Key('recovery-otp-$index'),
        controller: _boxes[index],
        focusNode: _nodes[index],
        autofocus: index == 0,
        keyboardType: TextInputType.number,
        textAlign: TextAlign.center,
        style: Theme.of(context).textTheme.headlineSmall,
        // OTP sensible: sin autofill ni sugerencias (mismo patrón que
        // `activation_page.dart`; docs/16 reglas 7 y 10).
        enableSuggestions: false,
        autocorrect: false,
        inputFormatters: <TextInputFormatter>[
          FilteringTextInputFormatter.digitsOnly,
          LengthLimitingTextInputFormatter(_codeLength),
        ],
        decoration: const InputDecoration(
          border: OutlineInputBorder(),
        ),
        onChanged: (value) => _onDigitChanged(index, value),
      ),
    );
  }
}
