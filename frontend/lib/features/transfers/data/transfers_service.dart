import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';
import '../../accounts/models/account.dart';
import '../../beneficiaries/models/beneficiary.dart';
import '../models/transfer.dart';

/// Contrato del servicio de transferencias (seam para tests con fake).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente, docs/05 §6.3):
/// - `POST /transfers/own` (+ `Idempotency-Key`) -> `201 {data, meta}`
/// - `POST /transfers/third-party` (+ `Idempotency-Key`) -> `201 {data, meta}`
/// - `POST /transfers/interbank` (+ `Idempotency-Key`) -> `202 {data, meta}`
/// - `GET /transactions/{id}` -> `200 {data, meta}` (seguimiento/polling)
/// - `GET /transactions/{id}/receipt` -> `200` PDF (comprobante)
///
/// Cliente delgado (docs/19 §4): captura, envia y presenta; no calcula
/// saldos ni comisiones. Todos los POST que mueven dinero usan
/// `ApiClient.postWithIdempotency`; el reintento del mismo intento reutiliza
/// la misma clave (se genera una por borrador si no se pasa).
abstract class TransfersServiceBase {
  /// Cuentas propias para el selector de origen (`GET /accounts`).
  Future<List<Account>> accounts();

  /// Beneficiarios `ACTIVE` para el destino a tercero (`GET /beneficiaries`).
  Future<List<Beneficiary>> beneficiaries({String? query});

  /// Transferencia entre cuentas propias (sin comision).
  Future<TransferResult> createOwnTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  });

  /// Transferencia a tercero del mismo banco.
  ///
  /// `beneficiaryId` viaja en `metadata["beneficiary_id"]` para la exencion
  /// por tope (E2-T14); el display usa la agenda (datos enmascarados).
  Future<TransferResult> createThirdPartyTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    int? feeMinor,
    String? beneficiaryId,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  });

  /// Transferencia interbancaria (saga `202` + seguimiento).
  ///
  /// El titular/CCI lo valida el backend contra la CCE antes de mover
  /// dinero (E2-T08): `HOLDER_NOT_FOUND` (404 neutro) y
  /// `HOLDER_LOOKUP_UNAVAILABLE` (503) se propagan como [ApiException]
  /// para los estados del formulario CCI.
  Future<TransferResult> createInterbankTransfer({
    required String sourceAccountId,
    required int amountMinor,
    required String cci,
    String currency = 'PEN',
    String? holderName,
    String? bankCode,
    int? feeMinor,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  });

  /// Estado y detalle (`GET /transactions/{id}`, polling interbancario).
  Future<TransactionDetail> getTransaction(String transactionId);

  /// Comprobante PDF (solo si la transaccion esta emitible).
  Future<ReceiptResult> getReceipt(String transactionId);
}

/// Servicio de transferencias sobre [ApiClient] (E2-T11, docs/05 §6.3).
class TransfersService implements TransfersServiceBase {
  TransfersService({required this.api});

  final ApiClient api;

  static const _uuid = Uuid();

  String _key(String? provided) =>
      (provided == null || provided.isEmpty) ? _uuid.v4() : provided;

  static Map<String, dynamic> _dataEnvelope(Object? body) {
    if (body is Map<String, dynamic> && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    if (body is Map<String, dynamic>) return body;
    throw const FormatException('Respuesta de transferencia invalida');
  }

  static bool _replayed(Object? body) {
    if (body is Map<String, dynamic> && body['meta'] is Map) {
      return (body['meta'] as Map)['replayed'] == true;
    }
    return false;
  }

  @override
  Future<List<Account>> accounts() async {
    final resp = await api.get('/accounts');
    final data = resp.data;
    if (data is Map<String, dynamic> && data['data'] is List) {
      return (data['data'] as List)
          .whereType<Map>()
          .map((e) => Account.fromJson(Map<String, dynamic>.from(e)))
          .toList();
    }
    if (data is List) {
      return data
          .whereType<Map>()
          .map((e) => Account.fromJson(Map<String, dynamic>.from(e)))
          .toList();
    }
    return const [];
  }

  @override
  Future<List<Beneficiary>> beneficiaries({String? query}) async {
    final q = (query ?? '').trim();
    final resp = await api.get(
      '/beneficiaries',
      queryParameters: {if (q.isNotEmpty) 'q': q},
    );
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
        .map((e) => Beneficiary.fromJson(Map<String, dynamic>.from(e)))
        .where((b) => b.status.toUpperCase() != 'DELETED')
        .toList();
  }

  @override
  Future<TransferResult> createOwnTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/transfers/own',
      data: {
        'source_account_id': sourceAccountId,
        'target_account_id': targetAccountId,
        'amount_minor': amountMinor,
        'currency': currency,
        'metadata': ?metadata,
      },
      idempotencyKey: _key(idempotencyKey),
    );
    return TransferResult.fromJson(
      _dataEnvelope(resp.data),
      replayed: _replayed(resp.data),
    );
  }

  @override
  Future<TransferResult> createThirdPartyTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    int? feeMinor,
    String? beneficiaryId,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) async {
    final meta = <String, dynamic>{...?metadata};
    if (beneficiaryId != null && beneficiaryId.isNotEmpty) {
      meta['beneficiary_id'] = beneficiaryId;
    }
    final resp = await api.postWithIdempotency(
      '/transfers/third-party',
      data: {
        'source_account_id': sourceAccountId,
        'target_account_id': targetAccountId,
        'amount_minor': amountMinor,
        'currency': currency,
        'fee_minor': ?feeMinor,
        if (meta.isNotEmpty) 'metadata': meta,
      },
      idempotencyKey: _key(idempotencyKey),
    );
    return TransferResult.fromJson(
      _dataEnvelope(resp.data),
      replayed: _replayed(resp.data),
    );
  }

  @override
  Future<TransferResult> createInterbankTransfer({
    required String sourceAccountId,
    required int amountMinor,
    required String cci,
    String currency = 'PEN',
    String? holderName,
    String? bankCode,
    int? feeMinor,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/transfers/interbank',
      data: {
        'source_account_id': sourceAccountId,
        'amount_minor': amountMinor,
        'currency': currency,
        'cci': normalizeCci(cci),
        if (holderName != null && holderName.isNotEmpty)
          'holder_name': holderName,
        if (bankCode != null && bankCode.isNotEmpty) 'bank_code': bankCode,
        'fee_minor': ?feeMinor,
        'metadata': ?metadata,
      },
      idempotencyKey: _key(idempotencyKey),
    );
    return TransferResult.fromJson(
      _dataEnvelope(resp.data),
      replayed: _replayed(resp.data),
    );
  }

  @override
  Future<TransactionDetail> getTransaction(String transactionId) async {
    final resp = await api.get('/transactions/$transactionId');
    return TransactionDetail.fromJson(_dataEnvelope(resp.data));
  }

  @override
  Future<ReceiptResult> getReceipt(String transactionId) async {
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
        'comprobante-$transactionId.pdf';
    return ReceiptResult(
      bytes: bytes,
      suggestedFilename: filename,
      contentType: contentType,
    );
  }

  /// Extrae `filename="..."` de `attachment; filename="..."`.
  static String? _filenameFromDisposition(String disposition) {
    final match = RegExp(r'filename="([^"]+)"').firstMatch(disposition);
    return match?.group(1);
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `accountsServiceFactory` / `beneficiariesServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con el
/// `ApiClient` compartido; `null` = DI no configurada: las pantallas muestran
/// un error accionable en vez de romper (los 4 estados siguen cubiertos).
typedef TransfersServiceFactory = TransfersServiceBase Function();

TransfersServiceFactory? transfersServiceFactory;

/// Mensaje UI en espanol para un [ApiException] de transferencias.
///
/// Cubre los errores del brief (docs/05 §4 + E2-T09): `INSUFFICIENT_FUNDS`
/// (201/202 con `REJECTED`, sin debitar), `NOT_FOUND`, `NOT_AUTHORIZED`,
/// `VALIDATION_ERROR`, `TRANSFER_TIMEOUT`/`INTERBANK_UNAVAILABLE`,
/// conflictos de idempotencia (`409`) y CCI (`HOLDER_NOT_FOUND` neutro,
/// `HOLDER_LOOKUP_UNAVAILABLE` reintentable). Sin PII: nunca incluye el CCI,
/// el titular en claro ni el cuerpo del servidor.
String transferUserMessage(ApiException error) {
  switch (error.code) {
    case 'INSUFFICIENT_FUNDS':
      return 'No tienes saldo suficiente para completar la operacion. '
          'Tu dinero esta seguro: no se debito nada.';
    case 'DAILY_LIMIT_EXCEEDED':
      return 'Superaste tu limite diario. Podras operar de nuevo manana.';
    case 'BIOMETRIC_PREVENTIVE_BLOCK':
      return 'Protegimos tu cuenta temporalmente. Verifica tu identidad '
          'para continuar.';
    case 'EXPIRED_NONCE':
      return 'La solicitud expiro. Vuelve a intentarlo con una '
          'nueva autorizacion.';
    case 'HOLDER_NOT_FOUND':
      return 'No pudimos validar el titular con los datos ingresados. '
          'Verifica el CCI directamente con el destinatario antes de continuar.';
    case 'HOLDER_LOOKUP_UNAVAILABLE':
    case 'HOLDER_LOOKUP_TIMEOUT':
      return 'Servicio interbancario no disponible temporalmente. '
          'Puedes reintentar la verificacion.';
    case 'TRANSFER_TIMEOUT':
    case 'INTERBANK_UNAVAILABLE':
    case 'INTERBANK_REJECTED':
      return 'La transferencia interbancaria sigue en seguimiento. '
          'Puedes consultar su estado sin riesgo de duplicarla.';
    case 'VALIDATION_ERROR':
      return 'Revisa los datos ingresados e intentalo de nuevo.';
    case 'NOT_FOUND':
      return 'No encontramos la cuenta destino. Verifica los datos.';
    case 'NOT_AUTHORIZED':
      return 'No tienes permiso para esta operacion.';
    case 'CONFLICT':
    case 'IDEMPOTENCY_CONFLICT':
    case 'DUPLICATE_REQUEST':
      return 'Esta operacion ya fue registrada. Revisa tus movimientos '
          'antes de reintentarla.';
    case 'RATE_LIMITED':
      return 'Demasiados intentos. Espera un momento e intentalo de nuevo.';
    default:
      return error.message;
  }
}
