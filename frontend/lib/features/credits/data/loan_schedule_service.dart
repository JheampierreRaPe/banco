// Servicio de préstamos y cronograma definitivo (E3-T21, HU12).
//
// Endpoints (base `/api/v1` ya incluida en el cliente, `docs/05` §6.4,
// detalle E3-T17/E3-T19/E3-T20):
// - `GET /credits/loans` -> préstamos del titular (`data: []` si no tiene).
// - `GET /credits/loans/{id}/schedule` -> `{loan, schedule[]}` definitivo.
// - `PUT /credits/loans/{id}/auto-debit` (cuerpo `{enabled: bool}` estricto;
//   SIN `Idempotency-Key`: no mueve dinero) -> `{loan_id,
//   auto_debit_enabled}`.
// - `POST /credits/loans/{id}/pay` (cuerpo `{schedule_id?}`; omitido = la
//   próxima cuota exigible; con `Idempotency-Key` obligatoria: MUEVE dinero)
//   -> `{loan_id, schedule_id, transaction_id, status (SETTLED|REJECTED),
//   amount_minor, outstanding_principal_minor, paid_at, failure_code?}`.
//
// Cliente delgado (`docs/19` §4): transporta y presenta; no calcula cuotas,
// intereses, saldos ni mora. Sin PII ni claves en logs.
library;

import 'package:dio/dio.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';
import '../models/loan_schedule.dart';

/// Resultado del pago de una cuota (tal cual responde el servidor).
class PayInstallmentResult {
  const PayInstallmentResult({
    required this.loanId,
    required this.scheduleId,
    required this.status,
    required this.amountMinor,
    required this.outstandingPrincipalMinor,
    this.transactionId,
    this.paidAt,
    this.failureCode,
    this.replayed = false,
  });

  final String loanId;
  final String scheduleId;

  /// `null` si el servidor no la informa (p. ej. rechazo sin débito).
  final String? transactionId;

  /// `SETTLED` (cobrada) o `REJECTED` (sin fondos, con `failure_code`).
  final String status;
  final int amountMinor;
  final int outstandingPrincipalMinor;
  final String? paidAt;
  final String? failureCode;
  final bool replayed;

  /// `true` solo si el servidor cobró (`SETTLED`).
  bool get isSettled => status.toUpperCase() == 'SETTLED';

  /// `true` si el rechazo fue por falta de fondos (mensaje accionable, no
  /// error de sistema; la cuota queda intacta).
  bool get isInsufficientFunds =>
      status.toUpperCase() == 'REJECTED' &&
      (failureCode ?? '').toUpperCase() == 'INSUFFICIENT_FUNDS';

  factory PayInstallmentResult.fromJson(
    Map<String, dynamic> json, {
    bool replayed = false,
  }) {
    int minorOf(Object? value) {
      if (value is int) return value;
      if (value is num) return value.toInt();
      return int.tryParse('${value ?? 0}') ?? 0;
    }

    return PayInstallmentResult(
      loanId: '${json['loan_id']}',
      scheduleId: '${json['schedule_id']}',
      transactionId: json['transaction_id']?.toString(),
      status: '${json['status'] ?? ''}',
      amountMinor: minorOf(json['amount_minor']),
      outstandingPrincipalMinor:
          minorOf(json['outstanding_principal_minor']),
      paidAt: json['paid_at']?.toString(),
      failureCode: json['failure_code']?.toString(),
      replayed: replayed,
    );
  }
}

/// Contrato del servicio de préstamos/cronograma (seam para tests con fake).
abstract class LoanScheduleServiceBase {
  /// Préstamos del titular (orden del servidor; vacía si no tiene).
  Future<List<Loan>> listLoans();

  /// Cronograma definitivo de un préstamo (préstamo + cuotas).
  Future<LoanSchedule> getSchedule(String loanId);

  /// Activa/desactiva el débito automático (refleja el servidor).
  ///
  /// Sin `Idempotency-Key` (no mueve dinero; el cobro lo hace el job con el
  /// caso de uso de `pay`).
  Future<bool> setAutoDebit(String loanId, {required bool enabled});

  /// Paga la próxima cuota exigible (omite `schedule_id`: el servidor cobra
  /// el `total_minor` de la cuota; MVP por cuota total).
  ///
  /// [idempotencyKey] reutiliza la clave del intento en curso ante
  /// reintentos; si es `null`, el cliente genera una nueva.
  Future<PayInstallmentResult> payNextInstallment(
    String loanId, {
    String? idempotencyKey,
  });
}

/// Servicio HTTP de préstamos sobre [ApiClient] (E3-T21, `docs/05` §6.4).
class LoanScheduleService implements LoanScheduleServiceBase {
  LoanScheduleService({required this.api});

  final ApiClient api;

  static Map<String, dynamic> _dataEnvelope(Object? body) {
    if (body is Map<String, dynamic> && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    if (body is Map<String, dynamic>) return body;
    throw const FormatException('Respuesta de préstamos inválida');
  }

  static bool _replayed(Object? body) {
    if (body is Map<String, dynamic> && body['meta'] is Map) {
      return (body['meta'] as Map)['replayed'] == true;
    }
    return false;
  }

  @override
  Future<List<Loan>> listLoans() async {
    final resp = await api.get('/credits/loans');
    final body = resp.data;
    List raw;
    if (body is Map<String, dynamic> && body['data'] is List) {
      raw = body['data'] as List;
    } else if (body is List) {
      raw = body;
    } else {
      return const [];
    }
    return raw
        .whereType<Map>()
        .map((e) => Loan.fromJson(Map<String, dynamic>.from(e)))
        .toList();
  }

  @override
  Future<LoanSchedule> getSchedule(String loanId) async {
    final resp = await api.get('/credits/loans/$loanId/schedule');
    return LoanSchedule.fromJson(_dataEnvelope(resp.data));
  }

  @override
  Future<bool> setAutoDebit(String loanId, {required bool enabled}) async {
    try {
      final resp = await api.dio.put(
        '/credits/loans/$loanId/auto-debit',
        data: {'enabled': enabled},
      );
      final data = _dataEnvelope(resp.data);
      final value = data['auto_debit_enabled'];
      if (value is bool) return value;
      return enabled;
    } on DioException catch (e) {
      throw ApiException.fromDioException(e);
    }
  }

  @override
  Future<PayInstallmentResult> payNextInstallment(
    String loanId, {
    String? idempotencyKey,
  }) async {
    final resp = await api.postWithIdempotency(
      '/credits/loans/$loanId/pay',
      data: const {},
      idempotencyKey: idempotencyKey,
    );
    return PayInstallmentResult.fromJson(
      _dataEnvelope(resp.data),
      replayed: _replayed(resp.data),
    );
  }
}

/// Fábrica inyectable del servicio (mismo patrón que
/// `creditApplicationsServiceFactory`).
///
/// La asigna el orquestador con el `ApiClient` compartido; `null` = DI no
/// configurada (las pantallas degradan a error accionable).
typedef LoanScheduleServiceFactory = LoanScheduleServiceBase Function();

LoanScheduleServiceFactory? loanScheduleServiceFactory;

/// Mensaje UI en español para un [ApiException] de préstamos (E3-T21).
///
/// Cubre los errores del brief (`docs/05` §6.4): `INSUFFICIENT_FUNDS`
/// (mensaje accionable, no error de sistema), `CONFLICT` (cuota ya pagada /
/// préstamo no activo), `NOT_FOUND` (neutro), `VALIDATION_ERROR` y errores
/// de red (`Reintentar`). Sin PII: nunca incluye el cuerpo del servidor.
String loanScheduleUserMessage(ApiException error) {
  switch (error.code) {
    case 'INSUFFICIENT_FUNDS':
      return 'Saldo insuficiente para pagar la cuota. '
          'Recarga tu cuenta e inténtalo de nuevo.';
    case 'CONFLICT':
    case 'IDEMPOTENCY_CONFLICT':
    case 'DUPLICATE_REQUEST':
      return 'Esta cuota ya fue pagada o el préstamo no está activo. '
          'Actualiza el cronograma.';
    case 'NOT_FOUND':
      return 'No encontramos el préstamo. '
          'Vuelve e inicia el flujo de nuevo.';
    case 'VALIDATION_ERROR':
      return 'No pudimos procesar la solicitud. Inténtalo de nuevo.';
    case 'NOT_AUTHENTICATED':
      return 'Tu sesión venció. Vuelve a iniciar sesión para continuar.';
    default:
      return error.message;
  }
}
