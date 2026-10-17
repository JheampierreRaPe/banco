import 'package:go_router/go_router.dart';

import '../accounts/data/accounts_service.dart';
import '../payment_authorization/payment_authorization_routes.dart';
import 'data/merchant_qr_service.dart';
import 'data/personal_qr_service.dart';
import 'data/qr_payment_service.dart';
import 'data/wallet_service.dart';
import 'presentation/merchant_qr_page.dart';
import 'presentation/merchant_qr_status_page.dart';
import 'presentation/personal_qr_page.dart';
import 'presentation/qr_pay_page.dart';
import 'presentation/qr_scan_page.dart';
import 'presentation/wallet_link_page.dart';
import 'presentation/wallet_page.dart';
import 'services_routes.dart';

/// Rutas del feature `wallet` (E4-T04 / HU13).
///
/// Convencion F-T01 (`features/README.md`): el feature NUNCA toca
/// `lib/core/router/app_router.dart`; el orquestador importa esta lista y
/// hace `...walletRoutes` (mas la fabrica `walletServiceFactory`, igual
/// que `beneficiariesServiceFactory`; el selector de cuentas reusa la
/// fabrica `accountsServiceFactory` ya cableada).
///
/// - `/wallet` -> [WalletPage] (billetera con vinculos enmascarados +
///   entrada a vincular).
/// - `/wallet/link` -> [WalletLinkPage] (vincular cuenta del banco o
///   tarjeta; devuelve `true` al vincular para recargar la lista).
/// - `/wallet/merchant-qr` -> [MerchantQrPage] (comercio: generar un
///   cobro QR con monto + concepto y verlo con su vigencia, E4-T09).
/// - `/wallet/merchant-charges/:chargeId` -> [MerchantQrStatusPage]
///   (estado efectivo del cobro: pendiente/pagado/vencido, E4-T09).
/// - `/wallet/personal-qr` -> [PersonalQrPage] (titular: generar y
///   mostrar su QR personal estatico, sin monto ni caducidad,
///   reutilizable, E4-T21).
/// - `/wallet/pay-qr` -> [QrScanPage] (pagador: escanea el QR e inyecta
///   el `qr_content` leido, E4-T13).
/// - `/wallet/pay-qr/confirm` -> [QrPayPage] (resumen + confirmacion en
///   un toque + biometria/PIN ante `PENDING_AUTHORIZATION` + resultado,
///   E4-T13/E4-T14; el `qr_content` viaja por `extra` como `String`).
/// - `/wallet/services*` + `/wallet/topups` -> `walletServicesRoutes`
///   (E4-T19 / HU16: catalogo, pago con comprobante, recarga con
///   comprobante, registrados; ver `services_routes.dart`).
///
/// Las paginas reciben los servicios desde las fabricas (cableadas por el
/// orquestador con el `ApiClient` compartido). Si una fabrica es `null`,
/// muestran un error accionable en vez de romper.
final List<GoRoute> walletRoutes = [
  GoRoute(
    path: '/wallet',
    builder: (context, state) => WalletPage(
      service: walletServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/wallet/link',
    builder: (context, state) => WalletLinkPage(
      service: walletServiceFactory?.call(),
      accountsService: accountsServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/wallet/merchant-qr',
    builder: (context, state) => MerchantQrPage(
      service: merchantQrServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/wallet/merchant-charges/:chargeId',
    builder: (context, state) => MerchantQrStatusPage(
      chargeId: state.pathParameters['chargeId'] ?? '',
      service: merchantQrServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/wallet/personal-qr',
    builder: (context, state) => PersonalQrPage(
      service: personalQrServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/wallet/pay-qr',
    builder: (context, state) => const QrScanPage(),
  ),
  GoRoute(
    path: '/wallet/pay-qr/confirm',
    builder: (context, state) {
      final qrContent = state.extra is String ? state.extra as String : '';
      final deps = paymentAuthorizationDepsFactory?.call();
      return QrPayPage(
        qrContent: qrContent,
        paymentService: qrPaymentServiceFactory?.call(),
        walletService: walletServiceFactory?.call(),
        authController: deps?.buildController(),
        authService: deps?.service,
      );
    },
  ),
  ...walletServicesRoutes,
];
