// Pantalla de arranque "Eucalipto y Ocre" (pagina `0:4` de `pantallas.fig`).
//
// Presentacion pura + espera acotada: muestra la marca y delega en la guarda
// del router (cliente delgado, `docs/19` §4). No comprueba sesion ni
// `user_ref` ni decide el destino: al terminar la espera navega a
// [nextLocation] y la guarda resuelve (onboarding, login o home).
//
// La espera es inyectable ([wait]) para que los tests no usen temporizadores
// reales; por defecto es [kSplashDuration] (acotada, sin espera infinita).
// Estados `docs/20` §7: aplican cargando (indicador de espera) y contenido
// (marca); vacio/error no aplican (sin datos ni red; pantalla efimera).
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';

/// Espera de la splash antes de delegar en la guarda (inyectable en tests).
typedef SplashWait = Future<void> Function();

/// Duracion acotada de la splash en produccion (marca + espera del mockup).
const Duration kSplashDuration = Duration(seconds: 2);

Future<void> _defaultSplashWait() => Future.delayed(kSplashDuration);

/// Splash de arranque (ruta `/splash`, publica pre-sesion).
class SplashPage extends StatefulWidget {
  const SplashPage({super.key, this.wait, this.nextLocation = '/home'});

  /// Seam de temporizacion (tests: espera inmediata o pendiente).
  final SplashWait? wait;

  /// Destino neutro que resuelve la guarda (la splash no decide).
  final String nextLocation;

  @override
  State<SplashPage> createState() => _SplashPageState();
}

class _SplashPageState extends State<SplashPage> {
  @override
  void initState() {
    super.initState();
    final wait = widget.wait ?? _defaultSplashWait;
    wait().then((_) {
      if (!mounted) return;
      context.go(widget.nextLocation);
    });
  }

  @override
  Widget build(BuildContext context) {
    // Pagina `0:4`: fondo eucalipto (`primary-container`), monograma y
    // wordmark claros, barra de espera ocre y pie "Banca digital · Perú".
    // Solo tokens de F-T34 (`AppColors`/`AppSpacing`/`AppTypography`/
    // `AppRadii`); las medidas de composicion (medallon 72, barra 120x3)
    // vienen del mockup (nodos `0:8`, `0:11`/`0:12`).
    return Scaffold(
      backgroundColor: AppColors.primaryContainer,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(AppSpacing.containerPadding),
          child: Column(
            children: [
              Expanded(
                child: Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      // Monograma de marca (mockup nodo `0:8`, 72px):
                      // medallon ocre con las iniciales.
                      Container(
                        key: const Key('splash-brand'),
                        width: 72,
                        height: 72,
                        decoration: const BoxDecoration(
                          color: AppColors.secondaryContainer,
                          shape: BoxShape.circle,
                        ),
                        alignment: Alignment.center,
                        child: Text(
                          'CC',
                          style: AppTypography.headlineSm.copyWith(
                            color: AppColors.onSecondaryContainer,
                          ),
                        ),
                      ),
                      const SizedBox(height: AppSpacing.stackMd),
                      Text(
                        'CuyCash',
                        style: AppTypography.headlineLgMobile.copyWith(
                          color: AppColors.onPrimary,
                        ),
                      ),
                      const SizedBox(height: AppSpacing.stackLg),
                      // Espera (mockup nodos `0:11`/`0:12`, 120x3): pista
                      // clara translucida + avance ocre, indeterminado.
                      ClipRRect(
                        borderRadius:
                            BorderRadius.circular(AppRadii.full),
                        child: SizedBox(
                          key: const Key('splash-progress'),
                          width: 120,
                          child: LinearProgressIndicator(
                            minHeight: 3,
                            backgroundColor: AppColors.onPrimary
                                .withValues(alpha: 0.18),
                            valueColor:
                                const AlwaysStoppedAnimation<Color>(
                              AppColors.secondaryContainer,
                            ),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              Text(
                'Banca digital · Perú',
                style: AppTypography.labelSm.copyWith(
                  color: AppColors.onPrimary.withValues(alpha: 0.55),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
