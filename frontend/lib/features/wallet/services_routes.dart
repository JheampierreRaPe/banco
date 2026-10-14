import 'package:go_router/go_router.dart';

import '../payment_authorization/payment_authorization_routes.dart';
import 'data/services_service.dart';
import 'data/wallet_service.dart';
import 'models/services.dart';
import 'presentation/service_pay_page.dart';
import 'presentation/service_subscriptions_page.dart';
import 'presentation/services_search_page.dart';
import 'presentation/topup_page.dart';

/// Rutas del area servicios y recargas (E4-T19 / HU16, rol Cliente).
///
/// Convencion F-T01 (`features/README.md`): el feature NUNCA toca
/// `lib/core/router/app_router.dart`; el orquestador importa esta lista
/// via `walletRoutes` (que hace `...walletServicesRoutes`).
///
/// - `/wallet/services` -> [ServicesSearchPage] (catalogo/busqueda
///   `GET /services/catalog` + accesos a recarga y registrados).
/// - `/wallet/services/pay` -> [ServicePayPage] (pago `POST
///   /services/payments` + comprobante; el `biller` viaja por `extra`).
/// - `/wallet/topups` -> [TopupPage] (recarga `POST /topups` +
///   comprobante).
/// - `/wallet/services/subscriptions` -> [ServiceSubscriptionsPage]
///   (registro `POST /services/subscriptions` + registrados).
///
/// Las paginas reciben los servicios desde las fabricas (cableadas por el
/// orquestador con el `ApiClient` compartido). Si una fabrica es `null`,
/// muestran un error accionable en vez de romper.
final List<GoRoute> walletServicesRoutes = [
  GoRoute(
    path: '/wallet/services',
    builder: (context, state) => ServicesSearchPage(
      service: servicesServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/wallet/services/pay',
    builder: (context, state) {
      final biller = state.extra is Biller ? state.extra as Biller : null;
      final deps = paymentAuthorizationDepsFactory?.call();
      return ServicePayPage(
        biller: biller,
        servicesService: servicesServiceFactory?.call(),
        walletService: walletServiceFactory?.call(),
        authController: deps?.buildController(),
        authService: deps?.service,
      );
    },
  ),
  GoRoute(
    path: '/wallet/topups',
    builder: (context, state) {
      final deps = paymentAuthorizationDepsFactory?.call();
      return TopupPage(
        servicesService: servicesServiceFactory?.call(),
        walletService: walletServiceFactory?.call(),
        authController: deps?.buildController(),
        authService: deps?.service,
      );
    },
  ),
  GoRoute(
    path: '/wallet/services/subscriptions',
    builder: (context, state) => ServiceSubscriptionsPage(
      service: servicesServiceFactory?.call(),
    ),
  ),
];
