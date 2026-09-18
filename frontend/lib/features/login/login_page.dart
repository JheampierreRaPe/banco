// Pantalla de login biometrico + PIN de contingencia (E1-T16, HU03).
//
//  - Boton grande biometrico: usa `local_auth` a traves de [BiometricService]
//    (punto de integracion `SystemBiometricReader`; en tests se inyecta un
//    fake via el [LoginController] ya construido).
//  - Campo PIN SIEMPRE visible como contingencia (CA-02).
//  - Temporizador visible de inactividad: al expirar limpia la sesion y
//    vuelve a `/login` (CA-04).
//  - Exito por cualquiera de las dos vias -> guarda sesion y navega a `/home`
//    (la guarda de `app_router.dart` tambien lo exigiria).
//  - Errores genericos sin filtrar (mismo mensaje facial-invalido vs PIN-mal).
//
// El [LoginController] lo crea el llamador (ver `login_routes.dart`) y sigue
// siendo suyo: esta pagina NO lo destruye, solo se desuscribe.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import 'login_controller.dart';

/// Formatea segundos a `MM:SS` para el contador visible de inactividad.
String formatLoginCountdown(int totalSeconds) {
  final clamped = totalSeconds < 0 ? 0 : totalSeconds;
  final minutes = clamped ~/ 60;
  final seconds = clamped % 60;
  return '${minutes.toString().padLeft(2, '0')}:'
      '${seconds.toString().padLeft(2, '0')}';
}

/// Pantalla de inicio de sesion (E1-T16).
class LoginPage extends StatefulWidget {
  const LoginPage({
    super.key,
    required this.controller,
    required this.userRef,
    required this.deviceId,
    this.biometricReason = 'Confirma tu identidad para ingresar',
    this.autoTick = true,
  });

  /// Controlador delgado (propiedad del llamador).
  final LoginController controller;

  /// Referencia del usuario (viaja como `user_ref` al backend).
  final String userRef;

  /// Identificador del dispositivo (viaja como `device_id`).
  final String deviceId;

  /// Texto que el SO muestra al pedir el biometrico.
  final String biometricReason;

  /// Si es `false` no se inicia el `Timer` real (seam para tests: el
  /// contador se avanza con `LoginController.tick`).
  final bool autoTick;

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  late final TextEditingController _pin;

  @override
  void initState() {
    super.initState();
    _pin = TextEditingController();
    widget.controller.addListener(_onControllerChanged);
    widget.controller.startInactivityTimer(
      autoTick: widget.autoTick,
      onExpired: () async {
        if (mounted) context.go('/login');
      },
    );
  }

  void _onControllerChanged() {
    if (!mounted) return;
    if (widget.controller.succeeded) {
      context.go('/home');
      return;
    }
    setState(() {});
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onControllerChanged);
    _pin.dispose();
    super.dispose();
  }

  Future<void> _submitBiometrics() => widget.controller.loginWithBiometrics(
        userRef: widget.userRef,
        deviceId: widget.deviceId,
        reason: widget.biometricReason,
      );

  Future<void> _submitPin() => widget.controller.loginWithPin(
        userRef: widget.userRef,
        deviceId: widget.deviceId,
        pin: _pin.text,
      );

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    final theme = Theme.of(context);

    return GestureDetector(
      onTap: c.notifyActivity,
      behavior: HitTestBehavior.opaque,
      child: Scaffold(
        appBar: AppBar(title: const Text('Inicia sesión')),
        body: SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Icon(
                  Icons.fingerprint,
                  size: 72,
                  color: theme.colorScheme.primary,
                ),
                const SizedBox(height: 8),
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    const Icon(Icons.timer_outlined, size: 18),
                    const SizedBox(width: 6),
                    Text(
                      'Se cerrará por inactividad en '
                      '${formatLoginCountdown(c.remainingSeconds)}',
                      key: const Key('login-inactivity-countdown'),
                    ),
                  ],
                ),
                const SizedBox(height: 24),
                FilledButton.icon(
                  key: const Key('login-biometric-button'),
                  onPressed: c.busy ? null : _submitBiometrics,
                  icon: c.busy
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.face),
                  label: const Text(
                    'Ingresar con biometría',
                    style: TextStyle(fontSize: 18),
                  ),
                  style: FilledButton.styleFrom(
                    minimumSize: const Size.fromHeight(56),
                  ),
                ),
                const SizedBox(height: 16),
                const Row(
                  children: [
                    Expanded(child: Divider()),
                    Padding(
                      padding: EdgeInsets.symmetric(horizontal: 12),
                      child: Text('o continúa con tu PIN'),
                    ),
                    Expanded(child: Divider()),
                  ],
                ),
                const SizedBox(height: 16),
                TextField(
                  key: const Key('login-pin-field'),
                  controller: _pin,
                  obscureText: true,
                  keyboardType: TextInputType.number,
                  // PIN sensible: sin sugerencias ni autocorreccion (docs/16
                  // reglas 7 y 10); nunca se loguea.
                  enableSuggestions: false,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    labelText: 'PIN',
                    border: OutlineInputBorder(),
                  ),
                  onChanged: (_) => c.notifyActivity(),
                ),
                const SizedBox(height: 12),
                FilledButton(
                  key: const Key('login-pin-submit'),
                  onPressed: c.busy ? null : _submitPin,
                  child: const Text('Ingresar con PIN'),
                ),
                const SizedBox(height: 16),
                if (c.errorMessage != null)
                  Text(
                    c.errorMessage!,
                    key: const Key('login-message'),
                    style: TextStyle(color: theme.colorScheme.error),
                  ),
                if (c.infoMessage != null)
                  Text(
                    c.infoMessage!,
                    key: const Key('login-info'),
                    style: TextStyle(color: theme.colorScheme.primary),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
