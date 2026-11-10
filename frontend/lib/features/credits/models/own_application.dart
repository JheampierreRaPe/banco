library;

/// Expediente propio del titular para "Mis créditos" (E3-T23, HU10/HU11).
///
/// Vista mínima del servidor (`GET /credits/applications` sin `status`,
/// `docs/05` §6.4 + spec `docs/design/creditos-pantallas.md` §7): solo lo que
/// la UI necesita (id, producto, monto, plazo, estado, motivo y fechas). Sin
/// ingresos/deuda/score/documento (regla de oro 7).
///
/// Cliente delgado (`docs/19` §4): solo transporta; el `status` y el
/// `decision_reason` se muestran TAL CUAL, sin reinterpretar. Dinero entero
/// en céntimos (`*_minor`, regla de oro 3: jamás `float`).

import 'loan_application.dart';

/// Etiqueta en español del estado (mismo set que la pantalla de estado,
/// `creditos-pantallas.md` §3/§7).
///
/// `DRAFT|SUBMITTED|IN_EVALUATION → Evaluando`, `IN_REVIEW → En revisión`,
/// `APPROVED → Aprobado`, `REJECTED → Rechazado`, `EXPIRED → Vencido`.
String ownApplicationStatusLabel(LoanApplicationStatus status) {
  switch (status) {
    case LoanApplicationStatus.approved:
      return 'Aprobado';
    case LoanApplicationStatus.rejected:
      return 'Rechazado';
    case LoanApplicationStatus.inReview:
      return 'En revisión';
    case LoanApplicationStatus.expired:
      return 'Vencido';
    case LoanApplicationStatus.draft:
    case LoanApplicationStatus.submitted:
    case LoanApplicationStatus.inEvaluation:
      return 'Evaluando';
  }
}

/// Expediente del titular tal como lo devuelve el listado (`{"data", "meta"}`).
class OwnCreditApplication {
  const OwnCreditApplication({
    required this.id,
    required this.productId,
    this.productName,
    required this.requestedAmountMinor,
    required this.termMonths,
    required this.status,
    this.contractSigned = false,
    this.decisionReason,
    this.createdAt,
    this.updatedAt,
    this.decidedAt,
  });

  final String id;
  final String productId;

  /// Nombre del producto (el servidor lo incluye; si falta, la UI usa el id).
  final String? productName;
  final int requestedAmountMinor;
  final int termMonths;
  final LoanApplicationStatus status;

  /// Contrato firmado (derivado del servidor desde `credits.contracts`:
  /// `true` solo si todas las filas del juego están `SIGNED`). El estado
  /// del expediente no cambia con la firma: un `APPROVED` firmado deja de
  /// ofrecer "Continuar a la firma" y muestra el badge `Firmado`.
  final bool contractSigned;

  /// Motivo exacto del servidor (vive en la pantalla de estado; la fila no
  /// lo muestra para no saturarla, spec §7).
  final String? decisionReason;
  final String? createdAt;
  final String? updatedAt;
  final String? decidedAt;

  /// Nombre visible del producto (`product_name ?? product_id`).
  String get displayProduct {
    final name = productName?.trim();
    if (name != null && name.isNotEmpty) return name;
    return productId;
  }

  static int _minorOf(Object? value, {int fallback = 0}) {
    if (value == null) return fallback;
    if (value is int) return value;
    if (value is num) return value.toInt();
    return int.tryParse(value.toString()) ?? fallback;
  }

  static bool _boolOf(Object? value) {
    if (value is bool) return value;
    if (value is num) return value != 0;
    if (value is String) return value.toLowerCase() == 'true';
    return false;
  }

  factory OwnCreditApplication.fromJson(Map<String, dynamic> json) {
    return OwnCreditApplication(
      id: '${json['id']}',
      productId: '${json['product_id']}',
      productName: json['product_name']?.toString(),
      requestedAmountMinor: _minorOf(json['requested_amount_minor']),
      termMonths: _minorOf(json['term_months']),
      status: LoanApplicationStatus.fromServer(json['status']?.toString()),
      contractSigned: _boolOf(json['contract_signed']),
      decisionReason: json['decision_reason']?.toString(),
      createdAt: json['created_at']?.toString(),
      updatedAt: json['updated_at']?.toString(),
      decidedAt: json['decided_at']?.toString(),
    );
  }
}

/// Página del listado (`data` + `meta.total/limit/offset`).
class OwnApplicationsPage {
  const OwnApplicationsPage({
    required this.items,
    required this.total,
    this.limit,
    this.offset,
  });

  final List<OwnCreditApplication> items;
  final int total;
  final int? limit;
  final int? offset;

  factory OwnApplicationsPage.fromJson(Map<String, dynamic> json) {
    final rawItems = json['data'];
    final items = rawItems is List
        ? rawItems
            .whereType<Map>()
            .map((e) => OwnCreditApplication.fromJson(
                  Map<String, dynamic>.from(e),
                ))
            .toList()
        : const <OwnCreditApplication>[];
    final meta = json['meta'];
    final metaMap = meta is Map ? Map<String, dynamic>.from(meta) : const {};
    int? intOf(Object? value) {
      if (value == null) return null;
      if (value is int) return value;
      if (value is num) return value.toInt();
      return int.tryParse(value.toString());
    }

    return OwnApplicationsPage(
      items: items,
      total: intOf(metaMap['total']) ?? items.length,
      limit: intOf(metaMap['limit']),
      offset: intOf(metaMap['offset']),
    );
  }
}
