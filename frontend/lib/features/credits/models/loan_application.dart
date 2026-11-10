/// Modelos de la solicitud de credito (E3-T09, HU10).
///
/// Cliente delgado (`docs/19` §4): estos modelos solo transportan lo que
/// responde el backend (`POST /credits/applications`,
/// `GET /credits/applications/{id}`, `docs/05` §6.4); el dispositivo NUNCA
/// decide elegibilidad ni calcula DTI/score. Dinero entero en centimos
/// (`*_minor`, regla de oro 3: jamas `float`).
library;

/// Estado del expediente (espejo del enum del servidor, `docs/05` §6.4).
enum LoanApplicationStatus {
  draft,
  submitted,
  inEvaluation,
  approved,
  rejected,
  inReview,
  expired;

  /// Parsea el `status` del servidor (`APPROVED`, `IN_REVIEW`, ...).
  ///
  /// Un valor desconocido colapsa a [inReview] (revision manual, nunca se
  /// promete una aprobacion que el servidor no dio).
  static LoanApplicationStatus fromServer(String? raw) {
    switch (raw?.toUpperCase()) {
      case 'DRAFT':
        return LoanApplicationStatus.draft;
      case 'SUBMITTED':
        return LoanApplicationStatus.submitted;
      case 'IN_EVALUATION':
        return LoanApplicationStatus.inEvaluation;
      case 'APPROVED':
        return LoanApplicationStatus.approved;
      case 'REJECTED':
        return LoanApplicationStatus.rejected;
      case 'EXPIRED':
        return LoanApplicationStatus.expired;
      case 'IN_REVIEW':
      default:
        return LoanApplicationStatus.inReview;
    }
  }

  /// `true` mientras el servidor aun no dicta (contenido transitorio).
  bool get isPending =>
      this == LoanApplicationStatus.draft ||
      this == LoanApplicationStatus.submitted ||
      this == LoanApplicationStatus.inEvaluation;
}

/// Solicitud que envia el cliente (`POST /credits/applications`).
///
/// Shape exacto del contrato (`docs/05` §6.4, `extra="forbid"` en el
/// servidor): solo estas claves; la cuenta de desembolso y el documento que
/// muestra la UI son solo lectura enmascarada y NO viajan (el servidor los
/// rechaza con `422`).
class LoanApplicationRequest {
  const LoanApplicationRequest({
    required this.productId,
    required this.requestedAmountMinor,
    required this.termMonths,
    required this.totalIncomeMinor,
    required this.totalDebtMinor,
    required this.bureauAuthorized,
  });

  final String productId;
  final int requestedAmountMinor;
  final int termMonths;
  final int totalIncomeMinor;
  final int totalDebtMinor;

  /// Consentimiento explicito de consulta al bureau (siempre `true` al
  /// enviar: la UI bloquea el CTA sin el checkbox marcado).
  final bool bureauAuthorized;

  Map<String, dynamic> toJson() => {
        'product_id': productId,
        'requested_amount_minor': requestedAmountMinor,
        'term_months': termMonths,
        'total_income_minor': totalIncomeMinor,
        'total_debt_minor': totalDebtMinor,
        'bureau_authorized': bureauAuthorized,
      };
}

/// Expediente tal como lo devuelve el servidor (`{"data", "meta"}`).
///
/// El dictamen (`status` + `decision_reason`) se muestra TAL CUAL, sin
/// interpretar ni recalcular (cliente delgado).
class LoanApplicationResult {
  const LoanApplicationResult({
    required this.id,
    required this.status,
    this.decisionReason,
    this.requestedAmountMinor,
    this.termMonths,
    this.totalIncomeMinor,
    this.totalDebtMinor,
    this.score,
    this.dtiRatio,
    this.bureauAuthorized,
    this.bureauAuthorizedAt,
    this.decidedAt,
    this.contractSigned = false,
  });

  final String id;
  final LoanApplicationStatus status;

  /// Motivo exacto del servidor (se muestra sin editar).
  final String? decisionReason;
  final int? requestedAmountMinor;
  final int? termMonths;
  final int? totalIncomeMinor;
  final int? totalDebtMinor;
  final int? score;
  final String? dtiRatio;
  final bool? bureauAuthorized;
  final String? bureauAuthorizedAt;
  final String? decidedAt;

  /// Contrato ya firmado (derivado del servidor desde `credits.contracts`:
  /// `true` solo si todas las filas del juego están `SIGNED`). El estado
  /// del expediente no cambia con la firma: un `APPROVED` firmado deja de
  /// ofrecer "Continuar a la firma" y muestra `Firmado`.
  final bool contractSigned;

  static int? _minorOf(Object? value) {
    if (value == null) return null;
    if (value is int) return value;
    if (value is num) return value.toInt();
    return int.tryParse(value.toString());
  }

  static bool _boolOf(Object? value) {
    if (value is bool) return value;
    if (value is num) return value != 0;
    if (value is String) return value.toLowerCase() == 'true';
    return false;
  }

  factory LoanApplicationResult.fromJson(Map<String, dynamic> json) {
    return LoanApplicationResult(
      id: '${json['id']}',
      status: LoanApplicationStatus.fromServer(json['status']?.toString()),
      decisionReason: json['decision_reason']?.toString(),
      requestedAmountMinor: _minorOf(json['requested_amount_minor']),
      termMonths: _minorOf(json['term_months']),
      totalIncomeMinor: _minorOf(json['total_income_minor']),
      totalDebtMinor: _minorOf(json['total_debt_minor']),
      score: _minorOf(json['score']),
      dtiRatio: json['dti_ratio']?.toString(),
      bureauAuthorized: json['bureau_authorized'] is bool
          ? json['bureau_authorized'] as bool
          : null,
      bureauAuthorizedAt: json['bureau_authorized_at']?.toString(),
      decidedAt: json['decided_at']?.toString(),
      contractSigned: _boolOf(json['contract_signed']),
    );
  }

  /// Resultado sintetico para `BUREAU_UNAVAILABLE` en el envio.
  ///
  /// El bureau caido NO es un error visible (`docs/05` §6.4: el caso pasa a
  /// revision manual): la UI muestra `En revision` aunque aun no haya id de
  /// expediente que consultar.
  factory LoanApplicationResult.inReviewNotice({String? reason}) {
    return LoanApplicationResult(
      id: '',
      status: LoanApplicationStatus.inReview,
      decisionReason: reason,
    );
  }
}
