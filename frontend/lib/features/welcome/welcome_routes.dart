// Rutas del feature `welcome` (F-T36: onboarding unico).
//
// Convencion `features/README.md`: el feature expone
// `List<GoRoute> welcomeRoutes` y NUNCA toca
// `lib/core/router/app_router.dart`; el orquestador hace `...welcomeRoutes`.
//
// - `/welcome`: pantalla unica de onboarding (PageView de 3 slides,
//   "Crear mi cuenta" -> `/kyc`, "Ya tengo cuenta · Restablecer PIN" ->
//   `/pin-reset`; `/recovery` y `/recovery/otp` fueron retirados en F-T51
//   y ya no existen). Es el path canonico; `/entry` queda retirado (F-T36):
//   la guarda de `app_router.dart` redirige el legacy `/entry`.
// - F-T37 (SCR-005 d5): la guarda depende solo de
//   `SessionIdentityStore.userRef` (+ sesion); no hay store de bienvenida.
library;

import 'package:go_router/go_router.dart';

import 'onboarding_page.dart';

/// Rutas del feature `welcome` (ver convencion en `features/README.md`).
final List<GoRoute> welcomeRoutes = [
  GoRoute(
    path: '/welcome',
    builder: (context, state) => const OnboardingPage(),
  ),
];
