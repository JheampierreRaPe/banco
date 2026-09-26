import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/session/session_identity_store.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../biometrics/biometric_reader.dart';
import '../data/biometric_consent_service.dart';

/// Pantalla `opciones-usuario` ("Mi perfil", F-T52, HU03).
///
/// Replica la estructura del fig (`pantallas.fig`, page `0:2903`, root
/// `FRAME opciones-usuario 0:2904`): topbar, hero, secciones y bottom-nav con
/// los textos del mockup y tokens del design system "Eucalipto y Ocre".
/// Solo dos acciones son funcionales:
///
/// - Switch de biometria (fila `0:2959`, `Key('biometric-switch')`): valor
///   inicial de `SessionIdentityStore.biometricEnabled` (F-T49); al cambiar
///   llama a `POST /auth/biometric/consent` (E1-T39) y refleja el
///   `biometric_enabled` devuelto (autoridad del servidor). Un fallo revierte
///   el switch y muestra un mensaje neutro con reintento.
/// - "Cerrar sesion" (`0:3032`, `Key('profile-logout')`): ejecuta [onLogout]
///   (en produccion `SessionService.logout()`, F-T02) y la guarda del router
///   redirige a `/login` o `/welcome`.
///
/// Todo lo demas (ayuda, editar, resto de filas, bottom-nav) es visual y no
/// dispara navegacion, endpoint ni cambio de estado (CA-06). Excepcion
/// declarada: el "volver" del topbar (`Key('profile-back')`) es cromo
/// navegable (`context.pop()`) y no cuenta como fila visual.
///
/// [consentService], [identity], [reader] y [onLogout] son inyectables para
/// tests. Sin servicio de consentimiento el switch muestra un error neutro.
class ProfilePage extends StatefulWidget {
  const ProfilePage({
    super.key,
    this.consentService,
    this.identity,
    this.reader,
    this.onLogout,
  });

  final BiometricConsentServiceBase? consentService;
  final SessionIdentityStore? identity;
  final BiometricReader? reader;
  final Future<void> Function()? onLogout;

  @override
  State<ProfilePage> createState() => _ProfilePageState();
}

class _ProfilePageState extends State<ProfilePage> {
  late bool _enabled;
  bool? _deviceAvailable;
  bool _sending = false;
  bool _loggingOut = false;
  String? _error;
  bool _lastIntent = false;

  @override
  void initState() {
    super.initState();
    _enabled = widget.identity?.biometricEnabled ?? false;
    _checkDevice();
  }

  Future<void> _checkDevice() async {
    final reader = widget.reader;
    if (reader == null) {
      if (mounted) setState(() => _deviceAvailable = true);
      return;
    }
    final available = await reader.isAvailable();
    if (mounted) setState(() => _deviceAvailable = available);
  }

  Future<void> _setConsent(bool value) async {
    if (_sending) return;
    final service = widget.consentService;
    if (service == null) {
      if (mounted) {
        setState(() {
          _error =
              'Biometria no disponible en este momento. Intentalo mas tarde.';
        });
      }
      return;
    }
    _lastIntent = value;
    setState(() {
      _sending = true;
      _error = null;
    });
    try {
      // El servidor es la autoridad: se refleja lo devuelto, no el optimismo.
      final serverValue = await service.setConsent(enabled: value);
      await widget.identity?.saveBiometricEnabled(serverValue);
      if (mounted) setState(() => _enabled = serverValue);
    } catch (_) {
      // Revierte: el switch conserva el ultimo valor confirmado por el
      // servidor y se muestra un mensaje neutro con reintento.
      if (mounted) {
        setState(() {
          _error = 'No se pudo actualizar la biometria. Intentalo de nuevo.';
        });
      }
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  Future<void> _logout() async {
    if (_loggingOut) return;
    setState(() => _loggingOut = true);
    try {
      await widget.onLogout?.call();
    } catch (_) {
      // `onLogout` es best-effort (F-T02): un fallo no debe propagar ni
      // romper la pantalla; se avisa con un mensaje neutro (sin tecnicismos).
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('No se pudo cerrar la sesion. Intentalo de nuevo.'),
          ),
        );
      }
    } finally {
      // La guarda del router reacciona al cambio de sesion y redirige a
      // `/login` o `/welcome`; aqui no se navega manualmente.
      if (mounted) setState(() => _loggingOut = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Scaffold(
      backgroundColor: AppColors.surfaceContainerLow,
      body: SafeArea(
        child: Column(
          children: [
            _TopBar(onBack: () => context.pop()),
            Expanded(
              child: ListView(
                padding: const EdgeInsets.all(AppSpacing.marginMobile),
                children: [
                  const _HeroCard(),
                  const SizedBox(height: AppSpacing.stackMd),
                  _Section(
                    label: 'TUS DATOS',
                    child: _StaticRow(
                      icon: Icons.person_outline,
                      title: 'Editar información de contacto',
                      subtitle: 'Teléfono, correo y dirección fiscal',
                    ),
                  ),
                  const SizedBox(height: AppSpacing.stackMd),
                  _Section(
                    label: 'SEGURIDAD',
                    trailing: Text(
                      'Protección activa',
                      style: AppTypography.labelMd.copyWith(
                        color: scheme.primary,
                      ),
                    ),
                    child: Column(
                      children: [
                        _StaticRow(
                          icon: Icons.credit_card_outlined,
                          iconBackground: AppColors.tertiaryFixed,
                          iconColor: AppColors.onTertiaryFixed,
                          title: 'Bloquear tarjeta temporalmente',
                          subtitle: 'Apaga compras físicas u online',
                          trailing: _VisualToggle(value: false),
                        ),
                        const Divider(height: 1),
                        _BiometricRow(
                          enabled: _enabled,
                          busy: _sending || _deviceAvailable == null,
                          available: (_deviceAvailable ?? true) &&
                              widget.reader != null,
                          showNoDeviceHint: (_deviceAvailable ?? true) ==
                                  false &&
                              widget.reader != null,
                          error: _error,
                          onChanged: _setConsent,
                          onRetry: () => _setConsent(_lastIntent),
                        ),
                        const Divider(height: 1),
                        const _StaticRow(
                          icon: Icons.lock_outline,
                          title: 'Cambiar PIN y contraseñas',
                          subtitle: 'Clave digital y código de 4 dígitos',
                        ),
                        const Divider(height: 1),
                        const _StaticRow(
                          icon: Icons.people_outline,
                          iconBackground: AppColors.secondaryFixed,
                          title: 'Cuentas asociadas',
                          subtitle: 'Cuentas bancarias del usuario',
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: AppSpacing.stackMd),
                  _Section(
                    label: 'SABER MÁS',
                    child: Column(
                      children: [
                        _StaticRow(
                          icon: Icons.card_giftcard_outlined,
                          iconBackground: AppColors.secondaryFixed,
                          title: 'Bonificaciones y beneficios',
                          subtitle: 'Cada día nuevas promociones',
                          trailing: Container(
                            padding: const EdgeInsets.symmetric(
                              horizontal: AppSpacing.stackSm,
                              vertical: AppSpacing.stackXs,
                            ),
                            decoration: BoxDecoration(
                              color: AppColors.secondaryContainer,
                              borderRadius:
                                  BorderRadius.circular(AppRadii.sm + 2),
                            ),
                            child: Text(
                              'Nuevo',
                              style: AppTypography.labelMd.copyWith(
                                color: AppColors.onSecondaryContainer,
                              ),
                            ),
                          ),
                        ),
                        const Divider(height: 1),
                        const _StaticRow(
                          icon: Icons.payments_outlined,
                          title: 'Créditos preaprobados',
                          subtitle: 'Línea disponible de hasta S/ 15,000',
                        ),
                        const Divider(height: 1),
                        const _StaticRow(
                          icon: Icons.menu_book_outlined,
                          iconBackground: AppColors.surfaceContainerHigh,
                          title: 'Educación financiera',
                          subtitle: 'Cursos rápidos y tips para ahorrar',
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: AppSpacing.stackMd),
                  SizedBox(
                    width: double.infinity,
                    height: AppSpacing.stackXl,
                    child: ElevatedButton.icon(
                      key: const Key('profile-logout'),
                      onPressed: _loggingOut ? null : _logout,
                      icon: const Icon(Icons.logout, size: 18),
                      label: Text(
                        _loggingOut
                            ? 'Cerrando sesión…'
                            : 'Cerrar sesión',
                        style: AppTypography.bodyLg.copyWith(
                          color: AppColors.onError,
                        ),
                      ),
                      style: ElevatedButton.styleFrom(
                        backgroundColor: AppColors.errorCarmine,
                        foregroundColor: AppColors.onError,
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(AppRadii.md),
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(height: AppSpacing.stackMd),
                ],
              ),
            ),
            const _BottomNav(),
          ],
        ),
      ),
    );
  }
}

/// Topbar del fig (`topbar 0:2905`): "volver" (cromo navegable con
/// `context.pop()`, fuera del alcance visual de CA-06), titulo "Mi perfil" y
/// ayuda (visual).
class _TopBar extends StatelessWidget {
  const _TopBar({required this.onBack});

  final VoidCallback onBack;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      color: AppColors.primary,
      padding: const EdgeInsets.symmetric(
        horizontal: AppSpacing.marginMobile,
        vertical: AppSpacing.stackSm,
      ),
      child: Row(
        children: [
          _CircleIconButton(
            key: const Key('profile-back'),
            icon: Icons.chevron_left,
            tooltip: 'Volver',
            onPressed: onBack,
          ),
          const SizedBox(width: AppSpacing.stackSm),
          Expanded(
            child: Text(
              'Mi perfil',
              style: AppTypography.headlineSm.copyWith(
                color: scheme.onPrimary,
              ),
            ),
          ),
          // Ayuda del fig (`btn-ayuda 0:2910`): visual, sin accion (CA-06).
          _CircleIconButton(
            key: const Key('profile-help'),
            icon: Icons.help_outline,
            tooltip: 'Ayuda',
            onPressed: () {},
          ),
        ],
      ),
    );
  }
}

class _CircleIconButton extends StatelessWidget {
  const _CircleIconButton({
    super.key,
    required this.icon,
    required this.tooltip,
    required this.onPressed,
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      width: 36,
      height: 36,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: scheme.onPrimary.withValues(alpha: 0.15),
      ),
      child: IconButton(
        onPressed: onPressed,
        icon: Icon(icon, size: 20),
        color: scheme.onPrimary,
        tooltip: tooltip,
        padding: EdgeInsets.zero,
      ),
    );
  }
}

/// Hero del fig (`hero 0:2915`): avatar, nombre verificado e ID (visual).
class _HeroCard extends StatelessWidget {
  const _HeroCard();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(AppSpacing.stackSm + 2),
      decoration: BoxDecoration(
        color: AppColors.primary,
        borderRadius: BorderRadius.circular(AppRadii.lg),
        boxShadow: AppShadows.cardList,
      ),
      child: Row(
        children: [
          Container(
            width: AppSpacing.stackXl,
            height: AppSpacing.stackXl,
            decoration: const BoxDecoration(
              shape: BoxShape.circle,
              color: AppColors.secondaryContainer,
            ),
            alignment: Alignment.center,
            child: Text(
              'CM',
              style: AppTypography.bodyLg.copyWith(
                color: AppColors.primary,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Flexible(
                      child: Text(
                        'Carlos Mendoza',
                        overflow: TextOverflow.ellipsis,
                        style: AppTypography.bodyLg.copyWith(
                          color: scheme.onPrimary,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    const SizedBox(width: AppSpacing.stackXs + 2),
                    const Icon(
                      Icons.verified_outlined,
                      size: 16,
                      color: AppColors.secondaryContainer,
                    ),
                  ],
                ),
                const SizedBox(height: AppSpacing.unit / 2),
                Text(
                  'ID Cliente 4829-0193',
                  style: AppTypography.bodyMd.copyWith(
                    color: scheme.onPrimaryContainer,
                  ),
                ),
              ],
            ),
          ),
          // Editar del fig (`btn-editar 0:2925`): visual, sin accion (CA-06).
          Container(
            width: 36,
            height: 36,
            decoration: const BoxDecoration(
              shape: BoxShape.circle,
              color: AppColors.primaryContainer,
            ),
            child: IconButton(
              key: const Key('profile-edit'),
              onPressed: () {},
              icon: const Icon(Icons.edit_outlined, size: 16),
              color: scheme.onPrimary,
              tooltip: 'Editar',
              padding: EdgeInsets.zero,
            ),
          ),
        ],
      ),
    );
  }
}

/// Seccion con etiqueta (`TUS DATOS`, `SEGURIDAD`, `SABER MÁS`) y card.
class _Section extends StatelessWidget {
  const _Section({required this.label, this.trailing, required this.child});

  final String label;
  final Widget? trailing;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                label,
                style: AppTypography.labelMd.copyWith(
                  color: scheme.primary,
                ),
              ),
            ),
            ...[?trailing],
          ],
        ),
        const SizedBox(height: AppSpacing.stackXs + 2),
        AppCard(
          padding: const EdgeInsets.symmetric(
            horizontal: AppSpacing.stackSm + 2,
            vertical: AppSpacing.stackXs,
          ),
          child: child,
        ),
      ],
    );
  }
}

/// Fila estatica del fig (visual, sin `onTap`: CA-06).
class _StaticRow extends StatelessWidget {
  const _StaticRow({
    required this.icon,
    required this.title,
    required this.subtitle,
    this.iconBackground = AppColors.successContainer,
    this.iconColor,
    this.trailing,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final Color iconBackground;
  final Color? iconColor;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: AppSpacing.stackSm),
      child: Row(
        children: [
          Container(
            width: 36,
            height: 36,
            decoration: BoxDecoration(
              color: iconBackground,
              borderRadius: BorderRadius.circular(AppRadii.md - 2),
            ),
            child: Icon(
              icon,
              size: 18,
              color: iconColor ?? scheme.primary,
            ),
          ),
          const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  title,
                  style: AppTypography.bodyLg.copyWith(
                    color: scheme.onSurface,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: AppSpacing.unit / 2),
                Text(
                  subtitle,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
              ],
            ),
          ),
          trailing ??
              Icon(
                Icons.chevron_right,
                color: AppColors.secondaryText,
              ),
        ],
      ),
    );
  }
}

/// Toggle decorativo del fig (`toggle-off 0:2956`): solo referencia visual.
class _VisualToggle extends StatelessWidget {
  const _VisualToggle({required this.value});

  final bool value;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 44,
      height: 24,
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(AppRadii.full),
        color: value
            ? AppColors.primaryContainer
            : AppColors.outlineVariant,
      ),
      alignment: value ? Alignment.centerRight : Alignment.centerLeft,
      padding: const EdgeInsets.all(2),
      child: Container(
        width: 20,
        height: 20,
        decoration: const BoxDecoration(
          shape: BoxShape.circle,
          color: Colors.white,
        ),
      ),
    );
  }
}

/// Fila de biometria del fig (`fila-biometria 0:2959`): UNICA funcional.
///
/// El [Switch] usa `Key('biometric-switch')` (patron de
/// `biometric_offer_page.dart`). Sin dispositivo ([available] false) se
/// deshabilita con explicacion; [error] muestra mensaje neutro + reintento.
class _BiometricRow extends StatelessWidget {
  const _BiometricRow({
    required this.enabled,
    required this.busy,
    required this.available,
    required this.showNoDeviceHint,
    required this.error,
    required this.onChanged,
    required this.onRetry,
  });

  final bool enabled;
  final bool busy;
  final bool available;
  final bool showNoDeviceHint;
  final String? error;
  final ValueChanged<bool> onChanged;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: AppSpacing.stackSm),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 36,
            height: 36,
            decoration: BoxDecoration(
              color: AppColors.successContainer,
              borderRadius: BorderRadius.circular(AppRadii.md - 2),
            ),
            child: Icon(
              Icons.fingerprint,
              size: 18,
              color: scheme.primary,
            ),
          ),
          const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'Ingreso por biometría',
                  style: AppTypography.bodyLg.copyWith(
                    color: scheme.onSurface,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: AppSpacing.unit / 2),
                Text(
                  'Accede rápido con Face ID o huella digital',
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
                if (showNoDeviceHint)
                  Padding(
                    padding: const EdgeInsets.only(top: AppSpacing.stackXs),
                    child: Text(
                      'Tu dispositivo no tiene biometría disponible.',
                      style: AppTypography.bodyMd.copyWith(
                        color: AppColors.secondaryText,
                      ),
                    ),
                  ),
                if (error != null)
                  Padding(
                    padding: const EdgeInsets.only(top: AppSpacing.stackXs),
                    child: Row(
                      children: [
                        Expanded(child: Text(error!)),
                        TextButton(
                          key: const Key('profile-biometric-retry'),
                          onPressed: busy ? null : onRetry,
                          child: const Text('Reintentar'),
                        ),
                      ],
                    ),
                  ),
              ],
            ),
          ),
          Switch(
            key: const Key('biometric-switch'),
            value: enabled,
            activeTrackColor: AppColors.primaryContainer,
            inactiveTrackColor: AppColors.outlineVariant,
            onChanged: (busy || !available) ? null : onChanged,
          ),
        ],
      ),
    );
  }
}

/// Bottom-nav del fig (`bottom-nav 0:3036`, `Perfil` activo): visual (CA-06).
class _BottomNav extends StatelessWidget {
  const _BottomNav();

  @override
  Widget build(BuildContext context) {
    const tabs = [
      (Icons.home_outlined, 'Inicio', false),
      (Icons.add_circle_outline, 'Operar', false),
      (Icons.credit_card_outlined, 'Tarjetas', false),
      (Icons.person_outline, 'Perfil', true),
    ];
    return Container(
      decoration: const BoxDecoration(
        color: AppColors.surfaceContainerLowest,
        border: Border(top: BorderSide(color: AppColors.divider)),
      ),
      padding: const EdgeInsets.symmetric(
        horizontal: AppSpacing.marginMobile,
        vertical: AppSpacing.stackSm,
      ),
      child: Row(
        children: [
          for (final tab in tabs)
            Expanded(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(
                    tab.$1,
                    size: 20,
                    color: tab.$3
                        ? AppColors.primary
                        : AppColors.secondaryText,
                  ),
                  const SizedBox(height: AppSpacing.stackXs),
                  Text(
                    tab.$2,
                    style: AppTypography.labelMd.copyWith(
                      color: tab.$3
                          ? AppColors.primary
                          : AppColors.secondaryText,
                      fontWeight:
                          tab.$3 ? FontWeight.w700 : FontWeight.w500,
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}
