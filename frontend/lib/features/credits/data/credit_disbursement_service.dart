// Servicio del desembolso del crédito (M-1, HU12).
//
// Cierra el hallazgo M-1 de la validación de HU12: el backend ya exponía
// `POST /credits/applications/{id}/disburse` (E3-T17) pero ninguna pantalla
// lo invocaba, así que el ciclo "firmar → desembolsar → pagar" no era
// alcanzable en la app. Este servicio es el disparador que consume la
// CTA "Desembolsar a mi cuenta" (`credit_disburse_section.dart`).
//
// Endpoint (base `/api/v1` ya incluida en el cliente, `docs/05` §6.4,
// detalle E3-T17):
// - `POST /credits/applications/{id}/disburse` (cuerpo `{account_id?}`
//   UUID; omitido = cuenta principal del titular, la `ACTIVE` más antigua;
//   cabecera `Idempotency-Key` obligatoria: MUEVE dinero) ->
//   `201 {"data": {loan_id, application_id, transaction_id, status
//   ("SETTLED"), disbursed_at, account_masked ("****dddd"), amount_minor,
//   currency}, "meta": {"request_id", "replayed"}}` con el préstamo creado
//   y acreditado, o `200` con la misma forma si es replay (misma clave +
//   mismo cuerpo, o reintento con otra clave sobre un expediente ya
//   desembolsado: un préstamo, un asiento, un evento).
// - Códigos: `404 NOT_FOUND` (expediente/cuenta ajeno o inexistente,
//   neutro), `409 CONFLICT` (expediente no `APPROVED`, juego no `SIGNED`) /
//   `409 IDEMPOTENCY_CONFLICT` (misma clave con cuerpo distinto),
//   `422 VALIDATION_ERROR` (sin `Idempotency-Key`, cuenta no `ACTIVE`,
//   moneda incoherente o cuerpo inválido).
//
// Cliente delgado (`docs/19` §4): transporta y presenta; no calcula montos.
// Sin PII ni claves en logs: la cuenta solo se muestra enmascarada
// (`account_masked` del servidor).
library;

import '../../../core/errors/api_exception.dart';
import '../../../core/http/api_client.dart';

/// Resultado del desembolso tal como lo devuelve el servidor.
///
/// Todos los valores se muestran TAL CUAL (cliente delgado): el monto
/// (`amountMinor`), la cuenta enmascarada (`accountMasked`, p. ej.
/// `****4521`) y las fechas vienen del backend; el dispositivo no calcula
/// ni reinterpreta nada.
class DisbursementResult {
  const DisbursementResult({
    required this.loanId,
    required this.status,
    this.applicationId,
    this.transactionId,
    this.disbursedAt,
    this.accountMasked,
    this.amountMinor,
    this.currency,
    this.replayed = false,
  });

  final String loanId;

  /// `SETTLED` cuando el dinero quedó acreditado.
  final String status;
  final String? applicationId;

  /// `null` si el servidor no la informa.
  final String? transactionId;
  final String? disbursedAt;

  /// Cuenta YA enmascarada por el backend. Se muestra tal cual.
  final String? accountMasked;
  final int? amountMinor;
  final String? currency;
  final bool replayed;

  /// `true` solo si el servidor acreditó (`SETTLED`).
  bool get isSettled => status.toUpperCase() == 'SETTLED';

  static int? _minorOf(Object? value) {
    if (value == null) return null;
    if (value is int) return value;
    if (value is num) return value.toInt();
    return int.tryParse('$value');
  }

  factory DisbursementResult.fromJson(
    Map<String, dynamic> json, {
    bool replayed = false,
  }) {
    return DisbursementResult(
      loanId: '${json['loan_id']}',
      status: '${json['status'] ?? ''}',
      applicationId: json['application_id']?.toString(),
      transactionId: json['transaction_id']?.toString(),
      disbursedAt: json['disbursed_at']?.toString(),
      accountMasked: json['account_masked']?.toString(),
      amountMinor: _minorOf(json['amount_minor']),
      currency: json['currency']?.toString(),
      replayed: replayed,
    );
  }
}

/// Contrato del servicio de desembolso (seam para tests con fake).
abstract class CreditDisbursementServiceBase {
  /// Desembolsa el expediente firmado a la cuenta indicada.
  ///
  /// [accountId] es el UUID lógico de la cuenta del titular; `null`/vacío
  /// = cuenta principal (el servidor la resuelve: la `ACTIVE` más antigua).
  /// [idempotencyKey] reutiliza la clave del intento en curso ante
  /// reintentos; si es `null`, el cliente genera una nueva.
  Future<DisbursementResult> disburseApplication({
    required String applicationId,
    String? accountId,
    String? idempotencyKey,
  });
}

/// Servicio HTTP de desembolso sobre [ApiClient] (M-1, `docs/05` §6.4).
class CreditDisbursementService implements CreditDisbursementServiceBase {
  CreditDisbursementService({required this.api});

  final ApiClient api;

  static Map<String, dynamic> _dataEnvelope(Object? body) {
    if (body is Map<String, dynamic> && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    if (body is Map<String, dynamic>) return body;
    throw const FormatException('Respuesta del desembolso inválida');
  }

  static bool _replayed(Object? body) {
    if (body is Map<String, dynamic> && body['meta'] is Map) {
      return (body['meta'] as Map)['replayed'] == true;
    }
    return false;
  }

  @override
  Future<DisbursementResult> disburseApplication({
    required String applicationId,
    String? accountId,
    String? idempotencyKey,
  }) async {
    final cleanAccount = (accountId ?? '').trim();
    final resp = await api.postWithIdempotency(
      '/credits/applications/$applicationId/disburse',
      // Sin cuenta elegida se omite `account_id`: el servidor desembolsa a
      // la cuenta principal (no se inventa un default en el cliente).
      data: cleanAccount.isEmpty ? const {} : {'account_id': cleanAccount},
      idempotencyKey: idempotencyKey,
    );
    return DisbursementResult.fromJson(
      _dataEnvelope(resp.data),
      replayed: _replayed(resp.data),
    );
  }
}

/// Fábrica inyectable del servicio (mismo patrón que
/// `creditApplicationsServiceFactory`).
///
/// La asigna el orquestador con el `ApiClient` compartido; `null` = DI no
/// configurada (la sección de desembolso degrada a aviso honesto en vez de
/// romper).
typedef CreditDisbursementServiceFactory
    = CreditDisbursementServiceBase Function();

CreditDisbursementServiceFactory? creditDisbursementServiceFactory;

/// Mensaje UI en español para un [ApiException] del desembolso (M-1).
///
/// Cubre los errores del brief (`docs/05` §6.4, detalle E3-T17): `CONFLICT`
/// (expediente no `APPROVED` o juego no `SIGNED`), `NOT_FOUND` (neutro),
/// `VALIDATION_ERROR` (cuenta no disponible) y errores de red. Sin PII:
/// nunca incluye el cuerpo del servidor.
String creditDisbursementUserMessage(ApiException error) {
  switch (error.code) {
    case 'NOT_AUTHENTICATED':
      return 'Tu sesión venció. Vuelve a iniciar sesión para continuar.';
    case 'NOT_FOUND':
      return 'No encontramos el expediente o la cuenta. '
          'Vuelve e inicia el flujo de nuevo.';
    case 'IDEMPOTENCY_CONFLICT':
    case 'DUPLICATE_REQUEST':
      return 'Esta solicitud ya fue registrada con otros datos. '
          'Revisa el estado del expediente.';
    case 'CONFLICT':
      return 'Aún no puedes desembolsar: el crédito debe estar aprobado y '
          'firmado. Si ya se desembolsó, verás el préstamo en tu lista.';
    case 'VALIDATION_ERROR':
      return 'No pudimos procesar el desembolso. '
          'Verifica la cuenta elegida e inténtalo de nuevo.';
    default:
      return error.message;
  }
}
