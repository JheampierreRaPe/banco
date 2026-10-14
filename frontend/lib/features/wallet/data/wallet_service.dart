import 'package:dio/dio.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';
import '../models/wallet.dart';

/// Parametros para vincular una cuenta propia (docs/05 §6.5, E4-T01).
///
/// - `type=ACCOUNT` exige `account_id` UUID y prohibe `card`.
/// - El `accountId` sale del selector de cuentas del titular
///   (`GET /accounts`); el cliente no teclea ni valida numeros
///   (decision 2 del brief: elegir de la lista).
class AccountLinkInput {
  const AccountLinkInput({required this.accountId});

  final String accountId;

  Map<String, dynamic> toJson() => {
        'type': 'ACCOUNT',
        'account_id': accountId,
      };
}

/// Parametros para vincular una tarjeta (docs/05 §6.5, E4-T01).
///
/// - `type=CARD` exige `card` y prohibe `account_id`.
/// - `card` lleva el PAN crudo (`pan`): el backend lo tokeniza
///   server-side y lo descarta. El PAN solo vive en este objeto
///   efimero en memoria: jamas se guarda en disco ni se escribe en
///   logs (regla de oro 7); la respuesta solo trae `last4`/`brand`.
class CardLinkInput {
  const CardLinkInput({required this.pan});

  /// PAN en claro (solo para el cuerpo del request; efimero).
  final String pan;

  Map<String, dynamic> toJson() => {
        'type': 'CARD',
        'card': {'pan': pan},
      };
}

/// Contrato del servicio de billetera (seam para tests con fake).
abstract class WalletServiceBase {
  /// Billetera del titular (`GET /wallet`; vacia `200` si no existe).
  Future<Wallet> getWallet();

  /// Vincula una cuenta (`POST /wallet/links` + `Idempotency-Key`).
  ///
  /// [idempotencyKey] permite reutilizar la misma clave en el reintento
  /// del mismo intento (CA: sin duplicar el vinculo).
  Future<WalletLink> linkAccount(
    AccountLinkInput input, {
    String? idempotencyKey,
  });

  /// Vincula una tarjeta con el PAN crudo (`POST /wallet/links` +
  /// `Idempotency-Key`). El PAN viaja solo en el request; la respuesta
  /// y la lista solo exponen `last4`/`brand`.
  Future<WalletLink> linkCard(
    CardLinkInput input, {
    String? idempotencyKey,
  });

  /// Desvincula una fuente (`DELETE /wallet/links/{id}`; baja logica a
  /// `INACTIVE`, solo titular). Responde el vinculo con `status: INACTIVE`;
  /// ajeno/inexistente/ya `INACTIVE` -> `404 NOT_FOUND` neutro.
  Future<WalletLink> unlink(String id);
}

/// Servicio de billetera sobre [ApiClient] (docs/05 §6.5, E4-T04).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente):
/// - `GET /wallet` -> `200 {"data": {wallet_id, status, links[]}}`
/// - `POST /wallet/links` (+ `Idempotency-Key` opcional) ->
///   `201 {"data": {vinculo enmascarado}}`
/// - `DELETE /wallet/links/{id}` (sin `Idempotency-Key`: no mueve dinero,
///   solo baja logica) -> `200 {"data": {vinculo INACTIVE}}`
class WalletService implements WalletServiceBase {
  WalletService({required this.api});

  final ApiClient api;

  static Wallet _parseWallet(Object? data) {
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return Wallet.fromJson(Map<String, dynamic>.from(data['data'] as Map));
    }
    if (data is Map<String, dynamic>) return Wallet.fromJson(data);
    throw const FormatException('Respuesta de billetera invalida');
  }

  static WalletLink _parseLink(Object? data) {
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return WalletLink.fromJson(
        Map<String, dynamic>.from(data['data'] as Map),
      );
    }
    if (data is Map<String, dynamic>) return WalletLink.fromJson(data);
    throw const FormatException('Respuesta de vinculo invalida');
  }

  @override
  Future<Wallet> getWallet() async {
    final resp = await api.get('/wallet');
    return _parseWallet(resp.data);
  }

  @override
  Future<WalletLink> linkAccount(
    AccountLinkInput input, {
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/wallet/links',
      data: input.toJson(),
      idempotencyKey: idempotencyKey,
    );
    return _parseLink(resp.data);
  }

  @override
  Future<WalletLink> linkCard(
    CardLinkInput input, {
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/wallet/links',
      data: input.toJson(),
      idempotencyKey: idempotencyKey,
    );
    return _parseLink(resp.data);
  }

  @override
  Future<WalletLink> unlink(String id) async {
    try {
      final resp = await api.dio.delete('/wallet/links/$id');
      return _parseLink(resp.data);
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
  }
}

/// Fabrica inyectable del servicio (mismo patron que
/// `accountsServiceFactory`).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con
/// el `ApiClient` compartido; `null` = DI no configurada: las pantallas
/// muestran un error accionable en vez de romper.
typedef WalletServiceFactory = WalletServiceBase Function();

WalletServiceFactory? walletServiceFactory;
