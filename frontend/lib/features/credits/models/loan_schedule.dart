/// Modelos del cronograma definitivo del préstamo (E3-T21, HU12).
///
/// Cliente delgado (`docs/19` §4): estos modelos solo transportan lo que
/// responde el backend (`GET /credits/loans`,
/// `GET /credits/loans/{id}/schedule`, `docs/05` §6.4); el dispositivo NUNCA
/// calcula cuotas, intereses, saldos ni mora: muestra los `*_minor` (céntimos,
/// regla de oro 3: jamás `float`) y el `status` TAL CUAL los manda el
/// servidor.
///
/// > Nota de contrato: el brief pedía el toggle con `{auto_debit_enabled}`,
/// > pero el servidor expone `PUT .../auto-debit` con `{enabled: bool}`
/// > estricto y responde `{loan_id, auto_debit_enabled}` (`docs/05` §6.4,
/// > detalle E3-T19). El servicio envía `{enabled}`.
library;

/// Estado de una cuota según el servidor (`installment_status`).
enum LoanInstallmentStatus {
  pending,
  partial,
  paid,
  overdue;

  /// Parsea el `status` del servidor (`PENDING`, `PARTIAL`, ...).
  ///
  /// Un valor desconocido colapsa a [pending] (se sigue ofreciendo el pago
  /// manual; el servidor decide el resultado real).
  static LoanInstallmentStatus fromServer(String? raw) {
    switch (raw?.toUpperCase()) {
      case 'PARTIAL':
        return LoanInstallmentStatus.partial;
      case 'PAID':
        return LoanInstallmentStatus.paid;
      case 'OVERDUE':
        return LoanInstallmentStatus.overdue;
      case 'PENDING':
      default:
        return LoanInstallmentStatus.pending;
    }
  }

  /// Etiqueta en español para la UI (sin reinterpretar el estado).
  String get label => switch (this) {
        LoanInstallmentStatus.pending => 'Pendiente',
        LoanInstallmentStatus.partial => 'Parcial',
        LoanInstallmentStatus.paid => 'Pagada',
        LoanInstallmentStatus.overdue => 'Vencida',
      };

  /// `true` si la cuota admite pago manual (el servidor cobra el total).
  bool get isPayable =>
      this == LoanInstallmentStatus.pending ||
      this == LoanInstallmentStatus.partial ||
      this == LoanInstallmentStatus.overdue;
}

/// Préstamo del titular tal como lo devuelve `GET /credits/loans`.
///
/// El `account_id` es un UUID lógico (sin número en claro): la UI no lo
/// muestra (regla de oro 7); solo viaja como referencia interna.
class Loan {
  const Loan({
    required this.id,
    required this.applicationId,
    required this.principalMinor,
    required this.outstandingPrincipalMinor,
    required this.termMonths,
    required this.status,
    required this.autoDebitEnabled,
    this.tcea,
    this.disbursedAt,
    this.createdAt,
  });

  final String id;
  final String applicationId;
  final int principalMinor;
  final int outstandingPrincipalMinor;
  final int termMonths;

  /// Estado del préstamo según el servidor (`ACTIVE`, `PAID`, ...).
  final String status;
  final bool autoDebitEnabled;
  final String? tcea;
  final String? disbursedAt;
  final String? createdAt;

  static int _minorOf(Object? value) {
    if (value is int) return value;
    if (value is num) return value.toInt();
    return int.tryParse('${value ?? 0}') ?? 0;
  }

  static bool _boolOf(Object? value) {
    if (value is bool) return value;
    if (value is num) return value != 0;
    if (value is String) return value.toLowerCase() == 'true';
    return false;
  }

  factory Loan.fromJson(Map<String, dynamic> json) {
    return Loan(
      id: '${json['id']}',
      applicationId: '${json['application_id']}',
      principalMinor: _minorOf(json['principal_minor']),
      outstandingPrincipalMinor:
          _minorOf(json['outstanding_principal_minor']),
      termMonths: _minorOf(json['term_months']),
      status: '${json['status'] ?? ''}',
      autoDebitEnabled: _boolOf(json['auto_debit_enabled']),
      tcea: json['tcea']?.toString(),
      disbursedAt: json['disbursed_at']?.toString(),
      createdAt: json['created_at']?.toString(),
    );
  }

  /// Etiqueta en español del estado del préstamo (sin reinterpretar).
  String get statusLabel => switch (status.toUpperCase()) {
        'ACTIVE' => 'Activo',
        'PAID' => 'Pagado',
        'OVERDUE' => 'En mora',
        _ => status.isEmpty ? 'Préstamo' : status,
      };
}

/// Cuota del cronograma definitivo (`GET /credits/loans/{id}/schedule`).
///
/// Todos los montos vienen del servidor (capital/interés/seguro/total en
/// céntimos); la UI los muestra sin sumarlos ni recalcularlos.
class LoanInstallment {
  const LoanInstallment({
    required this.installmentNo,
    required this.dueDate,
    required this.principalMinor,
    required this.interestMinor,
    required this.insuranceMinor,
    required this.totalMinor,
    required this.status,
    this.paidAt,
  });

  final int installmentNo;
  final String dueDate;
  final int principalMinor;
  final int interestMinor;
  final int insuranceMinor;
  final int totalMinor;
  final LoanInstallmentStatus status;
  final String? paidAt;

  static int _minorOf(Object? value) {
    if (value is int) return value;
    if (value is num) return value.toInt();
    return int.tryParse('${value ?? 0}') ?? 0;
  }

  factory LoanInstallment.fromJson(Map<String, dynamic> json) {
    return LoanInstallment(
      installmentNo: _minorOf(json['installment_no']),
      dueDate: '${json['due_date'] ?? ''}',
      principalMinor: _minorOf(json['principal_minor']),
      interestMinor: _minorOf(json['interest_minor']),
      insuranceMinor: _minorOf(json['insurance_minor']),
      totalMinor: _minorOf(json['total_minor']),
      status: LoanInstallmentStatus.fromServer(json['status']?.toString()),
      paidAt: json['paid_at']?.toString(),
    );
  }
}

/// Cronograma definitivo: préstamo + cuotas (`data: {loan, schedule}`).
class LoanSchedule {
  const LoanSchedule({required this.loan, required this.installments});

  final Loan loan;
  final List<LoanInstallment> installments;

  factory LoanSchedule.fromJson(Map<String, dynamic> json) {
    final loanRaw = json['loan'];
    final scheduleRaw = json['schedule'];
    final loan = loanRaw is Map
        ? Loan.fromJson(Map<String, dynamic>.from(loanRaw))
        : throw const FormatException('Cronograma sin préstamo');
    final rows = scheduleRaw is List ? scheduleRaw : const [];
    return LoanSchedule(
      loan: loan,
      installments: rows
          .whereType<Map>()
          .map(
            (e) => LoanInstallment.fromJson(Map<String, dynamic>.from(e)),
          )
          .toList(),
    );
  }

  /// `true` si queda alguna cuota por cobrar (para ofrecer el pago manual).
  bool get hasPayable =>
      installments.any((e) => e.status.isPayable);
}
