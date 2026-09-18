import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:go_router/go_router.dart';

import '../../features/accounts/accounts_routes.dart';
import '../../features/activation/activation_routes.dart';
import '../../features/activation/activation_service.dart';
import '../../features/biometrics/biometric_reader.dart';
import '../../features/home/home_routes.dart';
import '../../features/kyc/camera_frame_source.dart';
import '../../features/kyc/kyc_dependencies.dart';
import '../../features/kyc/kyc_routes.dart';
import '../../features/kyc/kyc_service.dart';
import '../../features/login/login_routes.dart';
import '../../features/pin_setup/pin_setup_routes.dart';
import '../../features/pin_setup/pin_setup_service.dart';
import '../../features/welcome/welcome_routes.dart';
import '../../features/welcome/welcome_seen_store.dart';
import '../http/api_client.dart';
import '../session/secure_key_value_storage.dart';
import '../session/session_repository.dart';

/// Orquestador de navegación (go_router con guarda de sesión + primera vez).
///
/// Flujo inicial (diseno simple, solo logica):
/// - Primera vez SIN sesion (`hasSeenWelcome == false`): `/welcome`. La
///   bienvenida solo se muestra una vez jamas (flag persistente en
///   `flutter_secure_storage` via [WelcomeSeenStore]).
/// - Sin sesion y bienvenida ya vista: puerta de entrada `/entry`
///   (eleccion "Iniciar sesión" / "Crear cuenta").
/// - Rutas públicas (pre-login, sin sesión): `/welcome`, `/entry`, `/login`,
///   `/kyc*`, `/activate`, `/pin-setup`. Todo lo demás exige sesión.
/// - Sin sesión en ruta no pública y sin haber visto la bienvenida ->
///   `/welcome`; si ya la vio -> `/entry` (antes era `/login`).
/// - `/welcome` ya vista -> `/entry`.
/// - Con sesión visitando `/welcome`, `/entry` o `/login` -> `/home`.
/// - Cada feature expone `List<GoRoute> <feature>Routes`; este archivo es el
///   ÚNICO que las agrega al router (ningún feature toca el router global).
/// - El cableado de dependencias reales (ApiClient compartido + sesión) se
///   hace aquí una vez; en tests se puede pasar un `api` propio y un
///   [WelcomeSeenStore] en memoria.
bool _isPublicLocation(String location) {
  return location == '/welcome' ||
      location == '/entry' ||
      location == '/login' ||
      location == '/activate' ||
      location == '/pin-setup' ||
      location == '/kyc' ||
      location == '/kyc/task' ||
      location == '/kyc/result';
}

bool _isEntryLocation(String location) {
  return location == '/welcome' ||
      location == '/entry' ||
      location == '/login';
}

GoRouter buildRouter(
  SessionRepository session, {
  String initialLocation = '/home',
  ApiClient? api,
  WelcomeSeenStore? welcomeSeen,
}) {
  final client = api ?? ApiClient.create(session: session);
  KycDependencies.configure(
    service: HttpKycService(client),
    frameSource: CameraFrameSource(),
  );
  activationServiceFactory =
      () => HttpActivationService(api: client, session: session);
  pinSetupServiceFactory = () => HttpPinSetupService(api: client);
  pinSetupResendServiceFactory =
      () => HttpActivationService(api: client, session: session);
  loginRouteDepsFactory = () => LoginRouteDeps(
        api: client,
        session: session,
        reader: SystemBiometricReader(),
      );
  final seen = welcomeSeen ??
      welcomeSeenStoreFactory?.call() ??
      SecureWelcomeSeenStore(storage: FlutterSecureStorageAdapter());
  // Comparte la misma instancia con las paginas de `/welcome` (el flag que
  // lee la guarda es el mismo que marca el boton "Comenzar").
  welcomeSeenStoreFactory = () => seen;
  // Hidrata el flag persistente; al completarse notifica y la guarda
  // reevalua (retornantes van a `/entry` sin parpadeo posterior). En
  // memoria es un no-op que conserva el valor inyectado en tests.
  unawaited(seen.load());
  return GoRouter(
    initialLocation: initialLocation,
    refreshListenable: Listenable.merge([session, seen]),
    redirect: (context, state) {
      final loggedIn = session.isAuthenticated;
      final hasSeen = seen.hasSeenWelcome;
      final loc = state.matchedLocation;
      if (loggedIn) {
        if (_isEntryLocation(loc)) return '/home';
        return null;
      }
      if (!hasSeen) {
        if (loc == '/welcome') return null;
      // Primera vez: deep links publicos (/login, /kyc*, /activate,
      // /pin-setup, /entry) se respetan; cualquier otra ruta lleva a la bienvenida.
        if (!_isPublicLocation(loc)) return '/welcome';
        return null;
      }
      if (loc == '/welcome') return '/entry';
      if (!_isPublicLocation(loc)) return '/entry';
      return null;
    },
    routes: [
      ...welcomeRoutes,
      ...kycRoutes,
      ...activationRoutes,
      ...pinSetupRoutes,
      ...loginRoutes,
      ...accountsRoutes,
      ...homeRoutes,
    ],
  );
}
