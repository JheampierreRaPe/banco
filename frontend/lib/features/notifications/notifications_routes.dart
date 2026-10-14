import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/widgets/error_view.dart';
import 'data/notifications_service.dart';
import 'models/notification.dart';
import 'presentation/notification_detail_page.dart';
import 'presentation/notification_preferences_page.dart';
import 'presentation/notifications_page.dart';

/// Rutas del feature `notifications` (E6-T15 / HU22).
///
/// Convencion F-T01: el feature NUNCA toca
/// `lib/core/router/app_router.dart`; el orquestador importa esta lista y
/// hace `...notificationsRoutes` (mas la fabrica
/// `notificationsServiceFactory` con el `ApiClient` compartido).
///
/// - `/notifications` -> [NotificationsPage] (centro del titular).
/// - `/notifications/preferences` -> [NotificationPreferencesPage].
/// - `/notifications/:id` -> [NotificationDetailPage] (detalle read-only
///   `GET /notifications/{id}`; el item viaja por `extra` como
///   [NotificationItem] solo como indicio, la fuente de verdad es el
///   servidor; el detalle + "no reconozco").
///
/// Todas son privadas: no estan en `_isPublicLocation`, asi que sin sesion
/// la guarda redirige a `/login` o `/welcome`.
final List<GoRoute> notificationsRoutes = [
  GoRoute(
    path: '/notifications',
    builder: (context, state) => NotificationsPage(
      service: notificationsServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/notifications/preferences',
    builder: (context, state) => NotificationPreferencesPage(
      service: notificationsServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/notifications/:id',
    builder: (context, state) {
      final id = state.pathParameters['id'] ?? '';
      final item =
          state.extra is NotificationItem ? state.extra as NotificationItem : null;
      final factory = notificationsServiceFactory;
      if (factory == null) {
        return Scaffold(
          appBar: AppBar(title: const Text('Detalle de alerta')),
          body: const ErrorView(
            message: 'Tu centro de notificaciones no está disponible en este '
                'momento. Inténtalo más tarde.',
          ),
        );
      }
      return NotificationDetailPage(
        notificationId: item?.id ?? id,
        notification: item,
        service: factory(),
      );
    },
  ),
];
