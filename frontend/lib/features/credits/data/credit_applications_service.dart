import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';
import '../models/loan_application.dart';

/// Contrato del expediente de credito (seam para tests con fake).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente, `docs/05` §6.4):
/// - `POST /credits/applications` (con `Idempotency-Key` obligatoria) ->
///   `201` con dictamen final o `202` con `IN_REVIEW` (bureau caido: no es
///   error 500).
/// - `GET /credits/applications/{id}` -> `200` con el expediente (solo el
///   titular; ajeno/inexistente -> `404` neutro).
///
/// Cliente delgado (`docs/19` §4): envia datos y muestra el dictamen del
/// servidor; no decide elegibilidad ni calcula DTI/score.
abstract class CreditApplicationsServiceBase {
  /// Abre el expediente (crea recurso: exige `Idempotency-Key`).
  ///
  /// [idempotencyKey] reutiliza la clave del intento en curso ante
  /// reintentos (`features/README.md` regla 4); si es `null`, el cliente
  /// genera una nueva.
  Future<LoanApplicationResult> submitApplication({
    required LoanApplicationRequest request,
    String? idempotencyKey,
  });

  /// Estado del expediente (refresco / pull-to-refresh).
  Future<LoanApplicationResult> getApplication(String id);
}

/// Servicio del expediente sobre [ApiClient] (E3-T09, `docs/05` §6.4).
class CreditApplicationsService implements CreditApplicationsServiceBase {
  CreditApplicationsService({required this.api});

  final ApiClient api;

  static Map<String, dynamic> _dataEnvelope(Object? body) {
    if (body is Map<String, dynamic> && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    if (body is Map<String, dynamic>) return body;
    throw const FormatException('Respuesta del expediente invalida');
  }

  @override
  Future<LoanApplicationResult> submitApplication({
    required LoanApplicationRequest request,
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/credits/applications',
      data: request.toJson(),
      idempotencyKey: idempotencyKey,
    );
    return LoanApplicationResult.fromJson(_dataEnvelope(resp.data));
  }

  @override
  Future<LoanApplicationResult> getApplication(String id) async {
    final resp = await api.get('/credits/applications/$id');
    return LoanApplicationResult.fromJson(_dataEnvelope(resp.data));
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `creditsServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con el
/// `ApiClient` compartido; `null` = DI no configurada: las pantallas
/// degradan a un error accionable en vez de romper.
typedef CreditApplicationsServiceFactory
    = CreditApplicationsServiceBase Function();

CreditApplicationsServiceFactory? creditApplicationsServiceFactory;

/// Mensaje UI en espanol para un [ApiException] del expediente.
///
/// Cubre los errores del brief (`docs/05` §6.4 + E3-T09): `VALIDATION_ERROR`
/// (422, campos/consentimiento), `NOT_FOUND` (404 neutro), `CONFLICT` /
/// `IDEMPOTENCY_CONFLICT` (409) y `BUREAU_UNAVAILABLE` (pasa a `En
/// revision`, no es error). Sin PII: nunca incluye el cuerpo del servidor.
String creditApplicationUserMessage(ApiException error) {
  if (error.code == 'BUREAU_UNAVAILABLE') {
    return 'Estamos revisando tu solicitud. '
        'Un analista la verificará. Te avisaremos.';
  }
  switch (error.code) {
    case 'VALIDATION_ERROR':
      return 'Revisa los datos de la solicitud e intentalo de nuevo. '
          'Sin el consentimiento marcado no se puede enviar.';
    case 'NOT_FOUND':
      return 'No encontramos el expediente. '
          'Vuelve e inicia el flujo de nuevo.';
    case 'CONFLICT':
    case 'IDEMPOTENCY_CONFLICT':
    case 'DUPLICATE_REQUEST':
      return 'Esta solicitud ya fue registrada. Revisa el estado del '
          'expediente.';
    default:
      return error.message;
  }
}
