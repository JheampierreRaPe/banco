// Rutas del feature `recovery` (F-T29, HU04).
//
// Convención `features/README.md`: el feature expone
// `List<GoRoute> recoveryRoutes` y NUNCA toca
// `lib/core/router/app_router.dart` (el orquestador las monta y declara
// `/recovery` y `/recovery/otp` como públicas).
//
// - `/recovery`: pantalla de email.
// - `/recovery/otp?email=<urlencoded>&ttl=<s>&resendWait=<s>`: pantalla OTP.
//   `ttl`/`resendWait` llegan del `request` (la página de email los propaga);
//   si faltan se usan los valores por defecto.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/http/api_client.dart';
import '../../core/session/session_identity_store.dart';
import '../../core/session/session_repository.dart';
import '../../core/widgets/error_view.dart';
import 'presentation/recovery_email_page.dart';
import 'presentation/recovery_otp_page.dart';
import 'recovery_controller.dart';
import 'recovery_service.dart';

/// Dependencias que el arranque inyecta a las rutas de recuperación.
class RecoveryRouteDeps {
  RecoveryRouteDeps({
    required this.api,
    required this.session,
    this.identity,
    this.platform,
  });

  final ApiClient api;
  final SessionRepository session;

  /// Store F-T20 (`device_id` + `user_ref`). Si es `null` se usa
  /// [sessionIdentityStoreFactory] (o se omite el guardado).
  final SessionIdentityStore? identity;

  /// `android`/`ios`; `null` = resolver del SO en el controlador.
  final String? platform;
}

/// Fábrica de dependencias de `/recovery` y `/recovery/otp` (en tests se
/// asigna un fake).
typedef RecoveryRouteDepsFactory = RecoveryRouteDeps Function();

RecoveryRouteDepsFactory? recoveryRouteDepsFactory;

/// Vigencia/cooldown por defecto si la ruta no trae `ttl`/`resendWait`.
@visibleForTesting
const int defaultRecoveryTtlSeconds = 600;

@visibleForTesting
const int defaultRecoveryResendWaitSeconds = 30;

/// Seam de integración para tests del router (orquestador): en `true` las
/// páginas se construyen con `autoTick: false` para que el `Timer` periódico
/// no quede pendiente al final del test. En producción siempre es `false`.
@visibleForTesting
bool debugDisableRecoveryAutoTick = false;

/// Rutas del feature `recovery` (ver convención en `features/README.md`).
final List<GoRoute> recoveryRoutes = [
  GoRoute(
    path: '/recovery',
    builder: (context, state) {
      final factory = recoveryRouteDepsFactory;
      if (factory == null) {
        return Scaffold(
          appBar: AppBar(title: const Text('Recuperar acceso')),
          body: const ErrorView(
            message: 'Recuperación no disponible en este momento. '
                'Inténtalo más tarde.',
          ),
        );
      }
      final deps = factory();
      final service = HttpRecoveryService(api: deps.api);
      final controller = RecoveryEmailController(service: service);
      return RecoveryEmailPage(controller: controller);
    },
  ),
  GoRoute(
    path: '/recovery/otp',
    builder: (context, state) {
      final factory = recoveryRouteDepsFactory;
      final email = state.uri.queryParameters['email'] ?? '';
      if (factory == null || email.isEmpty) {
        return Scaffold(
          appBar: AppBar(title: const Text('Recuperar acceso')),
          body: const ErrorView(
            message: 'Falta el correo de recuperación. '
                'Vuelve atrás e ingrésalo de nuevo.',
          ),
        );
      }
      final deps = factory();
      final ttl = int.tryParse(
            state.uri.queryParameters['ttl'] ?? '',
          ) ??
          defaultRecoveryTtlSeconds;
      final resendWait = int.tryParse(
            state.uri.queryParameters['resendWait'] ?? '',
          ) ??
          defaultRecoveryResendWaitSeconds;
      final service = HttpRecoveryService(api: deps.api);
      final controller = RecoveryOtpController(
        service: service,
        session: deps.session,
        email: email,
        otpValiditySeconds: ttl,
        resendWaitSeconds: resendWait,
        identity: deps.identity ?? sessionIdentityStoreFactory?.call(),
        platform: deps.platform,
      );
      return RecoveryOtpPage(
        email: email,
        controller: controller,
        autoTick: !debugDisableRecoveryAutoTick,
      );
    },
  ),
];
