import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/widgets/error_view.dart';
import 'data/credit_applications_service.dart';
import 'data/credit_contract_service.dart';
import 'data/credit_sign_controller.dart';
import 'data/credits_service.dart';
import 'models/credit.dart';
import 'models/loan_application.dart';
import 'presentation/credit_application_page.dart';
import 'presentation/credit_contract_page.dart';
import 'presentation/credit_sign_page.dart';
import 'presentation/credit_status_page.dart';
import 'presentation/credits_simulator_page.dart';

/// Rutas del feature `credits` (E3-T04 / HU09 + E3-T09 / HU10).
///
/// Convencion F-T01 (`features/README.md`): el feature NUNCA toca
/// `lib/core/router/app_router.dart`; el orquestador importa esta lista y
/// hace `...creditsRoutes` (mas las fabricas `creditsServiceFactory` y
/// `creditApplicationsServiceFactory`, igual que
/// `transfersServiceFactory`).
///
/// - `/credits` -> [CreditsSimulatorPage] (simulador: producto, monto,
///   plazo, desgravamen, cuota, cronograma, CTAs).
/// - `/credits/apply` -> [CreditApplicationPage] (solicitud +
///   consentimiento, E3-T09): recibe el [SimulationDraft] por `extra` y lo
///   pre-carga (producto/monto/plazo/insurance + cuota del servidor).
/// - `/credits/status` -> [CreditStatusPage] (estado del expediente,
///   E3-T09): recibe por `extra` el [LoanApplicationResult] del envio o el
///   `String` con el id para consultar `GET /credits/applications/{id}`.
/// - `/credits/contract` -> [CreditContractPage] (lectura del contrato,
///   E3-T15): recibe por `extra` el [LoanApplicationResult] aprobado (hero
///   con las condiciones + id) o el `String` con el id.
/// - `/credits/sign` -> [CreditSignPage] (firma con biometria + fallback
///   PIN, E3-T15): recibe por `extra` el `String` con el id o un `Map` con
///   `applicationId` (+ `summary` opcional con la linea del servidor).
///
/// Las paginas reciben los servicios desde las fabricas (cableadas por el
/// orquestador con el `ApiClient` compartido). Si una fabrica es `null`, la
/// pantalla degrada a un error accionable en vez de romper.
final List<GoRoute> creditsRoutes = [
  GoRoute(
    path: '/credits',
    builder: (context, state) => CreditsSimulatorPage(
      service: creditsServiceFactory?.call(),
    ),
  ),
  GoRoute(
    path: '/credits/apply',
    builder: (context, state) {
      final extra = state.extra;
      return CreditApplicationPage(
        draft: extra is SimulationDraft ? extra : null,
        service: creditApplicationsServiceFactory?.call(),
      );
    },
  ),
  GoRoute(
    path: '/credits/status',
    builder: (context, state) {
      final extra = state.extra;
      return CreditStatusPage(
        service: creditApplicationsServiceFactory?.call(),
        applicationId: extra is String ? extra : null,
        initialResult: extra is LoanApplicationResult ? extra : null,
      );
    },
  ),
  GoRoute(
    path: '/credits/contract',
    builder: (context, state) {
      final extra = state.extra;
      return CreditContractPage(
        service: creditContractServiceFactory?.call(),
        applicationId: extra is String ? extra : null,
        application: extra is LoanApplicationResult ? extra : null,
      );
    },
  ),
  GoRoute(
    path: '/credits/sign',
    builder: (context, state) {
      final deps = creditSignDepsFactory?.call();
      final extra = state.extra;
      String? applicationId;
      String? summary;
      if (extra is String) {
        applicationId = extra;
      } else if (extra is LoanApplicationResult) {
        applicationId = extra.id;
      } else if (extra is Map) {
        applicationId = extra['applicationId'] as String?;
        summary = extra['summary'] as String?;
      }
      final controller = deps?.buildController();
      return CreditSignPage(
        service: deps?.service ?? creditContractServiceFactory?.call(),
        reader: deps?.reader,
        signNonce: controller?.signNonce,
        deviceId: deps?.deviceId ?? '',
        applicationId: applicationId,
        summary: summary,
      );
    },
  ),
];

/// Error accionable cuando una ruta llega sin su `extra` (navegacion
/// directa o deep-link sin borrador): en vez de romper, explica y vuelve.
class MissingCreditArgs extends StatelessWidget {
  const MissingCreditArgs({super.key, required this.hint});

  final String hint;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Créditos')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: ErrorView(
          message: '$hint Vuelve e inicia el flujo de nuevo.',
          onRetry: () => context.pop(),
          retryLabel: 'Volver',
        ),
      ),
    );
  }
}
