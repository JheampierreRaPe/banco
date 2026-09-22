import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/app_spacing.dart';
import '../../core/widgets/app_button.dart';
import '../../core/widgets/app_version_label.dart';

/// Pantalla unica de onboarding (F-T36, decision SCR-005).
///
/// Reemplaza `welcome_page.dart` + `entry_page.dart`: un [PageView] con los
/// 3 slides del mockup `pantallas.fig` (paginas `0:16`, `0:53`, `0:87`),
/// indicador de pagina y dos acciones:
///
/// - Primaria "Crear mi cuenta" -> `/kyc`.
/// - Secundaria "Ya tengo cuenta · Iniciar sesión" -> `/recovery`.
///
/// Cliente delgado: solo presenta y navega; sin logica de negocio, red,
/// estado ni persistencia. Estilos solo con tokens/componentes de F-T34
/// ([AppColors], [AppSpacing], [AppRadii], tema `Inter`, [AppPrimaryButton],
/// [AppSecondaryButton], [AppVersionLabel]).
class OnboardingPage extends StatefulWidget {
  const OnboardingPage({super.key});

  /// Tamano del medallon ilustrativo (derivado del ritmo de 4px de F-T34:
  /// `stackXl * 4` = 192). Constante nombrada de la frontera (patron del fix
  /// F-T34): ningun numero magico inline.
  static const double illustrationDiameter = AppSpacing.stackXl * 4;

  /// Tamano del icono interior del medallon (token `stackLg`).
  static const double illustrationIconSize = AppSpacing.stackLg;

  /// Ancho del punto activo del indicador (`stackMd + stackXs` = 24).
  static const double activeDotWidth =
      AppSpacing.stackMd + AppSpacing.stackXs;

  /// Tamano del punto inactivo y alto de ambos (`stackXs` = 4).
  static const double dotSize = AppSpacing.stackXs;

  @override
  State<OnboardingPage> createState() => _OnboardingPageState();
}

class _OnboardingSlide {
  const _OnboardingSlide({
    required this.title,
    required this.body,
    required this.icon,
  });

  final String title;
  final String body;
  final IconData icon;
}

/// Slides exactos del mockup (textos de `pantallas.fig` 0:16, 0:53, 0:87).
const List<_OnboardingSlide> _slides = [
  _OnboardingSlide(
    title: 'Tu banco, sin colas ni papeles',
    body: 'Abre tu cuenta en minutos validando tu DNI y tu rostro.',
    icon: Icons.account_balance_outlined,
  ),
  _OnboardingSlide(
    title: 'Envía y cobra en segundos',
    body:
        'Manda dinero a cualquier persona en CuyCash sin comisiones, '
        'o cobra mostrando tu código.',
    icon: Icons.swap_horiz_outlined,
  ),
  _OnboardingSlide(
    title: 'Recibe desde otras billeteras',
    body:
        'El dinero que te envían desde otras apps llega directo '
        'a tu billetera CuyCash.',
    icon: Icons.account_balance_wallet_outlined,
  ),
];

class _OnboardingPageState extends State<OnboardingPage> {
  late final PageController _controller;
  int _index = 0;

  @override
  void initState() {
    super.initState();
    _controller = PageController();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final colorScheme = Theme.of(context).colorScheme;
    final textTheme = Theme.of(context).textTheme;
    return Scaffold(
      backgroundColor: colorScheme.surface,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(
            horizontal: AppSpacing.marginMobile,
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const SizedBox(height: AppSpacing.stackLg),
              Expanded(
                child: PageView.builder(
                  key: const Key('onboarding-pageview'),
                  controller: _controller,
                  itemCount: _slides.length,
                  onPageChanged: (i) => setState(() => _index = i),
                  itemBuilder: (context, i) {
                    final slide = _slides[i];
                    return Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Container(
                          key: Key('onboarding-illustration-$i'),
                          width: OnboardingPage.illustrationDiameter,
                          height: OnboardingPage.illustrationDiameter,
                          decoration: BoxDecoration(
                            color: AppColors.primaryContainer,
                            shape: BoxShape.circle,
                            border: Border.all(
                              color: AppColors.secondaryContainer,
                              width: AppSpacing.stackXs / 2,
                            ),
                          ),
                          child: Icon(
                            slide.icon,
                            size: OnboardingPage.illustrationIconSize * 2,
                            color: AppColors.onPrimary,
                          ),
                        ),
                        const SizedBox(height: AppSpacing.stackLg),
                        Text(
                          slide.title,
                          textAlign: TextAlign.center,
                          style: textTheme.headlineMedium?.copyWith(
                            color: AppColors.primary,
                          ),
                        ),
                        const SizedBox(height: AppSpacing.stackSm),
                        Text(
                          slide.body,
                          textAlign: TextAlign.center,
                          style: textTheme.bodyLarge?.copyWith(
                            color: AppColors.secondaryText,
                          ),
                        ),
                      ],
                    );
                  },
                ),
              ),
              Row(
                key: const Key('onboarding-indicator'),
                mainAxisAlignment: MainAxisAlignment.center,
                children: List.generate(_slides.length, (i) {
                  final active = i == _index;
                  return AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    margin: const EdgeInsets.symmetric(
                      horizontal: AppSpacing.stackXs / 2,
                    ),
                    width: active
                        ? OnboardingPage.activeDotWidth
                        : OnboardingPage.dotSize,
                    height: OnboardingPage.dotSize,
                    decoration: BoxDecoration(
                      color: active
                          ? AppColors.primary
                          : AppColors.outlineVariant,
                      borderRadius: BorderRadius.circular(AppRadii.full),
                    ),
                  );
                }),
              ),
              const SizedBox(height: AppSpacing.stackLg),
              AppPrimaryButton(
                key: const Key('onboarding-cta-primary'),
                label: 'Crear mi cuenta',
                onPressed: () => context.go('/kyc'),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              AppSecondaryButton(
                key: const Key('onboarding-cta-secondary'),
                label: 'Ya tengo cuenta · Iniciar sesión',
                onPressed: () => context.go('/recovery'),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              // Version visible del build (F-T30): discreta, al pie.
              const Center(child: AppVersionLabel()),
              const SizedBox(height: AppSpacing.stackSm),
            ],
          ),
        ),
      ),
    );
  }
}
