// Rutas del feature `welcome` (flujo inicial: bienvenida + eleccion).
//
// Convencion `features/README.md`: el feature expone
// `List<GoRoute> welcomeRoutes` y NUNCA toca
// `lib/core/router/app_router.dart`; el orquestador hace `...welcomeRoutes`.
//
// - `/welcome`: bienvenida de primera vez (solo si el flag no se ha visto).
// - `/entry`: eleccion "Iniciar sesión" (`/login`) / "Crear cuenta" (`/kyc`).
//
// El [WelcomeSeenStore] se resuelve via [welcomeSeenStoreFactory] (seam como
// `loginRouteDepsFactory`): el orquestador la fija a la instancia compartida
// con la guarda; en tests se inyecta un [InMemoryWelcomeSeenStore].
library;

import 'package:go_router/go_router.dart';

import 'entry_page.dart';
import 'welcome_page.dart';
import 'welcome_seen_store.dart';

/// Fabrica del store de primera vez (la asigna el orquestador; tests: fake).
typedef WelcomeSeenStoreFactory = WelcomeSeenStore Function();

WelcomeSeenStoreFactory? welcomeSeenStoreFactory;

WelcomeSeenStore _resolveStore() =>
    welcomeSeenStoreFactory?.call() ?? InMemoryWelcomeSeenStore();

/// Rutas del feature `welcome` (ver convencion en `features/README.md`).
final List<GoRoute> welcomeRoutes = [
  GoRoute(
    path: '/welcome',
    builder: (context, state) => WelcomePage(seen: _resolveStore()),
  ),
  GoRoute(
    path: '/entry',
    builder: (context, state) => const EntryPage(),
  ),
];
