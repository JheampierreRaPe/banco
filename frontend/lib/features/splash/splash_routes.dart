// Rutas del feature `splash` (pantalla de arranque, pagina `0:4`).
//
// Convencion `features/README.md`: el feature expone
// `List<GoRoute> splashRoutes` y NUNCA toca
// `lib/core/router/app_router.dart`; el orquestador hace `...splashRoutes`.
//
// - `/splash`: marca "Eucalipto y Ocre" + espera acotada; al terminar delega
//   en la guarda del router (cliente delgado, `docs/19` §4).
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import 'presentation/splash_page.dart';

/// Seam de temporizacion para tests del router (como
/// `debugDisableLoginAutoTick` en `login_routes.dart`): en `null` la pagina
/// usa la espera real acotada ([kSplashDuration]); en tests se inyecta una
/// espera inmediata (delega en la guarda) o pendiente (se queda en la splash).
@visibleForTesting
SplashWait? debugSplashWaitOverride;

/// Rutas del feature `splash` (ver convencion en `features/README.md`).
final List<GoRoute> splashRoutes = [
  GoRoute(
    path: '/splash',
    builder: (context, state) => SplashPage(wait: debugSplashWaitOverride),
  ),
];
