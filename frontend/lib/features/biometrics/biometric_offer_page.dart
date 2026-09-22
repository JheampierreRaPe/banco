// Pantalla "Acceso biometrico" del cierre del registro (F-T39, fig `0:704`).
//
// OFRECE habilitar el acceso biometrico: es un gate local del dispositivo
// ([BiometricReader], F-T03), no decide seguridad; el fallback a PIN siempre
// existe. Aceptar, rechazar u omitir continuan al OTP sin bloquear el alta.
//
// Cliente delgado (docs/19): solo pide el biometrico del SO y navega.
//
// Estados (docs/20 §7): contenido (oferta + switch), cargando (gate en
// curso), vacio/error (sin `userRef`/PIN, lo resuelve la ruta).
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/app_spacing.dart';
import '../../core/theme/app_typography.dart';
import '../../core/widgets/app_button.dart';
import '../../core/widgets/app_card.dart';
import 'biometric_reader.dart';

/// Ofrece el acceso biometrico (fig `0:704`:
/// "¿Quieres entrar con tu huella?", "Activar acceso biométrico").
/// El boton dice "Continuar" (el fig decia "Finalizar registro", pero en el
/// orden F-T39 aun falta el OTP + success: no se promete un fin que no es).
class BiometricOfferPage extends StatefulWidget {
  const BiometricOfferPage({
    super.key,
    required this.userRef,
    required this.pin,
    this.reader,
  });

  /// Referencia del usuario (query `userRef`).
  final String userRef;

  /// PIN creado en los pasos previos (solo memoria, jamas en ruta/logs).
  final String pin;

  /// Lector biometrico inyectable (tests). Por defecto, el punto de
  /// integracion del SO ([SystemBiometricReader], sin hardware => cae a
  /// PIN sin bloquear).
  final BiometricReader? reader;

  @override
  State<BiometricOfferPage> createState() => _BiometricOfferPageState();
}

class _BiometricOfferPageState extends State<BiometricOfferPage> {
  bool _enabled = true;
  bool _checking = false;
  bool _navigated = false;

  BiometricReader get _reader => widget.reader ?? SystemBiometricReader();

  void _goToOtp() {
    if (_navigated) return;
    _navigated = true;
    final ref = Uri.encodeComponent(widget.userRef);
    context.go('/pin-setup/otp?userRef=$ref', extra: widget.pin);
  }

  /// Continua al OTP. Si el switch esta activo, intenta el gate biometrico
  /// local; cualquier resultado (exito, cancelacion o no disponible) sigue
  /// al OTP: el biometrico nunca bloquea el registro.
  Future<void> _continue() async {
    if (_checking || _navigated) return;
    if (!_enabled) {
      _goToOtp();
      return;
    }
    setState(() => _checking = true);
    try {
      await _reader.authenticate(
        reason: 'Activa el acceso biométrico',
      );
    } catch (_) {
      // Gate local best-effort: un fallo inesperado cae al PIN.
    }
    if (!mounted) return;
    setState(() => _checking = false);
    _goToOtp();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(AppSpacing.stackMd),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  SizedBox(
                    width: AppSpacing.stackXl,
                    height: AppSpacing.stackXl,
                    child: IconButton(
                      key: const Key('biometric-back'),
                      onPressed: () => context.pop(),
                      icon: const Icon(Icons.arrow_back),
                      color: AppColors.primary,
                      iconSize: AppSpacing.stackLg,
                      tooltip: 'Volver',
                    ),
                  ),
                  Expanded(
                    child: Text(
                      'Protege tu cuenta',
                      textAlign: TextAlign.center,
                      style: AppTypography.titleMd.copyWith(
                        color: AppColors.primary,
                      ),
                    ),
                  ),
                  const SizedBox(
                    width: AppSpacing.stackXl,
                    height: AppSpacing.stackXl,
                  ),
                ],
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Row(
                children: [
                  for (var i = 1; i <= 4; i++)
                    Expanded(
                      child: Padding(
                        padding: EdgeInsets.only(
                          right: i == 4 ? 0 : AppSpacing.stackSm,
                        ),
                        child: Container(
                          height: AppSpacing.unit,
                          decoration: BoxDecoration(
                            color: AppColors.primaryContainer,
                            borderRadius: BorderRadius.circular(
                              AppRadii.full,
                            ),
                          ),
                        ),
                      ),
                    ),
                ],
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  'Paso 4 de 4 · Seguridad',
                  style: AppTypography.labelSm.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              Text(
                '¿Quieres entrar con tu huella?',
                style: AppTypography.headlineSm.copyWith(
                  color: AppColors.primary,
                ),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                'Podrás abrir la app y autorizar tus operaciones\n'
                'sin escribir el PIN.',
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: AppSpacing.stackLg),
              AppCard(
                child: Row(
                  children: [
                    Container(
                      width: AppSpacing.stackXl - AppSpacing.stackSm,
                      height: AppSpacing.stackXl - AppSpacing.stackSm,
                      decoration: BoxDecoration(
                        color: AppColors.surfaceContainerHigh,
                        borderRadius: BorderRadius.circular(AppRadii.md - 2),
                      ),
                      child: const Icon(
                        Icons.fingerprint,
                        size: AppSpacing.stackMd + AppSpacing.unit,
                        color: AppColors.primaryContainer,
                      ),
                    ),
                    const SizedBox(width: AppSpacing.stackMd - 2),
                    Expanded(
                      child: Text(
                        'Activar acceso biométrico',
                        style: AppTypography.bodyLg.copyWith(
                          color: AppColors.onSurface,
                        ),
                      ),
                    ),
                    Switch(
                      key: const Key('biometric-switch'),
                      value: _enabled,
                      activeTrackColor: AppColors.primaryContainer,
                      onChanged: _checking
                          ? null
                          : (value) =>
                              setState(() => _enabled = value),
                    ),
                  ],
                ),
              ),
              const Spacer(),
              AppPrimaryButton(
                key: const Key('biometric-continue'),
                label: _checking ? 'Verificando…' : 'Continuar',
                loading: _checking,
                onPressed: _checking ? null : _continue,
              ),
              const SizedBox(height: AppSpacing.stackSm),
              AppGhostButton(
                key: const Key('biometric-skip'),
                label: 'Omitir por ahora',
                onPressed: _checking ? null : _goToOtp,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
