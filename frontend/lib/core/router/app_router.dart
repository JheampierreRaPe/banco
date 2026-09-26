import 'package:flutter/foundation.dart';
import 'package:go_router/go_router.dart';

import '../../features/accounts/accounts_routes.dart';
import '../../features/accounts/data/accounts_service.dart';
import '../../features/activation/activation_routes.dart';
import '../../features/activation/activation_service.dart';
import '../../features/biometrics/biometric_reader.dart';
import '../../features/home/data/profile_service.dart';
import '../../features/home/home_routes.dart';
import '../../features/kyc/camera_frame_source.dart';
import '../../features/kyc/kyc_dependencies.dart';
import '../../features/kyc/kyc_routes.dart';
import '../../features/kyc/kyc_service.dart';
import '../../features/login/login_routes.dart';
import '../../features/pin_reset/pin_reset_routes.dart';
import '../../features/pin_setup/pin_setup_routes.dart';
import '../../features/pin_setup/pin_setup_service.dart';
import '../../features/profile/profile_routes.dart';
import '../../features/splash/splash_routes.dart';
import '../../features/welcome/welcome_routes.dart';
import '../http/api_client.dart';
import '../session/secure_key_value_storage.dart';
import '../session/session_identity_store.dart';
import '../session/session_repository.dart';

/// Orquestador de navegación (go_router con guarda por `userRef` + sesión).
///
/// Flujo inicial (F-T37, decisión SCR-005 d5; solo lógica, sin UI):
/// - Sin sesión y sin `userRef` (`null` o vacío): onboarding `/welcome`. El
///   flag local de bienvenida se eliminó (F-T37): ya no se lee ni se
///   escribe ninguna clave de primera vez.
/// - Sin sesión con `userRef`: rutas públicas pre-login permitidas
///   (`/splash`, `/welcome`, `/login`, `/login/device`, `/kyc*`, `/activate`,
///   `/pin-setup*`, `/registration-success`, `/pin-reset*`);
///   cualquier ruta privada -> `/login`.
/// - Con sesión: `/home`; visitar `/splash`, `/welcome`, `/login`,
///   `/login/device` o el legacy `/entry` redirige a `/home`.
/// - El legacy `/entry` (retirado en F-T36) redirige a `/welcome` sin sesión.
/// - Cada feature expone `List<GoRoute> <feature>Routes`; este archivo es el
///   ÚNICO que las agrega al router (ningún feature toca el router global).
/// - El cableado de dependencias reales (ApiClient compartido + sesión +
///   identidad) se hace aquí una vez; en tests se puede pasar un `api` propio
///   y un [SessionIdentityStore] en memoria.
/// - El arranque de la app entra por `/splash` (ver `app.dart`): la splash
///   delega en esta guarda, que resuelve onboarding, login o home.
/// - [SessionIdentityStore] es [Listenable]: el `refreshListenable` combina
///   sesión + identidad para que guardar el `userRef` (paso success del
///   registro, F-T39) reevalúe la guarda sin navegación manual.
bool _isPublicLocation(String location) {
  return       location == '/splash' ||
      location == '/welcome' ||
      location == '/login' ||
      // Entrada "iniciar sesion en este dispositivo" (F-T57, plomeria):
      // publica pre-login sin `userRef` (igual que `/pin-reset*`); la
      // pantalla del flujo de 3 pasos la monta F-T56 sobre esta ruta.
      location == '/login/device' ||
      location.startsWith('/login/device/') ||
      location == '/activate' ||
      // Cierre del registro F-T39 (PIN -> OTP -> success): todas las
      // sub-rutas de `/pin-setup` + el paso success son publicas pre-login.
      location == '/pin-setup' ||
      location.startsWith('/pin-setup/') ||
      location == '/registration-success' ||
      // Restablecimiento de PIN F-T43 (email + DNI + OTP -> PIN nuevo):
      // todas las sub-rutas de `/pin-reset` + el paso success son
      // públicas pre-login (no abren sesión).
      location == '/pin-reset' ||
      location.startsWith('/pin-reset/') ||
      location == '/kyc' ||
      location == '/kyc/document' ||
      location == '/kyc/task' ||
      location == '/kyc/result';
}

bool _isEntryLocation(String location) {
  return location == '/welcome' ||
      location == '/login' ||
      location == '/login/device' ||
      location.startsWith('/login/');
}

GoRouter buildRouter(
  SessionRepository session, {
  String initialLocation = '/splash',
  ApiClient? api,
  SessionIdentityStore? identity,
}) {
  final client = api ?? ApiClient.create(session: session);
  // Identidad local (F-T20): `user_ref` + `device_id` hidratados por `main`
  // antes del primer frame. En tests se inyecta un store en memoria.
  final identityStore = identity ??
      sessionIdentityStoreFactory?.call() ??
      SecureSessionIdentityStore(storage: FlutterSecureStorageAdapter());
  sessionIdentityStoreFactory = () => identityStore;
  KycDependencies.configure(
    service: HttpKycService(client),
    frameSource: CameraFrameSource(),
  );
  activationServiceFactory =
      () => HttpActivationService(api: client, session: session);
  pinSetupServiceFactory = () => HttpPinSetupService(api: client);
  pinSetupResendServiceFactory =
      () => HttpActivationService(api: client, session: session);
  // Cuentas (F-T41): el orquestador es el unico dueno del `ApiClient`
  // compartido, asi que cablea aqui la fabrica que resuelven `homeRoutes`
  // y `accountsRoutes`. Solo cableado: la guarda F-T37 no se toca.
  accountsServiceFactory = () => AccountsService(api: client);
  // Perfil del home (F-T54, saludo/avatar de `GET /me`): mismo patron, solo
  // cableado; la guarda F-T37 no se toca.
  homeProfileServiceFactory = () => HttpProfileService(api: client);
  loginRouteDepsFactory = () => LoginRouteDeps(
        api: client,
        session: session,
        reader: SystemBiometricReader(),
        userRef: identityStore.userRef ?? '',
        deviceId: identityStore.deviceId ?? '',
      );
  pinResetRouteDepsFactory = () => PinResetRouteDeps(
        api: client,
        session: session,
        identity: identityStore,
      );
  // Login en este dispositivo (F-T56, flujo email+DNI -> OTP -> PIN sobre
  // `/login/device`, plomeria publica F-T57): mismo `ApiClient` compartido +
  // sesion + identidad. Solo cableado: la guarda F-T37/F-T57 no se toca.
  deviceLoginRouteDepsFactory = () => DeviceLoginRouteDeps(
        api: client,
        session: session,
        identity: identityStore,
      );
  // Perfil / opciones de usuario (F-T52): ruta privada con el `ApiClient`
  // compartido (Bearer para `POST /auth/biometric/consent`), la sesion
  // (logout via `SessionService`), la identidad (`biometricEnabled`, F-T49)
  // y el lector biometrico del SO. Solo cableado: la guarda F-T37 no se
  // toca (`/profile` no es publica, exige sesion).
  profileRouteDepsFactory = () => ProfileRouteDeps(
        api: client,
        session: session,
        identity: identityStore,
        reader: SystemBiometricReader(),
      );
  return GoRouter(
    initialLocation: initialLocation,
    refreshListenable: Listenable.merge([session, identityStore]),
    redirect: (context, state) {
      final loggedIn = session.isAuthenticated;
      final userRef = identityStore.userRef;
      final hasUserRef = userRef != null && userRef.isNotEmpty;
      final loc = state.matchedLocation;
      // Legacy `/entry` (retirado en F-T36): redirige al onboarding; con
      // sesión el destino es `/home`.
      if (loc == '/entry') return loggedIn ? '/home' : '/welcome';
      if (loggedIn) {
        if (loc == '/splash' || _isEntryLocation(loc)) return '/home';
        return null;
      }
      if (!hasUserRef) {
        // Sin cuenta conocida: el login exige `userRef`; las rutas públicas
        // del alta/recupero pasan, todo lo demás va al onboarding.
        if (loc == '/login') return '/welcome';
        if (_isPublicLocation(loc)) return null;
        return '/welcome';
      }
      if (!_isPublicLocation(loc)) return '/login';
      return null;
    },
    routes: [
      ...splashRoutes,
      ...welcomeRoutes,
      ...kycRoutes,
      ...activationRoutes,
      ...pinSetupRoutes,
      ...loginRoutes,
      ...pinResetRoutes,
      ...accountsRoutes,
      ...homeRoutes,
      ...profileRoutes,
    ],
  );
}
