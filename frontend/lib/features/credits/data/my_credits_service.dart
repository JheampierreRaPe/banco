import '../../../core/http/api_client.dart';
import '../models/own_application.dart';

/// Contrato del listado "Mis créditos" (E3-T23, HU10/HU11; seam para tests).
///
/// Endpoint (base `/api/v1` ya incluida en el cliente, `docs/05` §6.4):
/// - `GET /credits/applications` (sin `status`) -> expedientes del titular
///   autenticado (`sub` del JWT; `user_id` nunca del cliente), orden
///   `created_at DESC`; sin expedientes -> `data: []` (no `404`).
///
/// Lectura sin `Idempotency-Key` (no crea recurso ni mueve dinero).
/// Cliente delgado (`docs/19` §4): pide la página y la muestra TAL CUAL.
abstract class MyCreditsServiceBase {
  /// Lista los expedientes del titular (paginación opcional del servidor).
  Future<OwnApplicationsPage> listMine({int? limit, int? offset});
}

/// Servicio de "Mis créditos" sobre [ApiClient].
///
/// i3 (auditoría): NO es código muerto. Es la implementación concreta
/// standalone (cubierta por `test/credit_mine_test.dart`) y el respaldo
/// de la ruta `/credits` (`credits_routes.dart`: se usa cuando el
/// servicio de `creditApplicationsServiceFactory` no implementa
/// [MyCreditsServiceBase]). Se conserva a propósito.
class MyCreditsService implements MyCreditsServiceBase {
  MyCreditsService({required this.api});

  final ApiClient api;

  @override
  Future<OwnApplicationsPage> listMine({int? limit, int? offset}) async {
    final query = <String, dynamic>{};
    if (limit != null) query['limit'] = limit;
    if (offset != null) query['offset'] = offset;
    final resp = await api.get(
      '/credits/applications',
      queryParameters: query.isEmpty ? null : query,
    );
    final body = resp.data;
    if (body is Map<String, dynamic>) return OwnApplicationsPage.fromJson(body);
    if (body is Map) {
      return OwnApplicationsPage.fromJson(Map<String, dynamic>.from(body));
    }
    throw const FormatException('Respuesta del listado inválida');
  }
}

/// Fábrica inyectable (mismo patrón que `creditsServiceFactory`).
///
/// i3 (auditoría): NO es código muerto. La usa `credits_routes.dart:57`
/// como respaldo cuando `creditApplicationsServiceFactory` no aporta el
/// listado. Se conserva a propósito.
///
/// La asigna el orquestador con el `ApiClient` compartido; `null` = DI no
/// configurada (la UI degrada a error accionable). Ver nota en
/// `credits_routes.dart`: hoy se resuelve desde
/// `creditApplicationsServiceFactory` (ya cableada) porque el servicio
/// concreto implementa ambas interfaces.
typedef MyCreditsServiceFactory = MyCreditsServiceBase Function();

MyCreditsServiceFactory? myCreditsServiceFactory;
