import 'package:dio/dio.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';
import '../models/beneficiary.dart';

/// Parametros de alta/edicion de beneficiario (docs/05 §6.2, E2-T13).
///
/// - Alta (`POST`): `alias` + `accountOrCci` obligatorios; resto opcional.
/// - La cuenta/CCI en claro solo viaja en el cuerpo del request; la respuesta
///   y la lista solo exponen `account_masked`. Jamas se guarda en disco ni se
///   escribe en logs (sin PII).
class BeneficiaryInput {
  const BeneficiaryInput({
    required this.alias,
    required this.accountOrCci,
    this.bankCode,
    this.holderName,
    this.holderDoc,
    this.preapprovedLimitMinor,
    this.currency = 'PEN',
  });

  final String alias;

  /// Cuenta o CCI en claro (solo para el request de alta/edicion).
  final String accountOrCci;
  final String? bankCode;
  final String? holderName;
  final String? holderDoc;
  final int? preapprovedLimitMinor;
  final String currency;

  Map<String, dynamic> toJson() => {
        'alias': alias,
        'account_or_cci': accountOrCci,
        if (bankCode != null && bankCode!.isNotEmpty) 'bank_code': bankCode,
        if (holderName != null && holderName!.isNotEmpty)
          'holder_name': holderName,
        if (holderDoc != null && holderDoc!.isNotEmpty)
          'holder_doc': holderDoc,
        if (preapprovedLimitMinor != null)
          'preapproved_limit_minor': preapprovedLimitMinor,
        'currency': currency,
      };
}

/// Contrato del servicio de beneficiarios (seam para tests con fake).
abstract class BeneficiariesServiceBase {
  /// Lista `ACTIVE` del titular; con [query] busca en el servidor (`?q=`).
  Future<List<Beneficiary>> list({String? query});

  /// Alta con `Idempotency-Key` (docs/05 §6.2: recomendada en el alta).
  Future<Beneficiary> create(BeneficiaryInput input, {String? idempotencyKey});

  /// Edicion parcial (incl. `preapproved_limit_minor`, HU07 CA-03).
  Future<Beneficiary> update(String id, BeneficiaryInput input);

  /// Soft-delete; exige `confirm=true` y solo tras 2xx se refleja en UI.
  Future<Beneficiary> remove(String id);
}

/// Servicio de beneficiarios sobre [ApiClient] (docs/05 §6.2, E2-T13).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente):
/// - `GET /beneficiaries?q=` -> `{"data": [...], "meta": {"total", "q"}}`
/// - `POST /beneficiaries` (+ `Idempotency-Key`) -> `201 {"data": ...}`
/// - `PUT /beneficiaries/{id}` -> `200 {"data": ...}`
/// - `DELETE /beneficiaries/{id}?confirm=true` -> `200 {"data": ...}`
class BeneficiariesService implements BeneficiariesServiceBase {
  BeneficiariesService({required this.api});

  final ApiClient api;

  static List<Beneficiary> _parseList(Object? data) {
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
        .toList();
  }

  static Beneficiary _parseOne(Object? data) {
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return Beneficiary.fromJson(
        Map<String, dynamic>.from(data['data'] as Map),
      );
    }
    if (data is Map<String, dynamic>) {
      return Beneficiary.fromJson(data);
    }
    throw const FormatException('Respuesta de beneficiario invalida');
  }

  @override
  Future<List<Beneficiary>> list({String? query}) async {
    final q = (query ?? '').trim();
    final resp = await api.get(
      '/beneficiaries',
      queryParameters: {if (q.isNotEmpty) 'q': q},
    );
    return _parseList(resp.data);
  }

  @override
  Future<Beneficiary> create(
    BeneficiaryInput input, {
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/beneficiaries',
      data: input.toJson(),
      idempotencyKey: idempotencyKey,
    );
    return _parseOne(resp.data);
  }

  @override
  Future<Beneficiary> update(String id, BeneficiaryInput input) async {
    try {
      final resp = await api.dio.put('/beneficiaries/$id', data: input.toJson());
      return _parseOne(resp.data);
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
  }

  @override
  Future<Beneficiary> remove(String id) async {
    try {
      final resp = await api.dio.delete(
        '/beneficiaries/$id',
        queryParameters: const {'confirm': 'true'},
      );
      return _parseOne(resp.data);
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
  }
}

/// Fabrica inyectable del servicio (mismo patron que `accountsServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con el
/// `ApiClient` compartido; `null` = DI no configurada: las pantallas muestran
/// un error accionable en vez de romper.
typedef BeneficiariesServiceFactory = BeneficiariesServiceBase Function();

BeneficiariesServiceFactory? beneficiariesServiceFactory;
