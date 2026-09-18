import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/widgets/error_view.dart';
import '../activation/activation_service.dart';
import 'pin_setup_page.dart';
import 'pin_setup_service.dart';

/// Fábrica del servicio de creación de PIN usado por `/pin-setup`.
///
/// El arranque de la app debe asignarla con dependencias reales ([ApiClient])
/// e importar `...pinSetupRoutes` en el orquestador
/// (`core/router/app_router.dart`). En tests se asignan fakes.
typedef PinSetupServiceFactory = PinSetupService Function();

PinSetupServiceFactory? pinSetupServiceFactory;

/// Fábrica del servicio de reenvío (MISMO contrato de `activation`:
/// `POST /auth/otp/resend`). No se duplica el endpoint: se reutiliza
/// [ActivationService].
typedef PinSetupResendServiceFactory = ActivationService Function();

PinSetupResendServiceFactory? pinSetupResendServiceFactory;

/// Rutas del feature `pin_setup` (ver convención en `features/README.md`).
///
/// - `/pin-setup?userRef=<id>`: pantalla de creación del PIN.
/// - El feature NUNCA toca `lib/core/router/app_router.dart`: el orquestador
///   hace `...pinSetupRoutes` en su lista `routes`.
final List<GoRoute> pinSetupRoutes = [
  GoRoute(
    path: '/pin-setup',
    builder: (context, state) {
      final setupFactory = pinSetupServiceFactory;
      final resendFactory = pinSetupResendServiceFactory;
      final userRef = state.uri.queryParameters['userRef'] ?? '';
      if (setupFactory == null ||
          resendFactory == null ||
          userRef.isEmpty) {
        return Scaffold(
          appBar: AppBar(title: const Text('Crea tu PIN')),
          body: ErrorView(
            message: userRef.isEmpty
                ? 'Falta la referencia de usuario. Vuelve al registro para '
                    'generar un nuevo código.'
                : 'Creación de PIN no disponible en este momento. '
                    'Inténtalo más tarde.',
          ),
        );
      }
      return PinSetupPage(
        userRef: userRef,
        setupService: setupFactory(),
        resendService: resendFactory(),
      );
    },
  ),
];
