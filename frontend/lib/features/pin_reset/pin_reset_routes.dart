// Rutas del feature `pin_reset` (F-T43, HU02/HU04).
//
// Convención `features/README.md`: el feature expone
// `List<GoRoute> pinResetRoutes` y NUNCA toca
// `lib/core/router/app_router.dart` (el orquestador las monta y declara
// `/pin-reset*` como públicas pre-login).
//
// Orden obligatorio del flujo:
// - `/pin-reset`: email + DNI (emite el OTP `RECOVERY`).
// - `/pin-reset/otp?email=<urlencoded>&ttl=<s>&resendWait=<s>` (+ `extra` =
//   DNI en memoria): OTP de 6 casillas con cuenta atrás y reenvío.
// - `/pin-reset/new-pin?email=<urlencoded>` (+ `extra` = [PinResetDraft]
//   con email/DNI/código): crear el PIN nuevo.
// - `/pin-reset/confirm?email=<urlencoded>` (+ `extra` = [PinResetDraft]
//   con PIN): confirmar y fijar con `POST /auth/pin-reset`.
// - `/pin-reset/success?userRef=<id>`: fin del flujo (navega al login).
//
// El DNI/OTP/PIN viajan SOLO en `extra` (memoria): nunca en la ruta ni en
// logs. El email viaja como query (igual que `/recovery/otp`).
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/http/api_client.dart';
import '../../core/session/session_identity_store.dart';
import '../../core/session/session_repository.dart';
import '../../core/widgets/empty_view.dart';
import '../../core/widgets/error_view.dart';
import 'pin_reset_controllers.dart';
import 'pin_reset_service.dart';
import 'presentation/pin_reset_confirm_page.dart';
import 'presentation/pin_reset_identity_page.dart';
import 'presentation/pin_reset_new_pin_page.dart';
import 'presentation/pin_reset_otp_page.dart';
import 'presentation/pin_reset_success_page.dart';

/// Dependencias que el arranque inyecta a las rutas de `/pin-reset`.
class PinResetRouteDeps {
  PinResetRouteDeps({
    required this.api,
    required this.session,
    this.identity,
  });

  final ApiClient api;
  final SessionRepository session;

  /// Store F-T20 (`device_id` + `user_ref`). Si es `null` se usa
  /// [sessionIdentityStoreFactory] (o se omite el guardado).
  final SessionIdentityStore? identity;
}

/// Fábrica de dependencias de `/pin-reset*` (en tests se asigna un fake).
typedef PinResetRouteDepsFactory = PinResetRouteDeps Function();

PinResetRouteDepsFactory? pinResetRouteDepsFactory;

/// Vigencia/cooldown por defecto si la ruta no trae `ttl`/`resendWait`.
@visibleForTesting
const int defaultPinResetTtlSeconds = 600;

@visibleForTesting
const int defaultPinResetResendWaitSeconds = 30;

/// Seam de integración para tests del router (orquestador): en `true` las
/// páginas se construyen con `autoTick: false` para que el `Timer` periódico
/// no quede pendiente al final del test. En producción siempre es `false`.
@visibleForTesting
bool debugDisablePinResetAutoTick = false;

/// Rutas del feature `pin_reset` (ver convención en `features/README.md`).
final List<GoRoute> pinResetRoutes = [
  GoRoute(
    path: '/pin-reset',
    builder: (context, state) {
      final factory = pinResetRouteDepsFactory;
      if (factory == null) {
        return Scaffold(
          appBar: AppBar(title: const Text('Restablecer PIN')),
          body: const ErrorView(
            message: 'Restablecer PIN no disponible en este momento. '
                'Inténtalo más tarde.',
          ),
        );
      }
      final deps = factory();
      final service = HttpPinResetService(api: deps.api);
      final controller = PinResetIdentityController(service: service);
      return PinResetIdentityPage(controller: controller);
    },
    routes: [
      GoRoute(
        path: 'otp',
        builder: (context, state) {
          final factory = pinResetRouteDepsFactory;
          final email = state.uri.queryParameters['email'] ?? '';
          final docNumber = state.extra is String ? state.extra as String : '';
          if (factory == null || email.isEmpty || docNumber.isEmpty) {
            return Scaffold(
              appBar: AppBar(title: const Text('Verifica tu correo')),
              body: EmptyView(
                message: 'Falta tu correo o tu DNI. '
                    'Vuelve atrás e ingrésalos de nuevo.',
                actionLabel: 'Volver al inicio',
                onAction: () => context.go('/pin-reset'),
              ),
            );
          }
          final deps = factory();
          final ttl = int.tryParse(
                state.uri.queryParameters['ttl'] ?? '',
              ) ??
              defaultPinResetTtlSeconds;
          final resendWait = int.tryParse(
                state.uri.queryParameters['resendWait'] ?? '',
              ) ??
              defaultPinResetResendWaitSeconds;
          final service = HttpPinResetService(api: deps.api);
          final controller = PinResetOtpController(
            service: service,
            email: email,
            otpValiditySeconds: ttl,
            resendWaitSeconds: resendWait,
          );
          return PinResetOtpPage(
            email: email,
            docNumber: docNumber,
            controller: controller,
            autoTick: !debugDisablePinResetAutoTick,
          );
        },
      ),
      GoRoute(
        path: 'new-pin',
        builder: (context, state) {
          final draft =
              state.extra is PinResetDraft ? state.extra as PinResetDraft : null;
          if (draft == null || draft.code.isEmpty) {
            return Scaffold(
              appBar: AppBar(title: const Text('Crea un nuevo PIN')),
              body: EmptyView(
                message: 'Primero verifica el código que te enviamos por '
                    'correo.',
                actionLabel: 'Volver al inicio',
                onAction: () => context.go('/pin-reset'),
              ),
            );
          }
          return PinResetNewPinPage(draft: draft);
        },
      ),
      GoRoute(
        path: 'confirm',
        builder: (context, state) {
          final factory = pinResetRouteDepsFactory;
          final draft =
              state.extra is PinResetDraft ? state.extra as PinResetDraft : null;
          if (factory == null || draft == null || draft.pin.isEmpty) {
            return Scaffold(
              appBar: AppBar(title: const Text('Confirma tu PIN')),
              body: EmptyView(
                message: 'Primero crea tu PIN nuevo para poder confirmarlo.',
                actionLabel: 'Volver al inicio',
                onAction: () => context.go('/pin-reset'),
              ),
            );
          }
          final deps = factory();
          return PinResetConfirmPage(
            draft: draft,
            service: HttpPinResetService(api: deps.api),
            identity: deps.identity,
          );
        },
      ),
      GoRoute(
        path: 'success',
        builder: (context, state) {
          final factory = pinResetRouteDepsFactory;
          final userRef = state.uri.queryParameters['userRef'] ?? '';
          final deps = factory?.call();
          return PinResetSuccessPage(
            userRef: userRef,
            identity:
                deps?.identity ?? sessionIdentityStoreFactory?.call(),
          );
        },
      ),
    ],
  ),
];
