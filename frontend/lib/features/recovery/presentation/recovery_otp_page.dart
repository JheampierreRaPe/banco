// Pantalla `/recovery/otp`: ingresa el código de 6 dígitos y continúa al login.
//
// Diseño canónico "Eucalipto y Ocre" (F-T42, página `recovery (otp)` +
// `docs/design/recovery-otp.png`): solo tokens de F-T34 (`AppColors`,
// `AppTypography`, `AppSpacing`) y componentes base (`AppCard`,
// `AppPrimaryButton`, `AppGhostButton`).
//
// Reusa el patrón de 6 casillas (pegado repartido, avance automático,
// autofill/sugerencias desactivados):
// - Cuenta atrás de vigencia (`ttl_seconds`) y reenvío con cooldown
//   (`resend_wait_seconds`) que vuelve a llamar a `request`.
// - Submit deshabilitado hasta completar el código, sin doble envío.
// - `401 INVALID_RECOVERY_CODE` (único error genérico: incorrecto / vencido /
//   agotados) -> mismo mensaje accionable + reintentar/reenviar.
// - `429 RATE_LIMITED` -> mensaje de espera.
// - E1-T33/SCR-005: al éxito la página navega a `/login?userRef=<user_ref>`
//   (verify YA NO abre sesión; la única sesión la abre el login).
// - El OTP nunca se loguea ni queda en el autofill del SO (docs/16 reglas
//   7 y 10).
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_button.dart';
import '../../../core/widgets/app_card.dart';
import '../recovery_controller.dart';

/// Pantalla de verificación del código de recuperación (F-T40).
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
      // Verify YA NO abre sesión (E1-T33): se continúa al login con el
      // `user_ref` devuelto para abrir ahí la única sesión.
      final userRef = Uri.encodeComponent(
        _controller.verifiedUserRef ?? '',
      );
      context.go('/login?userRef=$userRef');
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
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Verifica tu correo')),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(AppSpacing.marginMobile),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'Ingresa el código',
                style: AppTypography.headlineMd.copyWith(
                  color: AppColors.onSurface,
                ),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                'Lo enviamos a ${maskEmail(widget.email)}. '
                'Si está registrado, llegará en segundos.',
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                RecoveryOtpController.neutralMessage,
                key: const Key('recovery-otp-info'),
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              // Contenido: tarjeta del código (docs/20 §6: card blanca,
              // radio 16, sombra).
              AppCard(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Código de 6 dígitos',
                      style: AppTypography.labelMd.copyWith(
                        color: AppColors.primary,
                      ),
                    ),
                    const SizedBox(height: AppSpacing.stackSm),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        for (var i = 0; i < _codeLength; i++) _otpBox(i),
                      ],
                    ),
                    const SizedBox(height: AppSpacing.stackSm),
                    // Estado de cuenta atrás (vigencia del OTP).
                    Row(
                      children: [
                        const Icon(
                          Icons.timer_outlined,
                          size: 18,
                          color: AppColors.secondaryText,
                        ),
                        const SizedBox(width: 6),
                        Text(
                          c.resendCooldownSeconds > 0
                              ? 'Reenviar código en '
                                  '${formatRecoveryCountdown(c.resendCooldownSeconds)}'
                              : 'El código vence en '
                                  '${formatRecoveryCountdown(c.remainingSeconds)}',
                          key: const Key('recovery-otp-countdown'),
                          style: AppTypography.bodyMd.copyWith(
                            color: AppColors.secondaryText,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              // Franja informativa: al verificar se continúa al login.
              Container(
                padding: const EdgeInsets.all(AppSpacing.stackMd),
                decoration: BoxDecoration(
                  color: AppColors.primaryFixed,
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Icon(
                      Icons.info_outline,
                      size: 20,
                      color: AppColors.onPrimaryFixed,
                    ),
                    const SizedBox(width: AppSpacing.stackSm),
                    Expanded(
                      child: Text(
                        'Al verificar continuarás al inicio de sesión. '
                        'No se abre sesión aquí.',
                        style: AppTypography.bodyMd.copyWith(
                          color: AppColors.onPrimaryFixed,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              // Estado de error (docs/20 §7).
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
                    key: const Key('recovery-otp-message'),
                    style: AppTypography.bodyMd.copyWith(
                      color: AppColors.onErrorContainer,
                    ),
                  ),
                ),
              ],
              const SizedBox(height: AppSpacing.stackLg),
              // Estado de carga: el botón primario muestra spinner.
              AppPrimaryButton(
                key: const Key('recovery-otp-submit'),
                label: 'Verificar',
                loading: submitting,
                onPressed: c.canSubmit
                    ? () async {
                        await _controller.submit(_controller.code);
                      }
                    : null,
              ),
              const SizedBox(height: AppSpacing.stackSm),
              AppGhostButton(
                key: const Key('recovery-otp-resend'),
                label: c.resendCooldownSeconds > 0
                    ? 'Reenviar en '
                        '${formatRecoveryCountdown(c.resendCooldownSeconds)}'
                    : 'Reenviar código',
                onPressed: c.canResend
                    ? () async {
                        await _controller.resend();
                      }
                    : null,
              ),
              if (c.isResending)
                const Center(
                  key: Key('recovery-otp-resending'),
                  child: Padding(
                    padding: EdgeInsets.only(top: AppSpacing.stackSm),
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
    final filled = _boxes[index].text.isNotEmpty;
    return Expanded(
      child: Padding(
        padding: EdgeInsets.only(
          right: index == _codeLength - 1 ? 0 : AppSpacing.stackSm,
        ),
        child: TextField(
          key: Key('recovery-otp-$index'),
          controller: _boxes[index],
          focusNode: _nodes[index],
          autofocus: index == 0,
          keyboardType: TextInputType.number,
          textAlign: TextAlign.center,
          style: AppTypography.headlineSm.copyWith(
            color: AppColors.onSurface,
          ),
          // OTP sensible: sin autofill ni sugerencias (docs/16 reglas 7 y 10).
          enableSuggestions: false,
          autocorrect: false,
          inputFormatters: <TextInputFormatter>[
            FilteringTextInputFormatter.digitsOnly,
            LengthLimitingTextInputFormatter(_codeLength),
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
                color: filled
                    ? AppColors.primary
                    : AppColors.outlineVariant,
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
