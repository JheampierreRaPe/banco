import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/notifications/notification_service.dart';
import 'package:banca_online/features/activation/activation_controller.dart';
import 'package:banca_online/features/activation/activation_notifications.dart';
import 'package:banca_online/features/activation/activation_page.dart';
import 'package:banca_online/features/activation/activation_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del contrato de activación (sin red).
class FakeActivationService implements ActivationService {
  ApiException? resendError;
  int resendCalls = 0;

  @override
  Future<ActivationResult> activate({
    required String userRef,
    required String code,
  }) async =>
      const ActivationResult(userId: 'u-1', status: 'ACTIVE');

  @override
  Future<ResendResult> resend({required String userRef, String? channel}) async {
    resendCalls++;
    final error = resendError;
    if (error != null) throw error;
    return const ResendResult(
      userId: 'u-1',
      resendCount: 1,
      expiresInSeconds: 600,
    );
  }
}

ActivationController _controller(
  FakeActivationService service,
  NotificationService notifications,
) =>
    ActivationController(
      service: service,
      userRef: 'u-1',
      otpValiditySeconds: 600,
      resendWaitSeconds: 0,
      notifications: notifications,
    );

void main() {
  group('controller + NotificationService (mock)', () {
    test('resend exitoso dispara la notificación local (sin OTP)', () async {
      final service = FakeActivationService();
      final notifications = NotificationService();
      final controller = _controller(service, notifications);

      final ok = await controller.resend();

      expect(ok, isTrue);
      expect(notifications.notifications, hasLength(1));
      final n = notifications.last!;
      expect(n.route, '/activate?userRef=u-1');
      expect(RegExp(r'\b\d{6}\b').hasMatch(n.body), isFalse);
    });

    test('resend fallido NO dispara notificación', () async {
      final service = FakeActivationService()
        ..resendError = ApiException(code: 'RESEND_LIMIT', message: 'srv');
      final notifications = NotificationService();
      final controller = _controller(service, notifications);

      final ok = await controller.resend();

      expect(ok, isFalse);
      expect(notifications.notifications, isEmpty);
    });

    test('tap de la notificación resuelve a /activate (deep-link testeable)',
        () async {
      final service = FakeActivationService();
      final notifications = NotificationService();
      final controller = _controller(service, notifications);
      await controller.resend();

      String? tappedRoute;
      notifications.addTapListener((n) => tappedRoute = n.route);
      expect(notifications.tap(notifications.last!.id), isTrue);
      expect(tappedRoute, '/activate?userRef=u-1');
    });

    test('copy honesto: el cuerpo nunca trae el código', () {
      expect(
        RegExp(r'\b\d{6}\b')
            .hasMatch(ActivationNotifications.buildOtpSent('u-1').body),
        isFalse,
      );
      expect(
        ActivationNotifications.activateRoute('u-1'),
        '/activate?userRef=u-1',
      );
    });
  });

  group('ActivationPage (widget)', () {
    GoRouter router(
      FakeActivationService service,
      NotificationService notifications,
    ) =>
        GoRouter(
          initialLocation: '/activate?userRef=u-1',
          routes: [
            GoRoute(
              path: '/activate',
              builder: (context, state) => ActivationPage(
                userRef: state.uri.queryParameters['userRef'] ?? 'u-1',
                service: service,
                resendWaitSeconds: 0,
                autoTick: false,
                notifications: notifications,
              ),
            ),
            GoRoute(
              path: '/login',
              builder: (context, state) =>
                  const Scaffold(body: Text('login-ok')),
            ),
          ],
        );

    testWidgets('resend exitoso muestra aviso en el dispositivo', (tester) async {
      final service = FakeActivationService();
      final notifications = NotificationService();
      await tester.pumpWidget(
        MaterialApp.router(routerConfig: router(service, notifications)),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('activate-resend')));
      await tester.pumpAndSettle();

      expect(notifications.notifications, hasLength(1));
      expect(find.byKey(const Key('activate-otp-snackbar')), findsOneWidget);
      expect(find.text(ActivationController.resentMessage), findsOneWidget);
    });

    testWidgets('tap del aviso abre /activate con el userRef', (tester) async {
      final service = FakeActivationService();
      final notifications = NotificationService();
      final r = router(service, notifications);
      await tester.pumpWidget(MaterialApp.router(routerConfig: r));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('activate-resend')));
      await tester.pumpAndSettle();

      String? tappedRoute;
      notifications.addTapListener((n) => tappedRoute = n.route);
      notifications.tap(notifications.last!.id);
      await tester.pumpAndSettle();

      expect(tappedRoute, '/activate?userRef=u-1');
      // La página de activación sigue visible (deep-link a /activate).
      expect(find.byKey(const Key('activate-submit')), findsOneWidget);
    });

    testWidgets('¿Cómo llega el código? explica el canal sin inventar OTP',
        (tester) async {
      final service = FakeActivationService();
      await tester.pumpWidget(
        MaterialApp.router(
          routerConfig: router(service, NotificationService()),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('activate-how-code')));
      await tester.pumpAndSettle();

      expect(find.text(ActivationNotifications.howItArrivesTitle),
          findsOneWidget);
      expect(find.text(ActivationNotifications.howItArrivesBody),
          findsOneWidget);
      // Cierre del diálogo.
      await tester.tap(find.byKey(const Key('activate-how-code-close')));
      await tester.pumpAndSettle();
      expect(find.text(ActivationNotifications.howItArrivesBody),
          findsNothing);
    });
  });
}
