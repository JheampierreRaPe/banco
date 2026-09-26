// Paso final del alta: "registro exitoso" (F-T39, fig `0:740`).
//
// UNICO lugar donde se persiste el `user_ref` (SCR-005/F-T20):
// [SessionIdentityStore.saveUserRef] se invoca aqui con el `userId` del
// resultado KYC. Si el secure storage falla, best-effort: se avisa y no se
// inventa la referencia (el `userRef` ya viaja en la ruta).
//
// Cliente delgado (docs/19): solo muestra el resultado que confirmo el
// backend y navega al login.
//
// Estados (docs/20 §7): cargando (persistiendo), contenido (exito),
// vacio (sin `userRef`), error best-effort (aviso sin bloquear).
//
// Desviacion documentada del fig: la tarjeta de alias/billetera (`0:764`)
// no se muestra porque ningun endpoint del contrato (`docs/05` §6.1)
// devuelve alias ni cuenta en el alta; no se inventan datos.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/session/session_identity_store.dart';
import '../../core/theme/app_colors.dart';
import '../../core/theme/app_spacing.dart';
import '../../core/theme/app_typography.dart';
import '../../core/widgets/app_button.dart';
import '../../core/widgets/empty_view.dart';
import '../../core/widgets/loading_view.dart';

/// Diametros de los halos concentricos del check de exito (fig `0:741`:
/// circulos de 144/120/96 sobre `primary-container`).
const double kSuccessHaloOuter = 144;
const double kSuccessHaloMiddle = 120;
const double kSuccessBadgeDiameter = 96;

/// Registro exitoso (fig `0:740`: "¡Tu cuenta está activa!",
/// "Ya puedes empezar a mover tu dinero con CuyCash.", "Ir a mi cuenta").
class RegistrationSuccessPage extends StatefulWidget {
  const RegistrationSuccessPage({
    super.key,
    required this.userRef,
    this.identity,
  });

  /// Identificador del alta (query `userRef`, del `KycSubmitResult.userId`).
  final String userRef;

  /// Store de identidad inyectable (tests). Por defecto, el fijado por el
  /// orquestador via [sessionIdentityStoreFactory].
  final SessionIdentityStore? identity;

  @override
  State<RegistrationSuccessPage> createState() =>
      _RegistrationSuccessPageState();
}

class _RegistrationSuccessPageState extends State<RegistrationSuccessPage> {
  bool _saving = true;
  bool _persisted = false;

  @override
  void initState() {
    super.initState();
    _persistUserRef();
  }

  /// Persiste el `user_ref` UNA vez (guardado tardio, SCR-005). Sin
  /// referencia no hay nada que persistir (estado vacio).
  Future<void> _persistUserRef() async {
    if (widget.userRef.isEmpty) {
      if (mounted) setState(() => _saving = false);
      return;
    }
    final store =
        widget.identity ?? sessionIdentityStoreFactory?.call();
    if (store == null) {
      if (mounted) setState(() => _saving = false);
      return;
    }
    try {
      await store.saveUserRef(widget.userRef);
      if (mounted) {
        setState(() {
          _saving = false;
          _persisted = true;
        });
      }
    } catch (_) {
      // Best-effort (simetria con el alta previa): un fallo del secure
      // storage no bloquea ni inventa el `user_ref`; se avisa y se continua.
      if (!mounted) return;
      setState(() => _saving = false);
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            'No pudimos guardar tu identificador en este dispositivo, '
            'pero puedes continuar.',
          ),
        ),
      );
    }
  }

  /// Continua al login con `userRef` + `device_id` estable (F-T20).
  Future<void> _goToLogin() async {
    final store =
        widget.identity ?? sessionIdentityStoreFactory?.call();
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

  @override
  Widget build(BuildContext context) {
    if (widget.userRef.isEmpty) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: EmptyView(
            message: 'Falta la referencia de tu registro. '
                'Vuelve a crear tu cuenta.',
            actionLabel: 'Volver al registro',
            onAction: () => context.go('/kyc'),
          ),
        ),
      );
    }
    if (_saving && !_persisted) {
      return const Scaffold(
        backgroundColor: AppColors.surface,
        body: SafeArea(
          child: LoadingView(message: 'Activando tu cuenta…'),
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
                      width: kSuccessHaloOuter,
                      height: kSuccessHaloOuter,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: AppColors.primaryContainer.withValues(
                          alpha: 0.06,
                        ),
                      ),
                    ),
                    Container(
                      width: kSuccessHaloMiddle,
                      height: kSuccessHaloMiddle,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: AppColors.primaryContainer.withValues(
                          alpha: 0.12,
                        ),
                      ),
                    ),
                    Container(
                      width: kSuccessBadgeDiameter,
                      height: kSuccessBadgeDiameter,
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
                '¡Tu cuenta está activa!',
                textAlign: TextAlign.center,
                style: AppTypography.headlineMd.copyWith(
                  color: AppColors.primary,
                ),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                'Ya puedes empezar a mover tu dinero\ncon CuyCash.',
                textAlign: TextAlign.center,
                style: AppTypography.bodyLg.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const Spacer(),
              AppPrimaryButton(
                key: const Key('registration-success-login'),
                label: 'Ir a mi cuenta',
                onPressed: _goToLogin,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
