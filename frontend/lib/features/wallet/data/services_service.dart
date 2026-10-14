import '../../../core/http/api_client.dart';
import '../models/services.dart';

/// Contrato del servicio de servicios y recargas (seam para tests con
/// fake).
abstract class ServicesServiceBase {
  /// Catalogo filtrable (`GET /services/catalog`, solo lectura, < 2 s).
  Future<List<Biller>> catalog({String? category, String? query});

  /// Paga un servicio (`POST /services/payments` + `Idempotency-Key`
  /// obligatoria; mueve dinero).
  Future<ServiceOperationResult> payService({
    required String billerId,
    required String supplyRef,
    required int amountMinor,
    required String linkId,
    String? currency,
    String? idempotencyKey,
  });

  /// Recarga celular (`POST /topups` + `Idempotency-Key` obligatoria;
  /// mueve dinero).
  Future<ServiceOperationResult> topup({
    required String operatorRef,
    required String phone,
    required int amountMinor,
    required String linkId,
    String? currency,
    String? idempotencyKey,
  });

  /// Registra un servicio para recordatorios
  /// (`POST /services/subscriptions`; sin `Idempotency-Key`: no mueve
  /// dinero; idempotente por naturaleza con `replayed`).
  Future<ServiceSubscription> subscribe({
    required String billerId,
    required String supplyRef,
    int? notifyDaysBefore,
  });
}

/// Servicio de servicios y recargas sobre [ApiClient] (E4-T19,
/// `docs/05` §6.5).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente):
/// - `GET /services/catalog` (`category?`/`q?`) -> `200 {"data": [...]}`.
/// - `POST /services/payments` (+ `Idempotency-Key` obligatoria) ->
///   `201 {"data": {payment_id, transaction_id, status, receipt?, ...}}`.
/// - `POST /topups` (+ `Idempotency-Key` obligatoria) -> `201 {...}`.
/// - `POST /services/subscriptions` (sin clave) -> `201 {"data":
///   {subscription_id, biller_id, supply_ref, ...}}`.
///
/// > Sin PII en logs: este servicio no loguea cuerpos (`supply_ref` y
/// > `phone` viajan solo en el request y se muestran enmascarados desde
/// > el `receipt` del servidor).
class ServicesService implements ServicesServiceBase {
  ServicesService({required this.api});

  final ApiClient api;

  static List<Biller> _parseCatalog(Object? data) {
    List<Object?> raw;
    if (data is Map<String, dynamic> && data['data'] is List) {
      raw = data['data'] as List;
    } else if (data is List) {
      raw = data;
    } else {
      throw const FormatException('Respuesta de catalogo invalida');
    }
    return raw
        .whereType<Map>()
        .map((e) => Biller.fromJson(Map<String, dynamic>.from(e)))
        .toList();
  }

  static ServiceOperationResult _parseResult(Object? data) {
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return ServiceOperationResult.fromJson(
        Map<String, dynamic>.from(data['data'] as Map),
        replayed: (data['meta'] is Map) &&
            (data['meta'] as Map)['replayed'] == true,
      );
    }
    if (data is Map<String, dynamic>) {
      return ServiceOperationResult.fromJson(data);
    }
    throw const FormatException('Respuesta de operacion invalida');
  }

  static ServiceSubscription _parseSubscription(Object? data) {
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return ServiceSubscription.fromJson(
        Map<String, dynamic>.from(data['data'] as Map),
      );
    }
    if (data is Map<String, dynamic>) {
      return ServiceSubscription.fromJson(data);
    }
    throw const FormatException('Respuesta de suscripcion invalida');
  }

  @override
  Future<List<Biller>> catalog({String? category, String? query}) async {
    final params = <String, dynamic>{};
    if (category != null && category.isNotEmpty) {
      params['category'] = category;
    }
    if (query != null && query.isNotEmpty) params['q'] = query;
    final resp = await api.get('/services/catalog', queryParameters: params);
    return _parseCatalog(resp.data);
  }

  @override
  Future<ServiceOperationResult> payService({
    required String billerId,
    required String supplyRef,
    required int amountMinor,
    required String linkId,
    String? currency,
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/services/payments',
      data: {
        'biller_id': billerId,
        'supply_ref': supplyRef,
        'amount_minor': amountMinor,
        'currency': (currency ?? 'PEN').toUpperCase(),
        'link_id': linkId,
      },
      idempotencyKey: idempotencyKey,
    );
    return _parseResult(resp.data);
  }

  @override
  Future<ServiceOperationResult> topup({
    required String operatorRef,
    required String phone,
    required int amountMinor,
    required String linkId,
    String? currency,
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/topups',
      data: {
        'operator': operatorRef,
        'phone': phone,
        'amount_minor': amountMinor,
        'currency': (currency ?? 'PEN').toUpperCase(),
        'link_id': linkId,
      },
      idempotencyKey: idempotencyKey,
    );
    return _parseResult(resp.data);
  }

  @override
  Future<ServiceSubscription> subscribe({
    required String billerId,
    required String supplyRef,
    int? notifyDaysBefore,
  }) async {
    final body = <String, dynamic>{
      'biller_id': billerId,
      'supply_ref': supplyRef,
    };
    if (notifyDaysBefore != null) {
      body['notify_days_before'] = notifyDaysBefore;
    }
    final resp = await api.post('/services/subscriptions', data: body);
    return _parseSubscription(resp.data);
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `walletServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con
/// el `ApiClient` compartido; `null` = DI no configurada: las pantallas
/// muestran un error accionable en vez de romper.
typedef ServicesServiceFactory = ServicesServiceBase Function();

ServicesServiceFactory? servicesServiceFactory;
