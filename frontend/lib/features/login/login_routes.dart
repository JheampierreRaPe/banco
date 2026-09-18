// Rutas del feature `login` (E1-T16, HU03).
//
// Convencion `features/README.md`: el feature expone
// `List<GoRoute> loginRoutes` y NUNCA toca `lib/core/router/app_router.dart`.
//
// ENSAMBLE (orquestador): esta lista REEMPLAZA al placeholder `/login` de
// `features/auth/auth_routes.dart`. En `core/router/app_router.dart`:
//
// ```dart
// import '../../features/login/login_routes.dart';
// // ...
// routes: [
//   // ...authRoutes,   // <- quitar el placeholder de `/login`
//   ...loginRoutes,     // <- login real (E1-T16)
//   ...homeRoutes,
// ],
// ```
//
// El arranque de la app debe asignar [loginRouteDepsFactory] con dependencias
// reales ([ApiClient] + [SessionRepository] compartida con el router). La
// biometria usa `SystemBiometricReader` (punto de integracion `local_auth`
// documentado en `features/biometrics/biometric_reader.dart`); cuando el
// orquestador agregue la dependencia, el reader del sistema funcionara sin
// cambios aqui.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/http/api_client.dart';
import '../../core/session/session_repository.dart';
import '../../core/widgets/error_view.dart';
import '../biometrics/biometric_reader.dart';
import '../biometrics/biometric_service.dart';
import '../biometrics/login_controller.dart' as bio;
import 'login_controller.dart';
import 'login_page.dart';

/// Dependencias que el arranque inyecta a la ruta `/login`.
class LoginRouteDeps {
  LoginRouteDeps({
    required this.api,
    required this.session,
    required this.reader,
    this.userRef = '',
    this.deviceId = '',
  });

  final ApiClient api;
  final SessionRepository session;
  final BiometricReader reader;

  /// Referencia del usuario si ya se conoce (p. ej. ultimo usuario).
  final String userRef;

  /// Identificador del dispositivo (viaja como `device_id`).
  final String deviceId;
}

/// Fabrica de dependencias de `/login` (en tests se asigna un fake).
typedef LoginRouteDepsFactory = LoginRouteDeps Function();

LoginRouteDepsFactory? loginRouteDepsFactory;

/// Seam de integración para tests del router (orquestador): en `true` las
/// páginas se construyen con `autoTick: false` para que el `Timer` periódico
/// de inactividad no quede pendiente al final del test. En producción siempre
/// es `false` (valor por defecto).
@visibleForTesting
bool debugDisableLoginAutoTick = false;

/// Rutas del feature `login` (ver convencion en `features/README.md`).
///
/// - `/login?userRef=<id>&deviceId=<id>`: pantalla de login biometrico + PIN.
final List<GoRoute> loginRoutes = [
  GoRoute(
    path: '/login',
    builder: (context, state) {
      final factory = loginRouteDepsFactory;
      if (factory == null) {
        return Scaffold(
          appBar: AppBar(title: const Text('Inicia sesión')),
          body: const ErrorView(
            message: 'Inicio de sesión no disponible en este momento. '
                'Inténtalo más tarde.',
          ),
        );
      }
      final deps = factory();
      final biometrics = BiometricService(
        session: deps.session,
        reader: deps.reader,
      );
      final controller = LoginController(
        api: deps.api,
        session: deps.session,
        biometricLogin: bio.LoginController(
          api: deps.api,
          biometrics: biometrics,
        ),
      );
      return LoginPage(
        controller: controller,
        userRef: state.uri.queryParameters['userRef'] ?? deps.userRef,
        deviceId: state.uri.queryParameters['deviceId'] ?? deps.deviceId,
        autoTick: !debugDisableLoginAutoTick,
      );
    },
  ),
];
