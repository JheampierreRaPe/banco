import 'package:go_router/go_router.dart';

import 'presentation/kyc_result_page.dart';
import 'presentation/kyc_start_page.dart';
import 'presentation/kyc_task_page.dart';

/// Rutas del feature `kyc` (E1-T05).
///
/// Convencion F-T01: el feature expone `List<GoRoute> kycRoutes` y el
/// orquestador (`core/router/app_router.dart`) las agrega con `...kycRoutes`
/// SIN que el feature toque el router global.
///
/// Cableado: antes de navegar a `/kyc`, el orquestador llama
/// `KycDependencies.configure(service: HttpKycService(apiClient))`; las
/// paginas comparten ese controlador entre los tres pasos.
final List<GoRoute> kycRoutes = [
  GoRoute(
    path: '/kyc',
    builder: (context, state) => const KycStartPage(),
  ),
  GoRoute(
    path: '/kyc/task',
    builder: (context, state) => const KycTaskPage(),
  ),
  GoRoute(
    path: '/kyc/result',
    builder: (context, state) => const KycResultPage(),
  ),
];
