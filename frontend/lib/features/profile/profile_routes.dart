import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/http/api_client.dart';
import '../../core/session/session_identity_store.dart';
import '../../core/session/session_repository.dart';
import '../../core/session/session_service.dart';
import '../../core/widgets/error_view.dart';
import '../biometrics/biometric_reader.dart';
import 'data/biometric_consent_service.dart';
import 'presentation/profile_page.dart';

/// Dependencias que el arranque inyecta a la ruta `/profile` (F-T52).
class ProfileRouteDeps {
  ProfileRouteDeps({
    required this.api,
    required this.session,
    this.identity,
    required this.reader,
    this.onLogout,
  });

  final ApiClient api;
  final SessionRepository session;

  /// Store F-T20/F-T49 (fuente del flag `biometric_enabled`); `null` cuando
  /// la pagina lo resuelve desde [sessionIdentityStoreFactory].
  final SessionIdentityStore? identity;

  final BiometricReader reader;

  /// Seam de tests para el cierre de sesion; `null` = `SessionService`
  /// real (`dio` del [api] + [session], F-T02).
  final Future<void> Function()? onLogout;
}

/// Fabrica de dependencias de `/profile` (en tests se asigna un fake).
typedef ProfileRouteDepsFactory = ProfileRouteDeps Function();

ProfileRouteDepsFactory? profileRouteDepsFactory;

/// Rutas del feature `profile` (ver convencion en `features/README.md`).
///
/// - `/profile` -> [ProfilePage] (opciones de usuario "Mi perfil", F-T52).
///   Ruta privada: no esta en `_isPublicLocation`, asi que sin sesion la
///   guarda de `app_router.dart` redirige a `/login` o `/welcome`.
///
/// Si la fabrica es `null` (DI no configurada), la pantalla muestra un error
/// accionable en vez de romper.
final List<GoRoute> profileRoutes = [
  GoRoute(
    path: '/profile',
    builder: (context, state) {
      final factory = profileRouteDepsFactory;
      if (factory == null) {
        return Scaffold(
          appBar: AppBar(title: const Text('Mi perfil')),
          body: const ErrorView(
            message: 'Tu perfil no esta disponible en este momento. '
                'Intentalo mas tarde.',
          ),
        );
      }
      final deps = factory();
      return ProfilePage(
        consentService: HttpBiometricConsentService(api: deps.api),
        identity: deps.identity ?? sessionIdentityStoreFactory?.call(),
        reader: deps.reader,
        onLogout: deps.onLogout ??
            () => SessionService(dio: deps.api.dio, session: deps.session)
                .logout(),
      );
    },
  ),
];
