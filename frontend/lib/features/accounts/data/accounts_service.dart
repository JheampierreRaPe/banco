import 'dart:typed_data';

import 'package:dio/dio.dart';

import '../../../core/http/api_client.dart';
import '../models/account.dart';

/// Filtros de la lista de movimientos (HU05 CA-03).
class MovementFilters {
  const MovementFilters({this.dateFrom, this.dateTo, this.direction});

  final DateTime? dateFrom;
  final DateTime? dateTo;

  /// `CREDIT` | `DEBIT` | `null` (todas).
  final String? direction;

  /// Query params docs/05 (`snake_case`, fechas ISO `yyyy-MM-dd`).
  Map<String, dynamic> toQuery({required int page, required int pageSize}) {
    String iso(DateTime d) =>
        '${d.year.toString().padLeft(4, '0')}-'
        '${d.month.toString().padLeft(2, '0')}-'
        '${d.day.toString().padLeft(2, '0')}';
    return {
      'page': page,
      'page_size': pageSize,
      if (dateFrom != null) 'date_from': iso(dateFrom!),
      if (dateTo != null) 'date_to': iso(dateTo!),
      if (direction != null && direction!.isNotEmpty) 'direction': direction,
    };
  }
}

/// Contrato del servicio de cuentas (seam para tests de widgets con mock).
abstract class AccountsServiceBase {
  Future<List<Account>> getAccounts();
  Future<Account> getAccountDetail(String accountId);
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  });
  Future<ExportResult> exportMovements(
    String accountId, {
    required String format,
    required DateTime dateFrom,
    required DateTime dateTo,
  });
}

/// Servicio de cuentas sobre [ApiClient] (docs/05 §6.2).
///
/// Endpoints (base `/api/v1` ya incluida en el cliente):
/// - `GET /accounts`
/// - `GET /accounts/{id}`
/// - `GET /accounts/{id}/movements?page&page_size&date_from&date_to&direction`
/// - `GET /accounts/{id}/movements/export?format&date_from&date_to`
///
/// Solo GET: no mueve dinero, no requiere `Idempotency-Key`.
class AccountsService implements AccountsServiceBase {
  AccountsService({required this.api});

  final ApiClient api;

  /// Fabrica corta para pantallas/tests que ya tienen sesion.
  static AccountsService create({required ApiClient api}) =>
      AccountsService(api: api);

  static List<Account> _parseAccountList(Object? data) {
    if (data is Map<String, dynamic>) {
      final raw = data['data'];
      if (raw is List) {
        return raw
            .whereType<Map>()
            .map((e) => Account.fromJson(Map<String, dynamic>.from(e)))
            .toList();
      }
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
  Future<List<Account>> getAccounts() async {
    final resp = await api.get('/accounts');
    return _parseAccountList(resp.data);
  }

  @override
  Future<Account> getAccountDetail(String accountId) async {
    final resp = await api.get('/accounts/$accountId');
    final data = resp.data;
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return Account.fromJson(Map<String, dynamic>.from(data['data'] as Map));
    }
    if (data is Map<String, dynamic>) {
      return Account.fromJson(data);
    }
    throw const FormatException('Respuesta de detalle de cuenta invalida');
  }

  @override
  Future<MovementsPage> getMovements(
    String accountId, {
    int page = 1,
    int pageSize = 20,
    MovementFilters filters = const MovementFilters(),
  }) async {
    final resp = await api.get(
      '/accounts/$accountId/movements',
      queryParameters: filters.toQuery(page: page, pageSize: pageSize),
    );
    final data = resp.data;
    if (data is Map<String, dynamic>) return MovementsPage.fromJson(data);
    return MovementsPage(items: const [], page: page, pageSize: pageSize, total: 0);
  }

  @override
  Future<ExportResult> exportMovements(
    String accountId, {
    required String format,
    required DateTime dateFrom,
    required DateTime dateTo,
  }) async {
    String iso(DateTime d) =>
        '${d.year.toString().padLeft(4, '0')}-'
        '${d.month.toString().padLeft(2, '0')}-'
        '${d.day.toString().padLeft(2, '0')}';
    final resp = await api.get(
      '/accounts/$accountId/movements/export',
      queryParameters: {
        'format': format,
        'date_from': iso(dateFrom),
        'date_to': iso(dateTo),
      },
      options: Options(responseType: ResponseType.bytes),
    );
    final bytes = resp.data is List<int>
        ? Uint8List.fromList((resp.data as List<int>))
        : Uint8List(0);
    final contentType =
        resp.headers.map['content-type']?.firstOrNull ?? 'text/csv';
    final disposition =
        resp.headers.map['content-disposition']?.firstOrNull ?? '';
    final filename =
        _filenameFromDisposition(disposition) ??
        'movimientos_${accountId}_${iso(dateFrom)}_${iso(dateTo)}.$format';
    return ExportResult(
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

/// DECISION DE GUARDADO DE ARCHIVOS EXPORTADOS (E2-T05):
///
/// `exportMovements` retorna los bytes + nombre sugerido y NO guarda nada en
/// disco. El guardado nativo (descargas en Android/iOS, `file_saver` o
/// equivalente) queda como PENDIENTE F-futuro: no se agrega ninguna
/// dependencia nueva (`pubspec.yaml` intacto, docs/16: reglas configurables y
/// sin sorpresas). La UI muestra un dialogo con nombre/tamano y mensaje de
/// exito simulado que indica este pendiente.
