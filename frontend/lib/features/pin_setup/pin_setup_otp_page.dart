// Paso OTP del alta (F-T39): verifica el codigo que llego por correo y crea
// el PIN con `POST /auth/pin/setup`.
//
// Cliente delgado (docs/19): captura el codigo y navega; el backend valida
// y activa. En exito navega a `/registration-success` (ahi se persiste el
// `user_ref`, SCR-005).
//
// Estados (docs/20 §7): contenido (casillas+teclado), cargando
// (verificando/reenviando), error (mensaje accionable + reintento),
// vacio (sin `userRef`/PIN, lo resuelve la ruta con [EmptyView]).
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/core/theme/app_colors.dart';
import 'package:banca_online/core/theme/app_spacing.dart';
import 'package:banca_online/core/theme/app_typography.dart';
import 'package:banca_online/core/widgets/app_button.dart';
import 'package:banca_online/features/activation/activation_service.dart';

import 'pin_flow_widgets.dart';
import 'pin_setup_otp_controller.dart';
import 'pin_setup_service.dart';
import 'pin_setup_validators.dart';

/// Verifica el codigo de activacion (solo email) y crea el PIN.
class PinSetupOtpPage extends StatefulWidget {
  const PinSetupOtpPage({
    super.key,
    required this.userRef,
    required this.pin,
    required this.setupService,
    required this.resendService,
    this.biometricEnabled = false,
    this.identity,
  });

  /// Referencia del usuario (viaja como `user_ref` al backend).
  final String userRef;

  /// PIN creado en los pasos previos (solo memoria, jamas en ruta/logs).
  final String pin;

  /// Consentimiento de la oferta biometrica (fig `0:704`, F-T46): viaja como
  /// `biometric_enabled` en `POST /auth/pin/setup` (default `false`).
  final bool biometricEnabled;

  /// Servicio de creacion de PIN (inyectado; en tests se pasa un fake).
  final PinSetupService setupService;

  /// Servicio de reenvio (MISMO contrato de `activation`, sin duplicar).
  final ActivationService resendService;

  /// Store de identidad para resolver el `device_id` estable al ir al
  /// login. Inyectable en tests; por defecto el fijado por el orquestador.
  final SessionIdentityStore? identity;

  @override
  State<PinSetupOtpPage> createState() => _PinSetupOtpPageState();
}

class _PinSetupOtpPageState extends State<PinSetupOtpPage> {
  late final PinSetupOtpController _controller;
  String _code = '';
  bool _navigated = false;

  @override
  void initState() {
    super.initState();
    _controller = PinSetupOtpController(
      setupService: widget.setupService,
      resendService: widget.resendService,
      userRef: widget.userRef,
      biometricEnabled: widget.biometricEnabled,
    );
    _controller.addListener(_onControllerChanged);
  }

  void _onControllerChanged() {
    if (!mounted) return;
    if (_controller.status == PinSetupOtpStatus.success) {
      _navigateToSuccess();
      return;
    }
    setState(() {});
  }

  void _navigateToSuccess() {
    if (_navigated) return;
    _navigated = true;
    final ref = Uri.encodeComponent(widget.userRef);
    context.go('/registration-success?userRef=$ref');
  }

  /// Al exito tardio (o si el PIN ya existia) navega al login con
  /// `userRef` + `device_id` estable (F-T20).
  Future<void> _navigateToLogin() async {
    if (_navigated) return;
    _navigated = true;
    final store = widget.identity ?? sessionIdentityStoreFactory?.call();
    String deviceId = '';
    try {
      deviceId = await store?.getOrCreateDeviceId() ?? '';
    } catch (_) {
      deviceId = '';
    }
    if (!mounted) return;
    final ref = Uri.encodeComponent(widget.userRef);
    final device = Uri.encodeComponent(deviceId);
    context.go('/login?userRef=$ref&deviceId=$device');
  }

  void _onDigit(String digit) {
    if (_code.length >= kPinLength || _controller.isBusy || _navigated) {
      return;
    }
    setState(() => _code += digit);
    _controller.setCode(_code);
  }

  void _onBackspace() {
    if (_code.isEmpty || _controller.isBusy || _navigated) return;
    setState(() => _code = _code.substring(0, _code.length - 1));
    _controller.setCode(_code);
  }

  Future<void> _onSubmit() async {
    await _controller.submit(widget.pin);
  }

  @override
  void dispose() {
    _controller
      ..removeListener(_onControllerChanged)
      ..dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final c = _controller;
    final submitting = c.status == PinSetupOtpStatus.submitting;
    final canSubmit =
        _code.length == kPinLength && !c.isBusy && !_navigated;

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
                title: 'Revisa tu correo',
                subtitle: PinSetupOtpController.codeHint,
              ),
              const SizedBox(height: AppSpacing.stackLg),
              PinCodeBoxes(value: _code, obscure: false),
              const SizedBox(height: AppSpacing.stackMd),
              if (c.errorMessage != null)
                Text(
                  c.errorMessage!,
                  key: const Key('pin-setup-message'),
                  textAlign: TextAlign.center,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.errorCarmine,
                  ),
                ),
              if (c.infoMessage != null)
                Text(
                  c.infoMessage!,
                  key: const Key('pin-setup-info'),
                  textAlign: TextAlign.center,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.primary,
                  ),
                ),
              const SizedBox(height: AppSpacing.stackMd),
              if (c.isPinAlreadySet)
                AppSecondaryButton(
                  key: const Key('pin-setup-goto-login'),
                  label: 'Ir a iniciar sesión',
                  onPressed: _navigateToLogin,
                )
              else
                AppPrimaryButton(
                  key: const Key('pin-setup-submit'),
                  label: submitting ? 'Verificando…' : 'Verificar código',
                  loading: submitting,
                  onPressed: canSubmit ? _onSubmit : null,
                ),
              const SizedBox(height: AppSpacing.stackSm),
              AppGhostButton(
                key: const Key('pin-setup-resend'),
                label: c.isResending
                    ? 'Enviando…'
                    : 'Enviarme un código nuevo',
                onPressed:
                    c.isBusy ? null : () => _controller.resend(),
              ),
              const SizedBox(height: AppSpacing.stackLg),
              PinKeypad(
                onDigit: _onDigit,
                onBackspace: _onBackspace,
                enabled: !c.isBusy && !_navigated,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
