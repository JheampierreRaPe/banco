// Paso final del restablecimiento: "PIN restablecido" (F-T43).
//
// El `user_ref` ya se persistió best-effort en el paso de confirmación
// (`PinResetConfirmController`); aquí se reintenta si faltara, sin bloquear.
// Este flujo NO abre sesión: solo navega a `/login?userRef=<user_ref>`;
// nunca a `/home` ni se guardan tokens.
//
// Cliente delgado (docs/19): solo muestra el resultado que confirmó el
// backend y navega al login.
//
// Estados (docs/20 §7): cargando (persistiendo, `LoadingView`), contenido
// (éxito), vacío (sin `userRef`, `EmptyView`) y error best-effort (aviso sin
// bloquear, con reintento de guardado).
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/session/session_identity_store.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_button.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/loading_view.dart';

/// Diámetros de los halos concéntricos del check de éxito (mismo lenguaje
/// del success de F-T39: círculos de 144/120/96 sobre `primary-container`).
const double kPinResetHaloOuter = 144;
const double kPinResetHaloMiddle = 120;
const double kPinResetBadgeDiameter = 96;

/// PIN restablecido (fin del flujo `/pin-reset`).
class PinResetSuccessPage extends StatefulWidget {
  const PinResetSuccessPage({
    super.key,
    required this.userRef,
    this.identity,
  });

  /// Referencia devuelta por `POST /auth/pin-reset` (query `userRef`).
  final String userRef;

  /// Store de identidad inyectable (tests). Por defecto, el fijado por el
  /// orquestador vía [sessionIdentityStoreFactory].
  final SessionIdentityStore? identity;

  @override
  State<PinResetSuccessPage> createState() => _PinResetSuccessPageState();
}

class _PinResetSuccessPageState extends State<PinResetSuccessPage> {
  bool _saving = true;

  @override
  void initState() {
    super.initState();
    _persistUserRef();
  }

  /// Persiste el `user_ref` UNA vez (best-effort). Sin referencia no hay
  /// nada que persistir (estado vacío).
  Future<void> _persistUserRef() async {
    if (widget.userRef.isEmpty) {
      if (mounted) setState(() => _saving = false);
      return;
    }
    final store = widget.identity ?? sessionIdentityStoreFactory?.call();
    if (store == null) {
      if (mounted) setState(() => _saving = false);
      return;
    }
    try {
      await store.saveUserRef(widget.userRef);
    } catch (_) {
      // Best-effort: un fallo del secure storage no bloquea ni inventa el
      // `user_ref`; se avisa y se continúa.
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            'No pudimos guardar tu identificador en este dispositivo, '
            'pero puedes continuar.',
          ),
        ),
      );
    }
    if (mounted) setState(() => _saving = false);
  }

  /// Continúa al login con el `userRef` (sin abrir sesión aquí).
  void _goToLogin() {
    final ref = Uri.encodeComponent(widget.userRef);
    context.go('/login?userRef=$ref');
  }

  @override
  Widget build(BuildContext context) {
    if (widget.userRef.isEmpty) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: EmptyView(
            message: 'Falta la referencia de tu restablecimiento. '
                'Vuelve a intentarlo.',
            actionLabel: 'Volver al inicio',
            onAction: () => context.go('/pin-reset'),
          ),
        ),
      );
    }
    if (_saving) {
      return const Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: LoadingView(message: 'Guardando tu acceso…'),
        ),
      );
    }
    return Scaffold(
      backgroundColor: AppColors.surface,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(AppSpacing.stackLg),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Spacer(),
              Center(
                child: Stack(
                  alignment: Alignment.center,
                  children: [
                    Container(
                      width: kPinResetHaloOuter,
                      height: kPinResetHaloOuter,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: AppColors.primaryContainer.withValues(
                          alpha: 0.06,
                        ),
                      ),
                    ),
                    Container(
                      width: kPinResetHaloMiddle,
                      height: kPinResetHaloMiddle,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: AppColors.primaryContainer.withValues(
                          alpha: 0.12,
                        ),
                      ),
                    ),
                    Container(
                      width: kPinResetBadgeDiameter,
                      height: kPinResetBadgeDiameter,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: AppColors.primaryContainer,
                        boxShadow: AppShadows.cardList,
                      ),
                      child: const Icon(
                        Icons.check,
                        size: AppSpacing.stackXl,
                        color: AppColors.onPrimary,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: AppSpacing.stackLg),
              Text(
                '¡Tu PIN quedó restablecido!',
                textAlign: TextAlign.center,
                style: AppTypography.headlineMd.copyWith(
                  color: AppColors.primary,
                ),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                'Ya puedes iniciar sesión\ncon tu PIN nuevo.',
                textAlign: TextAlign.center,
                style: AppTypography.bodyLg.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const Spacer(),
              AppPrimaryButton(
                key: const Key('pin-reset-success-login'),
                label: 'Ir a iniciar sesión',
                onPressed: _goToLogin,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
