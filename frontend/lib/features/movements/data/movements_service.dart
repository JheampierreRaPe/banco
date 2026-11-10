import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';
import '../models/movement.dart';

/// Contrato del servicio de historial + reverso (seam para tests con fake).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente):
/// - `GET /transactions?page=&page_size=&date_from=&date_to=&type=&status=`
///   -> `200 {data, meta}` (E2-T24, solo lectura, sin `Idempotency-Key`).
/// - `GET /transactions/{id}` -> `200 {data, meta}` (detalle + `tracking`).
/// - `POST /transactions/{id}/reverse` (+ `Idempotency-Key`,
///   `{reason?}` <= 280) -> `200 {data, meta}` o `409` (E2-T25).
/// - `GET /transactions/{id}/receipt` -> `200` PDF (constancia, E2-T10).
///
/// Cliente delgado (docs/19 §4): captura, envia y presenta; no calcula
/// saldos ni comisiones. El unico POST (reverso) usa
/// `ApiClient.postWithIdempotency`; el reintento del mismo intento reutiliza
/// la misma clave.
abstract class MovementsServiceBase {
  /// Historial paginado con filtros del servidor.
  Future<PaginatedMovements> list({
    int page = 1,
    int pageSize = 10,
    MovementFilters filters = const MovementFilters(),
  });

  /// Detalle + seguimiento (`GET /transactions/{id}`).
  Future<MovementDetail> detail(String transactionId);

  /// Reverso con motivo opcional (<= 280) e idempotencia.
  Future<ReversalResult> reverse(
    String transactionId, {
    String? reason,
    String? idempotencyKey,
  });

  /// Constancia PDF (emision: solo si el detalle la admite).
  Future<MovementReceipt> receipt(String transactionId);
}

/// Constancia PDF (`GET /transactions/{id}/receipt`, E2-T10).
///
/// Solo bytes + nombre sugerido: el guardado nativo queda pendiente
/// (mismo patron que `ReceiptResult` de transfers; sin dependencias nuevas).
class MovementReceipt {
  const MovementReceipt({
    required this.bytes,
    required this.suggestedFilename,
    required this.contentType,
  });

  final List<int> bytes;
  final String suggestedFilename;
  final String contentType;
}

/// Servicio de historial + reverso sobre [ApiClient].
class MovementsService implements MovementsServiceBase {
  MovementsService({required this.api});

  final ApiClient api;

  static const _uuid = Uuid();

  static Map<String, dynamic> _dataEnvelope(Object? body) {
    if (body is Map<String, dynamic> && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    if (body is Map<String, dynamic>) return body;
    throw const FormatException('Respuesta de movimientos invalida');
  }

  static bool _replayed(Object? body) {
    if (body is Map<String, dynamic> && body['meta'] is Map) {
      return (body['meta'] as Map)['replayed'] == true;
    }
    return false;
  }

  @override
  Future<PaginatedMovements> list({
    int page = 1,
    int pageSize = 10,
    MovementFilters filters = const MovementFilters(),
  }) async {
    final resp = await api.get(
      '/transactions',
      queryParameters: filters.toQueryParams(page: page, pageSize: pageSize),
    );
    final body = resp.data;
    if (body is Map<String, dynamic>) {
      return PaginatedMovements.fromJson(body);
    }
    throw const FormatException('Respuesta de movimientos invalida');
  }

  @override
  Future<MovementDetail> detail(String transactionId) async {
    final resp = await api.get('/transactions/$transactionId');
    return MovementDetail.fromJson(_dataEnvelope(resp.data));
  }

  @override
  Future<ReversalResult> reverse(
    String transactionId, {
    String? reason,
    String? idempotencyKey,
  }) async {
    final clean = (reason ?? '').trim();
    final resp = await api.postWithIdempotency(
      '/transactions/$transactionId/reverse',
      data: {
        if (clean.isNotEmpty) 'reason': clean,
      },
      idempotencyKey: (idempotencyKey == null || idempotencyKey.isEmpty)
          ? _uuid.v4()
          : idempotencyKey,
    );
    return ReversalResult.fromJson(
      _dataEnvelope(resp.data),
      replayed: _replayed(resp.data),
    );
  }

  @override
  Future<MovementReceipt> receipt(String transactionId) async {
    Response<List<int>> resp;
    try {
      final raw = await api.dio.get<List<int>>(
        '/transactions/$transactionId/receipt',
        options: Options(responseType: ResponseType.bytes),
      );
      resp = raw;
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
    final bytes = resp.data ?? Uint8List(0);
    final contentType =
        resp.headers.map['content-type']?.firstOrNull ?? 'application/pdf';
    final disposition =
        resp.headers.map['content-disposition']?.firstOrNull ?? '';
    final filename = _filenameFromDisposition(disposition) ??
        'constancia-$transactionId.pdf';
    return MovementReceipt(
      bytes: bytes,
      suggestedFilename: filename,
      contentType: contentType,
    );
  }

  static String? _filenameFromDisposition(String disposition) {
    final match = RegExp(r'filename="([^"]+)"').firstMatch(disposition);
    return match?.group(1);
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `beneficiariesServiceFactory` / `transfersServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con el
/// `ApiClient` compartido; `null` = DI no configurada: las pantallas muestran
/// un error accionable en vez de romper (los 4 estados siguen cubiertos).
typedef MovementsServiceFactory = MovementsServiceBase Function();

MovementsServiceFactory? movementsServiceFactory;

/// Mensaje UI en espanol para un [ApiException] de historial/reverso.
///
/// Sin PII: nunca incluye montos, cuentas ni el motivo en claro.
String movementUserMessage(ApiException error) {
  switch (error.code) {
    case 'CONFLICT':
    case 'IDEMPOTENCY_CONFLICT':
    case 'DUPLICATE_REQUEST':
      return 'Esta operación ya fue reversada o está en conflicto. '
          'Revisa el detalle antes de reintentarla.';
    case 'VALIDATION_ERROR':
      return 'Revisa los datos ingresados e inténtalo de nuevo.';
    case 'NOT_FOUND':
      return 'No encontramos ese movimiento.';
    case 'NOT_AUTHORIZED':
      return 'No tienes permiso para esta operación.';
    case 'RATE_LIMITED':
      return 'Demasiados intentos. Espera un momento e inténtalo de nuevo.';
    default:
      return error.message;
  }
}
