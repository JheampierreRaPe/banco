// Paso OTP `/pin-reset/otp`: ingresa el código `RECOVERY` y continúa a
// crear el PIN nuevo.
//
// Diseño "Eucalipto y Ocre" (mockup `docs/design/pin-reset.png`: 6 casillas
// de OTP): solo tokens de F-T34 (`AppColors`, `AppTypography`, `AppSpacing`)
// y componentes base (`AppCard`, `AppPrimaryButton`, `AppGhostButton`).
//
// - 6 casillas (pegado repartido, avance automático, autofill/sugerencias
//   desactivados); submit deshabilitado hasta completar, sin doble envío.
// - Cuenta atrás de vigencia (`ttl_seconds`) y reenvío con cooldown
//   (`resend_wait_seconds`) que vuelve a llamar a `requestOtp` (canónico de
//   recovery; nunca `/auth/otp/resend`).
// - El OTP se consume recién en el request final de `pin-reset`: aquí no hay
//   red al avanzar (cliente delgado, docs/19).
// - Estados (docs/20 §7): cargando (`LoadingView`), vacío (sin email, lo
//   resuelve la ruta con `EmptyView`), error (`ErrorView` con reintento) y
//   contenido.
// - El DNI/OTP nunca se loguean ni viajan en la ruta (el DNI sigue en
//   `extra`, memoria).
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_button.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../pin_reset_controllers.dart';
import '../pin_reset_validators.dart';

/// Paso OTP del restablecimiento de PIN (F-T43).
class PinResetOtpPage extends StatefulWidget {
  const PinResetOtpPage({
    super.key,
    required this.email,
    required this.docNumber,
    this.docType = 'DNI',
    required this.controller,
    this.autoTick = true,
  });

  /// Email al que se envió el código (solo se muestra enmascarado).
  final String email;

  /// DNI/RUC capturado en el paso inicial (solo memoria; se propaga en
  /// `extra`). `docType` (`DNI|RUC`, F-T50) viaja igual, nunca en la ruta.
  final String docNumber;

  /// Tipo de documento del paso inicial (default `DNI` preserva F-T43).
  final String docType;

  /// Controlador delgado (propiedad del llamador; ver `pin_reset_routes.dart`).
  final PinResetOtpController controller;

  /// Si es `false` no se inicia el `Timer` real (seam para tests: el contador
  /// se avanza manualmente con `PinResetOtpController.tick`).
  final bool autoTick;

  @override
  State<PinResetOtpPage> createState() => _PinResetOtpPageState();
}

class _PinResetOtpPageState extends State<PinResetOtpPage> {
  static const int _codeLength = 6;

  late final List<TextEditingController> _boxes;
  late final List<FocusNode> _nodes;
  bool _syncingBoxes = false;
  bool _navigated = false;

  PinResetOtpController get _controller => widget.controller;

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
    if (_controller.succeeded && !_navigated) {
      _navigated = true;
      final email = Uri.encodeComponent(widget.email);
      // DNI + código + doc_type viajan SOLO en memoria (`extra`): nunca
      // en la ruta.
      context.go(
        '/pin-reset/new-pin?email=$email',
        extra: PinResetDraft(
          email: widget.email,
          docNumber: widget.docNumber,
          docType: widget.docType,
          code: _controller.code,
        ),
      );
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

  @override
  Widget build(BuildContext context) {
    final c = _controller;
    if (c.isBusy) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        appBar: AppBar(title: const Text('Verifica tu correo')),
        body: SafeArea(
          child: LoadingView(
            message: c.isResending ? 'Enviando código…' : 'Verificando…',
          ),
        ),
      );
    }
    if (c.status == PinResetOtpStatus.error && c.errorMessage != null) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        appBar: AppBar(title: const Text('Verifica tu correo')),
        body: SafeArea(
          child: ErrorView(
            message: c.errorMessage!,
            onRetry: () => c.retry(),
          ),
        ),
      );
    }
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
                'Lo enviamos a ${maskPinResetEmail(widget.email)}. '
                'Si está registrado, llegará en segundos.',
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                PinResetOtpController.neutralMessage,
                key: const Key('pin-reset-otp-info'),
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
                                  '${formatPinResetCountdown(c.resendCooldownSeconds)}'
                              : 'El código vence en '
                                  '${formatPinResetCountdown(c.remainingSeconds)}',
                          key: const Key('pin-reset-otp-countdown'),
                          style: AppTypography.bodyMd.copyWith(
                            color: AppColors.secondaryText,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(height: AppSpacing.stackLg),
              AppPrimaryButton(
                key: const Key('pin-reset-otp-submit'),
                label: 'Continuar',
                onPressed: c.canSubmit
                    ? () async {
                        await _controller.submit(_controller.code);
                      }
                    : null,
              ),
              const SizedBox(height: AppSpacing.stackSm),
              AppGhostButton(
                key: const Key('pin-reset-otp-resend'),
                label: c.resendCooldownSeconds > 0
                    ? 'Reenviar en '
                        '${formatPinResetCountdown(c.resendCooldownSeconds)}'
                    : 'Reenviar código',
                onPressed: c.canResend
                    ? () async {
                        await _controller.resend();
                      }
                    : null,
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
          key: Key('pin-reset-otp-$index'),
          controller: _boxes[index],
          focusNode: _nodes[index],
          autofocus: index == 0,
          keyboardType: TextInputType.number,
          textAlign: TextAlign.center,
          style: AppTypography.headlineSm.copyWith(
            color: AppColors.onSurface,
          ),
          // OTP sensible: sin autofill ni sugerencias (docs/16 reglas 7 y
          // 10); nunca se loguea.
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
