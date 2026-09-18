import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';

import '../../core/notifications/notification_service.dart';
import 'activation_controller.dart';
import 'activation_notifications.dart';
import 'activation_service.dart';

/// Pantalla de activación por OTP (E1-T11, HU02 CA-02..CA-04).
///
/// Decisiones documentadas:
/// - **6 casillas (una por dígito) en lugar de un campo único**: es el patrón
///   móvil estándar para OTP, facilita el avance automático y evita que el
///   teclado guarde el código en sugerencias. El pegado de 6 dígitos se
///   reparte entre casillas.
/// - **Autofill desactivado a propósito**: sin `autofillHints`,
///   `enableSuggestions`/`autocorrect` en `false`. El OTP es un secreto de un
///   solo uso y no debe quedar en el autofill del SO ni en el diccionario del
///   teclado (docs/16 reglas 7 y 10).
/// - **Sin auto-login tras activar**: `/auth/activate` no devuelve tokens
///   (verificado en `backend/tests/test_activation.py`: solo `user_id` +
///   `status`) y el primer inicio de sesión exige biometría/PIN según F-T03;
///   por eso se navega a `/login`.
class ActivationPage extends StatefulWidget {
  const ActivationPage({
    super.key,
    required this.userRef,
    required this.service,
    this.otpValiditySeconds = 600,
    this.resendWaitSeconds = 30,
    this.autoTick = true,
    this.notifications,
  });

  /// Referencia del usuario a activar (viaja como `user_ref` al backend).
  final String userRef;

  /// Servicio inyectado (en producción lo provee `activation_routes.dart` vía
  /// [activationServiceFactory]; en tests se pasa un fake).
  final ActivationService service;

  /// Servicio de notificaciones locales. Si no se inyecta, la página crea
  /// uno propio (y lo destruye al salir). Inyectarlo en tests permite
  /// verificar el aviso tras un reenvío exitoso y el tap hacia `/activate`.
  final NotificationService? notifications;

  /// Vigencia del OTP en segundos (600 = 10 min).
  final int otpValiditySeconds;

  /// Espera mínima entre reenvíos (30 s).
  final int resendWaitSeconds;

  /// Si es `false` no se inicia el `Timer` real (seam para tests: el contador
  /// se avanza manualmente con `ActivationController.tick`).
  final bool autoTick;

  @override
  State<ActivationPage> createState() => _ActivationPageState();
}

class _ActivationPageState extends State<ActivationPage> {
  static const int _codeLength = 6;

  late final ActivationController _controller;
  late final List<TextEditingController> _boxes;
  late final List<FocusNode> _nodes;
  late final NotificationService _notifications;
  bool _ownsNotifications = false;
  bool _syncingBoxes = false;

  @override
  void initState() {
    super.initState();
    _notifications = widget.notifications ?? NotificationService();
    _ownsNotifications = widget.notifications == null;
    _notifications.addTapListener(_onNotificationTap);
    _controller = ActivationController(
      service: widget.service,
      userRef: widget.userRef,
      otpValiditySeconds: widget.otpValiditySeconds,
      resendWaitSeconds: widget.resendWaitSeconds,
      notifications: _notifications,
    );
    _controller.addListener(_onControllerChanged);
    _boxes =
        List<TextEditingController>.generate(_codeLength, (_) => TextEditingController());
    _nodes = List<FocusNode>.generate(_codeLength, (_) => FocusNode());
    if (widget.autoTick) _controller.startAutoTick();
  }

  void _onControllerChanged() {
    if (!mounted) return;
    if (_controller.status == ActivationStatus.success) {
      // Cuenta ACTIVE: continúa a crear el PIN (flujo alta: activate ->
      // pin-setup -> login). Sin auto-login, ver doc de la clase.
      final userRef = Uri.encodeComponent(widget.userRef);
      context.go('/pin-setup?userRef=$userRef');
      return;
    }
    setState(() {});
  }

  void _onNotificationTap(AppNotification notification) {
    if (!mounted) return;
    // Deep-link del aviso (siempre `/activate?userRef=...`): el tap abre
    // la activación con la referencia del usuario.
    context.go(notification.route);
  }

  void _showHowCodeArrives() {
    showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text(ActivationNotifications.howItArrivesTitle),
        content: const Text(ActivationNotifications.howItArrivesBody),
        actions: [
          TextButton(
            key: const Key('activate-how-code-close'),
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Entendido'),
          ),
        ],
      ),
    );
  }

  Future<void> _onResendPressed() async {
    final ok = await _controller.resend();
    if (!mounted || !ok) return;
    final body =
        _notifications.last?.body ?? ActivationController.resentMessage;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        key: const Key('activate-otp-snackbar'),
        content: Text(body),
        action: SnackBarAction(
          label: 'Ver',
          onPressed: () => context.go(
            ActivationNotifications.activateRoute(widget.userRef),
          ),
        ),
      ),
    );
  }

  @override
  void dispose() {
    _notifications.removeTapListener(_onNotificationTap);
    if (_ownsNotifications) _notifications.dispose();
    _controller
      ..removeListener(_onControllerChanged)
      ..dispose();
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

  @override
  Widget build(BuildContext context) {
    final c = _controller;
    final submitting = c.status == ActivationStatus.submitting;
    final limitReached = c.status == ActivationStatus.limitReached;
    final canSubmit = c.code.length == _codeLength &&
        !c.isBusy &&
        c.status != ActivationStatus.success &&
        !limitReached;

    return Scaffold(
      appBar: AppBar(title: const Text('Activa tu cuenta')),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                'Ingresa el código de 6 dígitos que te enviamos por SMS.',
              ),
              const SizedBox(height: 8),
              Row(
                children: [
                  const Icon(Icons.timer_outlined, size: 18),
                  const SizedBox(width: 6),
                  Text(
                    'El código vence en '
                    '${formatCountdown(c.remainingSeconds)}',
                    key: const Key('activate-countdown'),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              TextButton(
                key: const Key('activate-how-code'),
                onPressed: _showHowCodeArrives,
                child: const Text('¿Cómo llega el código?'),
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
                  key: const Key('activate-message'),
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.error,
                  ),
                ),
              if (c.infoMessage != null)
                Text(
                  c.infoMessage!,
                  key: const Key('activate-info'),
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.primary,
                  ),
                ),
              const SizedBox(height: 24),
              FilledButton(
                key: const Key('activate-submit'),
                onPressed: canSubmit
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
                    : const Text('Activar cuenta'),
              ),
              const SizedBox(height: 8),
              if (!limitReached)
                TextButton(
                  key: const Key('activate-resend'),
                  onPressed: c.canResend ? _onResendPressed : null,
                  child: Text(
                    c.resendCooldownSeconds > 0
                        ? 'Reenviar en '
                            '${formatCountdown(c.resendCooldownSeconds)}'
                        : 'Reenviar código',
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
        key: Key('otp-$index'),
        controller: _boxes[index],
        focusNode: _nodes[index],
        autofocus: index == 0,
        keyboardType: TextInputType.number,
        textAlign: TextAlign.center,
        style: Theme.of(context).textTheme.headlineSmall,
        // OTP sensible: sin autofill ni sugerencias (ver doc de la clase).
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
