// Pantalla de login biométrico + PIN de contingencia (F-T40, HU03).
//
// Diseño canónico "Eucalipto y Ocre": `login` (página `0:860`) + `login PIN`
// (página `0:901`) de `pantallas.fig`. Solo tokens de F-T34 (`AppColors`,
// `AppTypography`, `AppSpacing`) y componentes base (`AppCard`,
// `AppPrimaryButton`, `AppGhostButton`).
//
//  - Botón biométrico: usa `local_auth` a través de [BiometricService]
//    (punto de integración `SystemBiometricReader`; en tests se inyecta un
//    fake vía el [LoginController] ya construido).
//  - PIN de 6 casillas estilo `0:901` como contingencia (CA-02).
//  - Temporizador visible de inactividad: al expirar limpia la sesión y
//    vuelve a `/login` (CA-04).
//  - Éxito por cualquiera de las dos vías -> guarda sesión y navega a `/home`
//    (la guarda de `app_router.dart` también lo exigiría).
//  - Errores genéricos sin filtrar (mismo mensaje facial-inválido vs PIN-mal).
//  - Estados obligatorios (docs/20 §7): carga (`busy`), vacío (sin `userRef`),
//    error y contenido.
//
// El [LoginController] lo crea el llamador (ver `login_routes.dart`) y sigue
// siendo suyo: esta página NO lo destruye, solo se desuscribe.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/app_spacing.dart';
import '../../core/theme/app_typography.dart';
import '../../core/widgets/app_button.dart';
import '../../core/widgets/app_card.dart';
import '../../core/widgets/app_version_label.dart';
import 'login_controller.dart';

/// Formatea segundos a `MM:SS` para el contador visible de inactividad.
String formatLoginCountdown(int totalSeconds) {
  final clamped = totalSeconds < 0 ? 0 : totalSeconds;
  final minutes = clamped ~/ 60;
  final seconds = clamped % 60;
  return '${minutes.toString().padLeft(2, '0')}:'
      '${seconds.toString().padLeft(2, '0')}';
}

/// Pantalla de inicio de sesión (F-T40).
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

  /// Referencia del usuario (viaja como `user_ref` al backend; también llega
  /// por `/login?userRef=` tras el restablecimiento de PIN).
  final String userRef;

  /// Identificador del dispositivo (viaja como `device_id`).
  final String deviceId;

  /// Texto que el SO muestra al pedir el biométrico.
  final String biometricReason;

  /// Si es `false` no se inicia el `Timer` real (seam para tests: el
  /// contador se avanza con `LoginController.tick`).
  final bool autoTick;

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  static const int _pinLength = 6;

  late final List<TextEditingController> _boxes;
  late final List<FocusNode> _nodes;
  bool _syncingBoxes = false;

  @override
  void initState() {
    super.initState();
    _boxes = List<TextEditingController>.generate(
      _pinLength,
      (_) => TextEditingController(),
    );
    _nodes = List<FocusNode>.generate(_pinLength, (_) => FocusNode());
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
    for (final c in _boxes) {
      c.dispose();
    }
    for (final n in _nodes) {
      n.dispose();
    }
    super.dispose();
  }

  String get _pinValue => _boxes.map((c) => c.text).join();

  void _onDigitChanged(int index, String value) {
    if (_syncingBoxes) return;
    _syncingBoxes = true;
    try {
      final digits = value.replaceAll(RegExp('[^0-9]'), '');
      if (digits.length > 1) {
        // Pegado: repartir desde esta casilla.
        for (var i = 0; i < digits.length && index + i < _pinLength; i++) {
          _boxes[index + i].text = digits[i];
        }
        final next = index + digits.length;
        if (next < _pinLength) {
          _nodes[next].requestFocus();
        } else {
          _nodes[_pinLength - 1].unfocus();
        }
      } else if (digits.length == 1) {
        if (_boxes[index].text != digits) _boxes[index].text = digits;
        if (index < _pinLength - 1) {
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
    widget.controller.notifyActivity();
    if (mounted) setState(() {});
  }

  Future<void> _submitBiometrics() => widget.controller.loginWithBiometrics(
        userRef: widget.userRef,
        deviceId: widget.deviceId,
        reason: widget.biometricReason,
      );

  Future<void> _submitPin() => widget.controller.loginWithPin(
        userRef: widget.userRef,
        deviceId: widget.deviceId,
        pin: _pinValue,
      );

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    final canSubmitPin =
        _pinValue.length == _pinLength && !c.busy && !c.succeeded;

    return GestureDetector(
      onTap: c.notifyActivity,
      behavior: HitTestBehavior.opaque,
      child: Scaffold(
        backgroundColor: AppColors.surface,
        appBar: AppBar(title: const Text('Inicia sesión')),
        body: SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(AppSpacing.marginMobile),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  'Bienvenido de nuevo',
                  style: AppTypography.headlineMd.copyWith(
                    color: AppColors.onSurface,
                  ),
                ),
                const SizedBox(height: AppSpacing.stackSm),
                Text(
                  'Ingresa con tu biometría o tu PIN para continuar.',
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
                const SizedBox(height: AppSpacing.stackMd),
                // Estado vacío: sin `userRef` no hay cuenta que abrir
                // (F-T57): copy vigente + CTA a `/login/device`; el
                // restablecimiento de PIN sigue como opción secundaria
                // ("Olvidé mi PIN" -> `/pin-reset`).
                if (widget.userRef.isEmpty)
                  Container(
                    key: const Key('login-empty'),
                    padding: const EdgeInsets.all(AppSpacing.stackMd),
                    decoration: BoxDecoration(
                      color: AppColors.warningContainer,
                      borderRadius: BorderRadius.circular(AppRadii.md),
                    ),
                    child: Text(
                      'Falta identificar tu cuenta. Crea tu cuenta o '
                      'inicia sesión en este dispositivo para continuar.',
                      style: AppTypography.bodyMd.copyWith(
                        color: AppColors.onWarningContainer,
                      ),
                    ),
                  ),
                if (widget.userRef.isEmpty)
                  const SizedBox(height: AppSpacing.stackSm),
                if (widget.userRef.isEmpty)
                  AppSecondaryButton(
                    key: const Key('login-device-start'),
                    label: 'Iniciar sesión en este dispositivo',
                    onPressed: () => context.go('/login/device'),
                  ),
                if (widget.userRef.isEmpty)
                  const SizedBox(height: AppSpacing.stackMd),
                // Contenido: tarjeta biométrica (página `0:860`).
                AppCard(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          const Icon(
                            Icons.timer_outlined,
                            size: 18,
                            color: AppColors.secondaryText,
                          ),
                          const SizedBox(width: 6),
                          Text(
                            'Se cerrará por inactividad en '
                            '${formatLoginCountdown(c.remainingSeconds)}',
                            key: const Key('login-inactivity-countdown'),
                            style: AppTypography.bodyMd.copyWith(
                              color: AppColors.secondaryText,
                            ),
                          ),
                        ],
                      ),
                      // Boton biometrico (pagina `0:860`, F-T49): solo si el
                      // servidor informo consentimiento (`biometric_enabled`
                      // sincronizado tras el login con PIN, E1-T39). Sin
                      // consentimiento conocido se oculta y queda el PIN.
                      if (c.biometricEnabled == true) ...[
                        const SizedBox(height: AppSpacing.stackMd),
                        AppPrimaryButton(
                          key: const Key('login-biometric-button'),
                          label: 'Ingresar con biometría',
                          icon: Icons.face,
                          loading: c.busy,
                          onPressed: c.busy ? null : _submitBiometrics,
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(height: AppSpacing.stackMd),
                Row(
                  children: [
                    const Expanded(
                      child: Divider(color: AppColors.divider),
                    ),
                    Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: AppSpacing.stackSm + AppSpacing.unit,
                      ),
                      child: Text(
                        'o continúa con tu PIN',
                        style: AppTypography.bodyMd.copyWith(
                          color: AppColors.secondaryText,
                        ),
                      ),
                    ),
                    const Expanded(
                      child: Divider(color: AppColors.divider),
                    ),
                  ],
                ),
                const SizedBox(height: AppSpacing.stackMd),
                // Contenido: tarjeta PIN de 6 casillas (página `0:901`).
                AppCard(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'PIN de 6 dígitos',
                        style: AppTypography.labelMd.copyWith(
                          color: AppColors.primary,
                        ),
                      ),
                      const SizedBox(height: AppSpacing.stackSm),
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          for (var i = 0; i < _pinLength; i++) _pinBox(i),
                        ],
                      ),
                      const SizedBox(height: AppSpacing.stackSm),
                      Text(
                        'Nunca compartas tu PIN con nadie.',
                        style: AppTypography.bodyMd.copyWith(
                          color: AppColors.secondaryText,
                        ),
                      ),
                      const SizedBox(height: AppSpacing.stackMd),
                      AppPrimaryButton(
                        key: const Key('login-pin-submit'),
                        label: 'Ingresar con PIN',
                        loading: c.busy,
                        onPressed: canSubmitPin ? _submitPin : null,
                      ),
                    ],
                  ),
                ),
                // Estado de error (docs/20 §7): mensaje genérico único.
                if (c.errorMessage != null) ...[
                  const SizedBox(height: AppSpacing.stackMd),
                  Container(
                    padding: const EdgeInsets.all(AppSpacing.stackMd),
                    decoration: BoxDecoration(
                      color: AppColors.errorContainer,
                      borderRadius: BorderRadius.circular(AppRadii.md),
                    ),
                    child: Text(
                      c.errorMessage!,
                      key: const Key('login-message'),
                      style: AppTypography.bodyMd.copyWith(
                        color: AppColors.onErrorContainer,
                      ),
                    ),
                  ),
                ],
                if (c.infoMessage != null) ...[
                  const SizedBox(height: AppSpacing.stackMd),
                  Container(
                    padding: const EdgeInsets.all(AppSpacing.stackMd),
                    decoration: BoxDecoration(
                      color: AppColors.primaryFixed,
                      borderRadius: BorderRadius.circular(AppRadii.md),
                    ),
                    child: Text(
                      c.infoMessage!,
                      key: const Key('login-info'),
                      style: AppTypography.bodyMd.copyWith(
                        color: AppColors.onPrimaryFixed,
                      ),
                    ),
                  ),
                ],
                const SizedBox(height: AppSpacing.stackSm),
                // Enlace aditivo de restablecimiento de PIN (F-T43, relabel
                // F-T57 "Olvidé mi PIN"): navega a `/pin-reset` sin tocar la
                // lógica de biometría/PIN.
                AppGhostButton(
                  key: const Key('login-pin-reset-link'),
                  label: 'Olvidé mi PIN',
                  onPressed: () => context.go('/pin-reset'),
                ),
                AppGhostButton(
                  key: const Key('login-signup-link'),
                  label: '¿Aún no tienes cuenta? Crear cuenta',
                  onPressed: () => context.go('/welcome'),
                ),
                // Versión visible del build (F-T30): discreta, al pie.
                const SizedBox(height: AppSpacing.stackMd),
                const Center(child: AppVersionLabel()),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _pinBox(int index) {
    final filled = _boxes[index].text.isNotEmpty;
    return Expanded(
      child: Padding(
        padding: EdgeInsets.only(
          right: index == _pinLength - 1 ? 0 : AppSpacing.stackSm,
        ),
        child: TextField(
          key: Key('login-pin-$index'),
          controller: _boxes[index],
          focusNode: _nodes[index],
          autofocus: index == 0,
          keyboardType: TextInputType.number,
          textAlign: TextAlign.center,
          obscureText: true,
          style: AppTypography.headlineSm.copyWith(
            color: AppColors.onSurface,
          ),
          // PIN sensible: sin sugerencias ni autocorrección (docs/16
          // reglas 7 y 10); nunca se loguea.
          enableSuggestions: false,
          autocorrect: false,
          inputFormatters: <TextInputFormatter>[
            FilteringTextInputFormatter.digitsOnly,
            LengthLimitingTextInputFormatter(_pinLength),
          ],
          decoration: InputDecoration(
            filled: true,
            fillColor: AppColors.surfaceContainerLowest,
            contentPadding: const EdgeInsets.symmetric(
              vertical: AppSpacing.stackMd,
            ),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: BorderSide(
                color:
                    filled ? AppColors.primary : AppColors.outlineVariant,
                width: filled ? 2 : 1,
              ),
            ),
            focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: const BorderSide(
                color: AppColors.primary,
                width: 2,
              ),
            ),
          ),
          onChanged: (value) => _onDigitChanged(index, value),
        ),
      ),
    );
  }
}
