import 'package:go_router/go_router.dart';

import 'data/beneficiaries_service.dart';
import 'models/beneficiary.dart';
import 'presentation/beneficiaries_page.dart';
import 'presentation/beneficiary_form_page.dart';

/// Rutas del feature `beneficiaries` (E2-T15 / HU07).
///
/// Convencion F-T01 (`features/README.md`): el feature NUNCA toca
/// `lib/core/router/app_router.dart`; el orquestador importa esta lista y
/// hace `...beneficiariesRoutes`.
///
/// - `/beneficiaries` -> [BeneficiariesPage] (agenda con busqueda `?q=` y
///   agrupacion por banco/alfabetica).
/// - `/beneficiaries/new` -> [BeneficiaryFormPage] (alta).
/// - `/beneficiaries/:id` -> [BeneficiaryFormPage] (edicion; el beneficiario
///   viaja por `extra` desde la lista o se resuelve por id).
///
/// Las paginas reciben el servicio desde `beneficiariesServiceFactory`
/// (cableada por el orquestador con el `ApiClient` compartido). Si la fabrica
/// es `null`, muestran un error accionable en vez de romper.
final List<GoRoute> beneficiariesRoutes = [
  GoRoute(
    path: '/beneficiaries',
    builder: (context, state) => BeneficiariesPage(
      service: beneficiariesServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/beneficiaries/new',
    builder: (context, state) => BeneficiaryFormPage(
      service: beneficiariesServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/beneficiaries/:id',
    builder: (context, state) {
      final id = state.pathParameters['id'] ?? '';
      final extra = state.extra;
      return BeneficiaryFormPage(
        service: beneficiariesServiceFactory?.call(),
        beneficiaryId: id,
        initial: extra is Beneficiary ? extra : null,
      );
    },
  ),
];
