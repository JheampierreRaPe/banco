import 'dart:typed_data';

import 'package:dio/dio.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';
import '../models/credit.dart';

/// Contrato del servicio del simulador (seam para tests con fake).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente, docs/05 §6.4):
/// - `GET /credits/products` -> `200 {data, meta}` (catalogo habilitado).
/// - `POST /credits/simulate` -> `200 {data, meta}` (cuota + cronograma;
///   calculo puro sin `Idempotency-Key`: no mueve dinero ni crea recurso).
/// - `POST /credits/simulate/export` -> `200` PDF del cronograma (mismo
///   cuerpo y validaciones que `simulate`).
///
/// Cliente delgado (docs/19 §4): captura parametros, envia y presenta; no
/// calcula cuotas, TEA, TCEA ni saldos.
abstract class CreditsServiceBase {
  /// Productos habilitados con tasas y topes.
  Future<List<LoanProduct>> products();

  /// Simula cuota y cronograma contra el backend (con debounce en la UI).
  Future<SimulationResult> simulate({
    required String productId,
    required int amountMinor,
    required int termMonths,
    required bool insurance,
  });

  /// Exporta el cronograma simulado en PDF (mismo cuerpo que [simulate]).
  Future<SimulationExport> exportSimulation({
    required String productId,
    required int amountMinor,
    required int termMonths,
    required bool insurance,
  });
}

/// Servicio del simulador sobre [ApiClient] (E3-T04, docs/05 §6.4).
class CreditsService implements CreditsServiceBase {
  CreditsService({required this.api});

  final ApiClient api;

  static Map<String, dynamic> _dataEnvelope(Object? body) {
    if (body is Map<String, dynamic> && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    if (body is Map<String, dynamic>) return body;
    throw const FormatException('Respuesta del simulador invalida');
  }

  static Map<String, dynamic> _simulateBody({
    required String productId,
    required int amountMinor,
    required int termMonths,
    required bool insurance,
  }) {
    return {
      'product_id': productId,
      'amount_minor': amountMinor,
      'term_months': termMonths,
      'insurance': insurance,
    };
  }

  @override
  Future<List<LoanProduct>> products() async {
    final resp = await api.get('/credits/products');
    final data = resp.data;
    List raw;
    if (data is Map<String, dynamic> && data['data'] is List) {
      raw = data['data'] as List;
    } else if (data is List) {
      raw = data;
    } else {
      return const [];
    }
    return raw
        .whereType<Map>()
        .map((e) => LoanProduct.fromJson(Map<String, dynamic>.from(e)))
        .toList();
  }

  @override
  Future<SimulationResult> simulate({
    required String productId,
    required int amountMinor,
    required int termMonths,
    required bool insurance,
  }) async {
    final resp = await api.post(
      '/credits/simulate',
      data: _simulateBody(
        productId: productId,
        amountMinor: amountMinor,
        termMonths: termMonths,
        insurance: insurance,
      ),
    );
    return SimulationResult.fromJson(_dataEnvelope(resp.data));
  }

  @override
  Future<SimulationExport> exportSimulation({
    required String productId,
    required int amountMinor,
    required int termMonths,
    required bool insurance,
  }) async {
    Response<List<int>> resp;
    try {
      final raw = await api.dio.post<List<int>>(
        '/credits/simulate/export',
        data: _simulateBody(
          productId: productId,
          amountMinor: amountMinor,
          termMonths: termMonths,
          insurance: insurance,
        ),
        options: Options(responseType: ResponseType.bytes),
      );
      resp = raw;
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
    final bytes = resp.data ?? Uint8List(0);
    final disposition =
        resp.headers.map['content-disposition']?.firstOrNull ?? '';
    final filename = _filenameFromDisposition(disposition) ??
        'simulacion-credito.pdf';
    return SimulationExport(
      bytes: bytes,
      suggestedFilename: filename,
    );
  }

  /// Extrae `filename="..."` de `attachment; filename="..."`.
  static String? _filenameFromDisposition(String disposition) {
    final match = RegExp(r'filename="([^"]+)"').firstMatch(disposition);
    return match?.group(1);
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `transfersServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con el
/// `ApiClient` compartido; `null` = DI no configurada: la pantalla muestra
/// un error accionable en vez de romper (los 4 estados siguen cubiertos).
typedef CreditsServiceFactory = CreditsServiceBase Function();

CreditsServiceFactory? creditsServiceFactory;

/// PDF exportado del simulador (bytes + nombre sugerido del servidor).
class SimulationExport {
  const SimulationExport({
    required this.bytes,
    required this.suggestedFilename,
  });

  final List<int> bytes;
  final String suggestedFilename;
}

/// Mensaje UI en espanol para un [ApiException] del simulador.
///
/// Cubre los errores del brief (docs/05 §6.4 + E3-T04): `VALIDATION_ERROR`
/// (422, monto/plazo fuera de rango), `NOT_FOUND` (producto inexistente) y
/// `EXPORT_FORMAT_NOT_SUPPORTED` (501 del export). Sin PII: nunca incluye el
/// cuerpo del servidor.
String creditUserMessage(ApiException error) {
  if (error.code == 'EXPORT_FORMAT_NOT_SUPPORTED' ||
      error.statusCode == 501) {
    return 'Exportación no disponible por ahora.';
  }
  switch (error.code) {
    case 'VALIDATION_ERROR':
      return 'Revisa el monto y el plazo e intentalo de nuevo.';
    case 'NOT_FOUND':
      return 'El producto de credito ya no esta disponible. '
          'Elige otro producto.';
    default:
      return error.message;
  }
}
