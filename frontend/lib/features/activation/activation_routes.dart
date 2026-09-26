import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/widgets/error_view.dart';
import 'activation_page.dart';
import 'activation_service.dart';

/// Fábrica del servicio usado por la ruta `/activate`.
///
/// El arranque de la app debe asignarla con dependencias reales ([ApiClient]
/// + [SessionRepository] compartida con el router) e importar
/// `...activationRoutes` en el orquestador (`core/router/app_router.dart`).
/// En tests se asigna un fake. Si no está asignada (o falta `userRef`), la
/// ruta muestra un [ErrorView] de configuración en lugar de fallar.
///
/// PENDIENTE de integración: la guarda actual de `app_router.dart` redirige a
/// `/login` toda ruta pública sin sesión; al cablear `/activate` hay que
/// permitirla como ruta pública (igual que `/login`), pues la activación
/// ocurre ANTES del primer inicio de sesión.
typedef ActivationServiceFactory = ActivationService Function();

ActivationServiceFactory? activationServiceFactory;

/// Rutas del feature `activation` (ver convención en `features/README.md`).
///
/// - `/activate?userRef=<id>`: pantalla de ingreso del OTP.
/// - El feature NUNCA toca `lib/core/router/app_router.dart`: el orquestador
///   hace `...activationRoutes` en su lista `routes`.
final List<GoRoute> activationRoutes = [
  GoRoute(
    path: '/activate',
    builder: (context, state) {
      final factory = activationServiceFactory;
      final userRef = state.uri.queryParameters['userRef'] ?? '';
      if (factory == null || userRef.isEmpty) {
        return Scaffold(
          appBar: AppBar(title: const Text('Activa tu cuenta')),
          body: ErrorView(
            message: userRef.isEmpty
                ? 'Falta la referencia de usuario. Vuelve al registro para '
                    'generar un nuevo código.'
                : 'Activación no disponible en este momento. '
                    'Inténtalo más tarde.',
          ),
        );
      }
      return ActivationPage(userRef: userRef, service: factory());
    },
  ),
];
