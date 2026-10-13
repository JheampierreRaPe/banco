// Rutas del feature `payment_authorization` (E2-T21, HU08).
//
// Convencion F-T01 (`features/README.md`): el feature NUNCA toca
// `lib/core/router/app_router.dart`; el orquestador importa esta lista y
// hace `...paymentAuthorizationRoutes` (mas la fabrica
// `paymentAuthorizationServiceFactory`, igual que
// `transfersServiceFactory`).
//
// - Sin rutas propias: la auditoria i1 elimino la ruta huerfana
//   `/payments/error` (nadie navegaba a ella: `ConfirmPage` renderiza
//   `PaymentErrorView` inline con sus callbacks `_onPaymentPrimary` /
//   `_onPaymentSecondary`, que se perderian via `extra`). La lista queda
//   vacia a proposito: el orquestador sigue haciendo
//   `...paymentAuthorizationRoutes` sin cambios y `ConfirmPage` no se toca.
//
// El reto biometrico vive como bottom sheet ([showBiometricChallengeSheet])
// dentro del flujo de pago (`ConfirmPage`): no necesita ruta propia.
library;

import 'package:go_router/go_router.dart';

import '../../core/session/session_repository.dart';
import '../biometrics/biometric_reader.dart';
import '../biometrics/biometric_service.dart';
import 'data/payment_authorization_service.dart';
import 'payment_authorization_controller.dart';

/// Dependencias del reto biometrico, cableadas una vez por el orquestador
/// (`core/router/app_router.dart`) con el `ApiClient` compartido, la
/// sesion, el lector del SO y el `device_id` vigente.
class PaymentAuthorizationDeps {
  PaymentAuthorizationDeps({
    required this.service,
    required this.reader,
    required this.session,
    this.deviceId = '',
  });

  final PaymentAuthorizationServiceBase service;
  final BiometricReader reader;
  final SessionRepository session;
  final String deviceId;

  /// Controlador listo para un intento (reutiliza el firmador de `nonce`
  /// de F-T03: `BiometricService.signNonce`, sin romperlo).
  PaymentAuthorizationController buildController() {
    final reader = this.reader;
    final session = this.session;
    return PaymentAuthorizationController(
      service: service,
      reader: reader,
      signNonce: (nonce) =>
          BiometricService(session: session, reader: reader).signNonce(nonce),
      deviceId: deviceId,
    );
  }
}

/// Fabrica inyectable (en tests se asigna un fake).
typedef PaymentAuthorizationDepsFactory = PaymentAuthorizationDeps Function();

PaymentAuthorizationDepsFactory? paymentAuthorizationDepsFactory;

/// Sin rutas dedicadas (auditoria i1: `/payments/error` era huerfana).
/// La lista vacia mantiene el punto de ensamble del orquestador.
final List<GoRoute> paymentAuthorizationRoutes = [];
