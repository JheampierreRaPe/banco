// Servicio del juego contractual y la firma (E3-T15, HU11).
//
// Contrato (`docs/05` §6.4, detalle E3-T12/E3-T14):
// - `GET /credits/applications/{id}/contract` (solo lectura, sin
//   `Idempotency-Key`; `?format=json` por defecto o
//   `?format=pdf&doc=contrato|pagare|resumen` para el PDF).
// - `POST /credits/applications/{id}/sign/challenge` (solo rota el reto,
//   sin `Idempotency-Key`; repetir reemite).
// - `POST /credits/applications/{id}/sign` (dos vias mutuamente
//   excluyentes: `{device_id, signature[, nonce]}` o `{pin}`; con
//   `Idempotency-Key` obligatoria).
//
// Cliente delgado (`docs/19` §4): transporta y presenta; no interpreta el
// contrato ni recalcula cifras. Sin PII ni firmas en logs.
library;

import 'dart:typed_data';

import 'package:dio/dio.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';
import '../models/credit_contract.dart';

/// PDF descargado del servidor (bytes en memoria + nombre sugerido).
class ContractPdf {
  const ContractPdf({required this.bytes, required this.suggestedFilename});

  final List<int> bytes;
  final String suggestedFilename;
}

/// Contrato del servicio contractual (seam para tests con fake).
abstract class CreditContractServiceBase {
  /// Metadatos del juego (los 3 documentos). Solo lectura.
  Future<ContractBundle> getContract(String applicationId);

  /// Descarga un documento en PDF (`doc`: `contrato|pagare|resumen`).
  Future<ContractPdf> downloadDocument(String applicationId, String doc);

  /// Emite/rota el reto (`nonce` de un solo uso). Sin idempotencia.
  Future<SignChallenge> requestChallenge(
    String applicationId, {
    String? deviceId,
  });

  /// Firma con biometria (`{device_id, signature, nonce}`).
  ///
  /// [idempotencyKey] reutiliza la clave del intento en curso ante
  /// reintentos; si es `null`, el cliente genera una nueva.
  Future<ContractSignatureResult> signWithBiometrics({
    required String applicationId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  });

  /// Firma con PIN (`{pin}`, contingencia). Misma regla de [idempotencyKey].
  Future<ContractSignatureResult> signWithPin({
    required String applicationId,
    required String pin,
    String? idempotencyKey,
  });
}

/// Servicio HTTP contractual sobre [ApiClient] (E3-T15, `docs/05` §6.4).
class CreditContractService implements CreditContractServiceBase {
  CreditContractService({required this.api});

  final ApiClient api;

  static Map<String, dynamic> _dataEnvelope(Object? body) {
    if (body is Map<String, dynamic> && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    if (body is Map<String, dynamic>) return body;
    throw const FormatException('Respuesta del contrato invalida');
  }

  static bool _replayed(Object? body) {
    if (body is Map<String, dynamic> && body['meta'] is Map) {
      return (body['meta'] as Map)['replayed'] == true;
    }
    return false;
  }

  @override
  Future<ContractBundle> getContract(String applicationId) async {
    final resp = await api.get('/credits/applications/$applicationId/contract');
    return ContractBundle.fromJson(_dataEnvelope(resp.data));
  }

  @override
  Future<ContractPdf> downloadDocument(
    String applicationId,
    String doc,
  ) async {
    Response<List<int>> resp;
    try {
      resp = await api.dio.get<List<int>>(
        '/credits/applications/$applicationId/contract',
        queryParameters: {'format': 'pdf', 'doc': doc},
        options: Options(responseType: ResponseType.bytes),
      );
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
    final bytes = resp.data ?? Uint8List(0);
    final disposition =
        resp.headers.map['content-disposition']?.firstOrNull ?? '';
    final filename = _filenameFromDisposition(disposition) ?? '$doc.pdf';
    return ContractPdf(bytes: bytes, suggestedFilename: filename);
  }

  @override
  Future<SignChallenge> requestChallenge(
    String applicationId, {
    String? deviceId,
  }) async {
    final clean = (deviceId ?? '').trim();
    final resp = await api.post(
      '/credits/applications/$applicationId/sign/challenge',
      data: clean.isEmpty ? {} : {'device_id': clean},
    );
    return SignChallenge.fromJson(_dataEnvelope(resp.data));
  }

  @override
  Future<ContractSignatureResult> signWithBiometrics({
    required String applicationId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/credits/applications/$applicationId/sign',
      data: {
        'device_id': deviceId,
        'signature': signature,
        'nonce': nonce,
      },
      idempotencyKey: idempotencyKey,
    );
    return ContractSignatureResult.fromJson(
      _dataEnvelope(resp.data),
      replayed: _replayed(resp.data),
    );
  }

  @override
  Future<ContractSignatureResult> signWithPin({
    required String applicationId,
    required String pin,
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/credits/applications/$applicationId/sign',
      data: {'pin': pin},
      idempotencyKey: idempotencyKey,
    );
    return ContractSignatureResult.fromJson(
      _dataEnvelope(resp.data),
      replayed: _replayed(resp.data),
    );
  }

  /// Extrae `filename="..."` de `attachment; filename="..."`.
  static String? _filenameFromDisposition(String disposition) {
    final match = RegExp(r'filename="([^"]+)"').firstMatch(disposition);
    return match?.group(1);
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `creditApplicationsServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con el
/// `ApiClient` compartido; `null` = DI no configurada: las pantallas
/// degradan a un error accionable en vez de romper.
typedef CreditContractServiceFactory = CreditContractServiceBase Function();

CreditContractServiceFactory? creditContractServiceFactory;

/// Mensaje UI en espanol para un [ApiException] contractual (E3-T15).
///
/// Cubre los errores del brief (`docs/05` §6.4 + spec §6): `EXPIRED_NONCE`
/// (rotar y reintentar), `NOT_AUTHORIZED` (generico, sin oraculo),
/// `CONFLICT` (ya firmado), `NOT_FOUND` (neutro), `VALIDATION_ERROR` y
/// `EXPORT_FORMAT_NOT_SUPPORTED` (501 en descarga). Sin PII: nunca incluye
/// el cuerpo del servidor.
String creditContractUserMessage(ApiException error) {
  if (error.code == 'EXPORT_FORMAT_NOT_SUPPORTED' ||
      error.statusCode == 501) {
    return 'Exportación no disponible por ahora.';
  }
  switch (error.code) {
    case 'EXPIRED_NONCE':
      return 'Tu sesión de firma venció. Estamos generando un nuevo reto…';
    case 'NOT_AUTHORIZED':
      return 'No pudimos verificar tu identidad. '
          'Reintenta o usa tu PIN.';
    case 'CONFLICT':
    case 'IDEMPOTENCY_CONFLICT':
    case 'DUPLICATE_REQUEST':
      return 'Este contrato ya fue firmado. Revisa tu correo.';
    case 'NOT_FOUND':
      return 'No encontramos el contrato. '
          'Vuelve e inicia el flujo de nuevo.';
    case 'VALIDATION_ERROR':
      return 'Revisa los datos ingresados e intentalo de nuevo.';
    default:
      return error.message;
  }
}

/// Mensaje de biometria cancelada/fallida (sin revelar el motivo exacto).
const String creditBiometricFallbackMessage =
    'No pudimos verificar tu biometría. Reintenta o usa tu PIN.';

/// Mensaje cuando la biometria no esta disponible en el dispositivo.
const String creditBiometricUnavailableMessage =
    'Este dispositivo no tiene biometría disponible. Continúa con tu PIN.';

/// Mensaje de PIN invalido (formato local o rechazo del servidor).
const String creditPinInvalidMessage =
    'PIN incorrecto. Verifica e intenta de nuevo.';
