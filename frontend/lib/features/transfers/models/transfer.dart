// Modelos del feature `transfers` (E2-T11 / HU06).
//
// Shapes del backend (`docs/05` §6.3):
/// - `POST /transfers/own` y `POST /transfers/third-party` (`201`) ->
///   `{data: {transaction_id, type, status, source_account_id,
///   target_account_id, amount_minor, currency, fee_minor, failure_code},
///   meta: {request_id, replayed}}`.
/// - `POST /transfers/interbank` (`202`) -> `{data: {transaction_id, type,
///   status, source_account_id, amount_minor, currency, fee_minor,
///   external_ref, tracking, failure_code}, meta: {request_id, replayed}}`.
/// - `GET /transactions/{id}` (`200`) -> detalle + `tracking` (solo
///   interbancaria) + `history`.
/// - `GET /transactions/{id}/receipt` (`200`) -> PDF con
///   `Content-Disposition: attachment; filename="comprobante-<OP-...>.pdf"`.
///
/// Reglas de oro (docs/16 §2.9, docs/13 §1.2, docs/19 §4):
/// - Dinero entero en centimos (`int`); el cliente NUNCA calcula saldos ni
///   comisiones: el total (`amount + fee`) se presenta tal como lo devuelve
///   el backend ([TransferResult.totalMinor] solo suma para exhibir los dos
///   valores ya devueltos por el servidor, sin decidir nada).
/// - Datos enmascarados: el CCI/titular en claro solo viaja en el cuerpo del
///   request; la UI solo muestra mascaras (`••••` + ultimos 4).
/// - Sin PII ni secretos en logs.
library;

import 'package:uuid/uuid.dart';

import '../../accounts/models/account.dart';

/// Tipo de transferencia del flujo.
enum TransferKind {
  /// Entre cuentas propias (`POST /transfers/own`, sin comision).
  own,

  /// A tercero del mismo banco (`POST /transfers/third-party`).
  thirdParty,

  /// A otro banco por CCE (`POST /transfers/interbank`, `202` + polling).
  interbank;

  /// Etiqueta corta para la UI (mockup `Transferencias`).
  String get label {
    switch (this) {
      case TransferKind.own:
        return 'Entre mis cuentas';
      case TransferKind.thirdParty:
        return 'A un tercero';
      case TransferKind.interbank:
        return 'Interbancaria CCI';
    }
  }

  /// Resuelve `?kind=` de la ruta (default: propia).
  static TransferKind fromQuery(String? value) {
    switch (value) {
      case 'third':
        return TransferKind.thirdParty;
      case 'interbank':
        return TransferKind.interbank;
      case 'own':
      default:
        return TransferKind.own;
    }
  }
}

/// Borrador del intento de transferencia en curso (memoria, sin disco).
///
/// El [idempotencyKey] se genera UNA vez al crear el borrador y se reutiliza
/// en cada reintento del mismo intento (regla 4 de `features/README.md`).
class TransferDraft {
  TransferDraft({
    required this.kind,
    required this.sourceAccountId,
    required this.amountMinor,
    this.targetAccountId,
    this.beneficiaryId,
    this.beneficiaryAlias = '',
    this.destinationMasked = '',
    this.cci = '',
    this.bankCode = '',
    this.holderName = '',
    this.currency = 'PEN',
    this.motive = '',
    String? idempotencyKey,
  }) : idempotencyKey = idempotencyKey ?? const Uuid().v4();

  final TransferKind kind;

  /// UUID de la cuenta origen (propia, de `GET /accounts`).
  final String sourceAccountId;

  /// UUID de la cuenta destino (propia o de tercero; `null` en interbancaria).
  final String? targetAccountId;

  /// Beneficiario de la agenda (tercero; display + `metadata`, E2-T14).
  final String? beneficiaryId;
  final String beneficiaryAlias;
  final String destinationMasked;

  /// CCI destino normalizado (20 digitos, sin guiones/espacios).
  final String cci;
  final String bankCode;
  final String holderName;

  final String currency;

  /// Monto en centimos (> 0, validado en UI y en el backend).
  final int amountMinor;

  /// Motivo libre (viaja en `metadata`, nunca en `external_ref` para no
  /// chocar con la unicidad de la referencia externa).
  final String motive;

  /// Clave del intento: el reintento reutiliza SIEMPRE la misma.
  final String idempotencyKey;

  TransferDraft copyWith({
    TransferKind? kind,
    String? sourceAccountId,
    String? targetAccountId,
    String? beneficiaryId,
    String? beneficiaryAlias,
    String? destinationMasked,
    String? cci,
    String? bankCode,
    String? holderName,
    String? currency,
    int? amountMinor,
    String? motive,
    String? idempotencyKey,
  }) {
    return TransferDraft(
      kind: kind ?? this.kind,
      sourceAccountId: sourceAccountId ?? this.sourceAccountId,
      amountMinor: amountMinor ?? this.amountMinor,
      targetAccountId: targetAccountId ?? this.targetAccountId,
      beneficiaryId: beneficiaryId ?? this.beneficiaryId,
      beneficiaryAlias: beneficiaryAlias ?? this.beneficiaryAlias,
      destinationMasked: destinationMasked ?? this.destinationMasked,
      cci: cci ?? this.cci,
      bankCode: bankCode ?? this.bankCode,
      holderName: holderName ?? this.holderName,
      currency: currency ?? this.currency,
      motive: motive ?? this.motive,
      idempotencyKey: idempotencyKey ?? this.idempotencyKey,
    );
  }
}

/// Resultado de un POST de transferencia (propia, tercero o interbancaria).
class TransferResult {
  const TransferResult({
    required this.transactionId,
    required this.type,
    required this.status,
    required this.amountMinor,
    required this.currency,
    this.sourceAccountId,
    this.targetAccountId,
    this.feeMinor = 0,
    this.externalRef,
    this.tracking = const {},
    this.failureCode,
    this.replayed = false,
  });

  final String transactionId;
  final String type;
  final String status;
  final int amountMinor;
  final String currency;
  final String? sourceAccountId;
  final String? targetAccountId;
  final int feeMinor;
  final String? externalRef;
  final Map<String, dynamic> tracking;
  final String? failureCode;
  final bool replayed;

  /// Total a debitar para EXHIBIR (`amount + fee` tal como los devuelve el
  /// backend; presentacion, el cliente no decide reglas con este valor).
  int get totalMinor => amountMinor + feeMinor;

  /// `true` si el backend rechazo sin mover dinero (p. ej. fondos).
  bool get isRejected => status.toUpperCase() == 'REJECTED';

  bool get isInsufficientFunds =>
      isRejected && failureCode == 'INSUFFICIENT_FUNDS';

  /// Titular enmascarado del seguimiento interbancario (`tracking`
  /// de `POST /transfers/interbank`), o vacio.
  String get holderMasked =>
      tracking['holder_name_masked']?.toString() ?? '';

  factory TransferResult.fromJson(
    Map<String, dynamic> json, {
    bool replayed = false,
  }) {
    int minorOf(Object? value) {
      if (value is int) return value;
      if (value is num) return value.toInt();
      return int.tryParse(value?.toString() ?? '') ?? 0;
    }

    Map<String, dynamic> trackingOf(Object? value) {
      if (value is Map) return Map<String, dynamic>.from(value);
      return const {};
    }

    return TransferResult(
      transactionId: json['transaction_id']?.toString() ?? '',
      type: json['type']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      amountMinor: minorOf(json['amount_minor']),
      currency: json['currency']?.toString() ?? 'PEN',
      sourceAccountId: json['source_account_id']?.toString(),
      targetAccountId: json['target_account_id']?.toString(),
      feeMinor: minorOf(json['fee_minor']),
      externalRef: json['external_ref']?.toString(),
      tracking: trackingOf(json['tracking']),
      failureCode: json['failure_code']?.toString(),
      replayed: replayed,
    );
  }
}

/// Detalle y seguimiento (`GET /transactions/{id}`).
class TransactionDetail {
  const TransactionDetail({
    required this.transactionId,
    required this.type,
    required this.status,
    required this.amountMinor,
    required this.currency,
    this.feeMinor = 0,
    this.externalRef,
    this.tracking = const {},
    this.failureCode,
    this.historyLength = 0,
  });

  final String transactionId;
  final String type;
  final String status;
  final int amountMinor;
  final String currency;
  final int feeMinor;
  final String? externalRef;
  final Map<String, dynamic> tracking;
  final String? failureCode;
  final int historyLength;

  /// Total a debitar para EXHIBIR (ver [TransferResult.totalMinor]).
  int get totalMinor => amountMinor + feeMinor;

  bool get isTerminal {
    switch (status.toUpperCase()) {
      case 'SETTLED':
      case 'POSTED':
      case 'CONCILIATED':
      case 'REJECTED':
      case 'FAILED':
      case 'REVERSED':
        return true;
      default:
        return false;
    }
  }

  bool get isSuccess {
    switch (status.toUpperCase()) {
      case 'SETTLED':
      case 'POSTED':
      case 'CONCILIATED':
        return true;
      default:
        return false;
    }
  }

  bool get isInsufficientFunds =>
      failureCode == 'INSUFFICIENT_FUNDS';

  String get holderMasked =>
      tracking['holder_name_masked']?.toString() ?? '';

  factory TransactionDetail.fromJson(Map<String, dynamic> json) {
    int minorOf(Object? value) {
      if (value is int) return value;
      if (value is num) return value.toInt();
      return int.tryParse(value?.toString() ?? '') ?? 0;
    }

    final history = json['history'];
    return TransactionDetail(
      transactionId: json['transaction_id']?.toString() ?? '',
      type: json['type']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      amountMinor: minorOf(json['amount_minor']),
      currency: json['currency']?.toString() ?? 'PEN',
      feeMinor: minorOf(json['fee_minor']),
      externalRef: json['external_ref']?.toString(),
      tracking: json['tracking'] is Map
          ? Map<String, dynamic>.from(json['tracking'] as Map)
          : const {},
      failureCode: json['failure_code']?.toString(),
      historyLength: history is List ? history.length : 0,
    );
  }
}

/// Comprobante PDF (`GET /transactions/{id}/receipt`).
///
/// Solo bytes + nombre sugerido: el guardado nativo queda pendiente
/// (F-futuro, mismo patron que `exportMovements` de E2-T05; sin dependencias
/// nuevas).
class ReceiptResult {
  const ReceiptResult({
    required this.bytes,
    required this.suggestedFilename,
    required this.contentType,
  });

  final List<int> bytes;
  final String suggestedFilename;
  final String contentType;

  /// Numero de operacion derivado del nombre (`OP-` + 12 hex, E2-T10).
  String get operationNumber {
    final match =
        RegExp(r'OP-[0-9A-Fa-f]{12}').firstMatch(suggestedFilename);
    return match?.group(0)?.toUpperCase() ?? '';
  }
}

/// Estado de la verificacion del CCI en la UI (mockups `CCI 2/3/4` +
/// `Formulario Interbancario CCI`).
///
/// La verificacion real del titular la ejecuta el backend al confirmar
/// (`POST /transfers/interbank` valida contra la CCE antes de mover dinero,
/// E2-T08/E2-T09): estos estados modelan el resultado de ese intento sin
/// exponer el CCI en claro.
enum CciState {
  /// Formulario inicial (sin banner o con ayuda neutra).
  idle,

  /// Intento en curso (`Consultando CCE...`, mockup `CCI 2`).
  verifying,

  /// Titular validado (banner verde + nombre enmascarado del `tracking`).
  verified,

  /// `404 HOLDER_NOT_FOUND` neutro (mockup `CCI 3`, sin eco del CCI).
  notFound,

  /// `503 HOLDER_LOOKUP_UNAVAILABLE` / timeout (mockup `CCI 4` + reintento).
  unavailable,
}

/// Normaliza un CCI (`str`, sin espacios ni guiones intermedios).
String normalizeCci(String value) =>
    value.trim().replaceAll(RegExp(r'[\s\-]+'), '');

/// `true` si el CCI tiene formato valido (exacto 20 digitos, docs/05 §6.3).
bool isValidCciFormat(String value) =>
    RegExp(r'^[0-9]{20}$').hasMatch(normalizeCci(value));

/// Mascara corta para exhibir (`•••• 4458`, ultimos 4; nunca el completo).
String maskCciShort(String cci) {
  final digits = normalizeCci(cci);
  if (digits.length < 4) return '••••';
  return '•••• ${digits.substring(digits.length - 4)}';
}

/// Formatea centimos a moneda (`485020/PEN` -> `S/ 4,850.20`).
///
/// Presentacion de valores que YA vienen del servidor; no decide reglas.
String formatMoneyMinor(int minor, String currency) {
  final symbol = currency.toUpperCase() == 'USD' ? r'$' : 'S/';
  final negative = minor < 0;
  final abs = minor.abs();
  final units = abs ~/ 100;
  final cents = (abs % 100).toString().padLeft(2, '0');
  final grouped = _thousands(units);
  return '$symbol ${negative ? '-' : ''}$grouped.$cents';
}

String _thousands(int value) {
  final digits = value.toString();
  final buffer = StringBuffer();
  var count = 0;
  for (var i = digits.length - 1; i >= 0; i--) {
    buffer.write(digits[i]);
    count++;
    if (count == 3 && i != 0) {
      buffer.write(',');
      count = 0;
    }
  }
  return buffer.toString().split('').reversed.join();
}

/// Parsea `350.00` / `350` a centimos (`35000`); `null` si invalido.
int? parseAmountToMinor(String value) {
  final cleaned = value.trim().replaceAll(' ', '').replaceAll(',', '');
  if (cleaned.isEmpty) return null;
  final parts = cleaned.split('.');
  if (parts.length > 2) return null;
  final units = int.tryParse(parts[0]);
  if (units == null || units < 0) return null;
  var cents = 0;
  if (parts.length == 2) {
    final frac = parts[1];
    if (frac.length > 2 || int.tryParse(frac) == null) return null;
    cents = int.parse(frac.padRight(2, '0'));
  }
  return units * 100 + cents;
}

/// Tope UX del campo monto (mockup `Límite: S/ 30,000`; el backend decide).
const int kMaxAmountMinor = 3000000;

/// Argumentos del flujo destino -> monto -> confirmacion.
///
/// Viajan por `extra` del router (memoria, sin persistencia ni PII en claro:
/// solo mascaras + UUIDs). La [draft] lleva la `Idempotency-Key` del intento.
class TransferFlowArgs {
  const TransferFlowArgs({
    required this.draft,
    required this.source,
    this.target,
  });

  final TransferDraft draft;
  final Account source;
  final Account? target;

  TransferFlowArgs withDraft(TransferDraft next) =>
      TransferFlowArgs(draft: next, source: source, target: target);
}

/// Resultado que la confirmacion devuelve al formulario interbancario al
/// volver (el titular/CCI lo valida el backend al confirmar).
///
/// El formulario muestra el banner CCI correspondiente; `unavailable`
/// ofrece reintento con la MISMA `Idempotency-Key` del borrador.
class CciOutcome {
  const CciOutcome(this.state);

  final CciState state;
}
