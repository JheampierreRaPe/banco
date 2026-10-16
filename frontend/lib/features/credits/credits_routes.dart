import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/widgets/error_view.dart';
import '../accounts/data/accounts_service.dart';
import 'data/credit_applications_service.dart';
import 'data/credit_contract_service.dart';
import 'data/credit_disbursement_service.dart';
import 'data/credit_sign_controller.dart';
import 'data/credits_service.dart';
import 'data/loan_schedule_service.dart';
import 'data/my_credits_service.dart';
import 'models/credit.dart';
import 'models/loan_application.dart';
import 'presentation/credit_application_page.dart';
import 'presentation/credit_contract_page.dart';
import 'presentation/credit_sign_page.dart';
import 'presentation/credit_status_page.dart';
import 'presentation/credits_home_page.dart';
import 'presentation/loan_list_page.dart';
import 'presentation/loan_schedule_page.dart';

/// Rutas del feature `credits` (E3-T04 / HU09 + E3-T09 / HU10).
///
/// Convencion F-T01 (`features/README.md`): el feature NUNCA toca
/// `lib/core/router/app_router.dart`; el orquestador importa esta lista y
/// hace `...creditsRoutes` (mas las fabricas `creditsServiceFactory` y
/// `creditApplicationsServiceFactory`, igual que
/// `transfersServiceFactory`).
///
/// - `/credits` -> [CreditsHomePage] (E3-T23: dos secciones con
///   `SegmentedButton`: `Simulador` (el simulador actual, embebido) +
///   `Mis créditos` (listado del titular)).
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
/// - `/credits/loans` -> [LoanListPage] (mis prestamos desembolsados,
///   E3-T21): filas con saldo de capital y debito automatico.
/// - `/credits/loans/:id` -> [LoanSchedulePage] (cronograma definitivo +
///   toggle de debito automatico + pago de cuota, E3-T21): recibe el
///   `loanId` por path param (o por `extra` como `String`).
///
/// Las paginas reciben los servicios desde las fabricas (cableadas por el
/// orquestador con el `ApiClient` compartido). Si una fabrica es `null`, la
/// pantalla degrada a un error accionable en vez de romper.
final List<GoRoute> creditsRoutes = [
  GoRoute(
    path: '/credits',
    builder: (context, state) {
      // E3-T23: la misma ruta monta las dos secciones. El listado reusa la
      // fábrica ya cableada por el orquestador (el servicio concreto
      // implementa `MyCreditsServiceBase`): sin tocar `app_router.dart`.
      final apps = creditApplicationsServiceFactory?.call();
      return CreditsHomePage(
        simulatorService: creditsServiceFactory?.call(),
        myCreditsService: apps is MyCreditsServiceBase
            ? apps as MyCreditsServiceBase
            : myCreditsServiceFactory?.call(),
      );
    },
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
        disburseService: creditDisbursementServiceFactory?.call(),
        loansService: loanScheduleServiceFactory?.call(),
        accountsService: accountsServiceFactory?.call(),
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
        // M-1 (HU12): disparador de desembolso en el éxito de la firma.
        // Cada fábrica resuelve el servicio con el `ApiClient` compartido;
        // `null` = DI no configurada (la sección degrada a aviso honesto).
        disburseService: creditDisbursementServiceFactory?.call(),
        loansService: loanScheduleServiceFactory?.call(),
        accountsService: accountsServiceFactory?.call(),
      );
    },
  ),
  GoRoute(
    path: '/credits/loans',
    builder: (context, state) {
      return LoanListPage(service: loanScheduleServiceFactory?.call());
    },
    routes: [
      GoRoute(
        path: ':id',
        builder: (context, state) {
          final extra = state.extra;
          return LoanSchedulePage(
            service: loanScheduleServiceFactory?.call(),
            loanId: state.pathParameters['id'] ??
                (extra is String ? extra : null),
          );
        },
      ),
    ],
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
