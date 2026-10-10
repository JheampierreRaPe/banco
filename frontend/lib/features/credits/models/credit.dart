/// Modelos del simulador de creditos (E3-T04, HU09).
///
/// Cliente delgado (`docs/19` §4): estos modelos solo transportan y presentan
/// lo que responde el backend (`GET /credits/products`,
/// `POST /credits/simulate`); el dispositivo NUNCA calcula cuotas, TEA, TCEA
/// ni saldos. Dinero entero en centimos (`*_minor`); tasas como strings
/// decimales exactos tal como viajan en el JSON.
library;

/// Producto del catalogo `GET /credits/products` (solo habilitados).
class LoanProduct {
  const LoanProduct({
    required this.id,
    required this.code,
    required this.name,
    required this.tcea,
    this.tea,
    required this.insuranceRate,
    required this.minAmountMinor,
    required this.maxAmountMinor,
    required this.minTermMonths,
    required this.maxTermMonths,
    this.allowedTerms,
    required this.enabled,
  });

  final String id;
  final String code;
  final String name;

  /// Tasa anual (string decimal exacto, p. ej. `"0.185"`).
  final String tcea;

  /// TEA (string decimal exacto; puede venir `null`).
  final String? tea;
  final String insuranceRate;
  final int minAmountMinor;
  final int maxAmountMinor;
  final int minTermMonths;
  final int maxTermMonths;

  /// Plazos permitidos (`null` = solo manda el rango min/max).
  final List<int>? allowedTerms;
  final bool enabled;

  factory LoanProduct.fromJson(Map<String, dynamic> json) {
    final rawTerms = json['allowed_terms'];
    return LoanProduct(
      id: '${json['id']}',
      code: '${json['code']}',
      name: '${json['name']}',
      tcea: '${json['tcea']}',
      tea: json['tea'] == null ? null : '${json['tea']}',
      insuranceRate: '${json['insurance_rate']}',
      minAmountMinor: (json['min_amount_minor'] as num).toInt(),
      maxAmountMinor: (json['max_amount_minor'] as num).toInt(),
      minTermMonths: (json['min_term_months'] as num).toInt(),
      maxTermMonths: (json['max_term_months'] as num).toInt(),
      allowedTerms: rawTerms is List
          ? rawTerms.map((e) => (e as num).toInt()).toList()
          : null,
      enabled: json['enabled'] != false,
    );
  }
}

/// Cuota del cronograma provisional (forma de `03b#84`, sin persistir).
class ScheduleRow {
  const ScheduleRow({
    required this.installmentNo,
    required this.principalMinor,
    required this.interestMinor,
    required this.insuranceMinor,
    required this.totalMinor,
    required this.balanceMinor,
  });

  final int installmentNo;
  final int principalMinor;
  final int interestMinor;
  final int insuranceMinor;
  final int totalMinor;
  final int balanceMinor;

  factory ScheduleRow.fromJson(Map<String, dynamic> json) {
    return ScheduleRow(
      installmentNo: (json['installment_no'] as num).toInt(),
      principalMinor: (json['principal_minor'] as num).toInt(),
      interestMinor: (json['interest_minor'] as num).toInt(),
      insuranceMinor: (json['insurance_minor'] as num).toInt(),
      totalMinor: (json['total_minor'] as num).toInt(),
      balanceMinor: (json['balance_minor'] as num).toInt(),
    );
  }
}

/// Resultado de `POST /credits/simulate`: cuota, TEA/TCEA y cronograma,
///
/// Todo calculado por el servidor; la UI lo muestra tal cual.
class SimulationResult {
  const SimulationResult({
    required this.productId,
    required this.productCode,
    required this.productName,
    required this.amountMinor,
    required this.termMonths,
    required this.insurance,
    required this.tea,
    required this.tcea,
    required this.paymentMinor,
    required this.schedule,
  });

  final String? productId;
  final String? productCode;
  final String? productName;
  final int amountMinor;
  final int termMonths;
  final bool insurance;

  /// TEA aplicada (string decimal exacto del servidor).
  final String tea;

  /// TCEA con desgravamen (string decimal exacto del servidor).
  final String tcea;

  /// Cuota base francesa en centimos (la que muestra la card de cuota).
  final int paymentMinor;
  final List<ScheduleRow> schedule;

  factory SimulationResult.fromJson(Map<String, dynamic> json) {
    final rawProduct = json['product'];
    final product = rawProduct is Map
        ? Map<String, dynamic>.from(rawProduct)
        : null;
    final rawSchedule = json['schedule'];
    return SimulationResult(
      productId:
          product == null ? null : product['id']?.toString(),
      productCode:
          product == null ? null : product['code']?.toString(),
      productName:
          product == null ? null : product['name']?.toString(),
      amountMinor: (json['amount_minor'] as num).toInt(),
      termMonths: (json['term_months'] as num).toInt(),
      insurance: json['insurance'] == true,
      tea: '${json['tea']}',
      tcea: '${json['tcea']}',
      paymentMinor: (json['payment_minor'] as num).toInt(),
      schedule: rawSchedule is List
          ? rawSchedule
              .whereType<Map>()
              .map((e) => ScheduleRow.fromJson(
                  Map<String, dynamic>.from(e)))
              .toList()
          : const [],
    );
  }
}

/// Borrador que el simulador entrega a la solicitud (gancho E3-T09).
///
/// La pantalla de solicitud (`/credits/apply`, E3-T09) lo recibe por `extra`
/// y lo pre-carga; este feature no la implementa.
class SimulationDraft {
  const SimulationDraft({
    required this.productId,
    required this.productName,
    required this.amountMinor,
    required this.termMonths,
    required this.insurance,
    required this.paymentMinor,
  });

  final String productId;
  final String productName;
  final int amountMinor;
  final int termMonths;
  final bool insurance;
  final int paymentMinor;
}
