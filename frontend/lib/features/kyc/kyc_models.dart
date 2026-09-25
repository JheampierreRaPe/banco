/// Modelos del flujo KYC (E1-T05 / F-T19, HU01).
///
/// Formas tomadas UNICAMENTE del contrato observado en
/// `backend/tests/test_kyc_proxy.py` (envelope docs/05 `{data, meta}`):
/// - challenge -> `data: {token, steps, expires_in}`
/// - submit    -> `data: {overall_result, detail_code, distance, user_id,
///                        status, account_id}`
library;

/// Tipos de documento que ofrece la UI (valores mostrados al usuario).
///
/// F-T44 (crearCuenta-1Datos `0:1350`): la pantalla de inicio solo ofrece
/// `DNI · RUC`; el `document.type` del submit reutiliza
/// [mapDocumentTypeToApi] (la aceptacion de `RUC` por `submit`, hoy
/// `DNI|CE|PASSPORT`, sigue pendiente de confirmar en el backend).
const List<String> kKycDocumentTypes = ['DNI', 'RUC'];

/// Mapea el tipo de documento de la UI al aceptado por el backend
/// (`DNI|CE|PASSPORT`, mas `RUC` pendiente de confirmar).
///
/// `Pasaporte -> PASSPORT`; `DNI`/`CE`/`RUC` se envian igual.
///
/// El cliente solo traduce el valor de presentacion al del contrato; la
/// validacion de que el tipo exista la hace el servidor (cliente delgado).
String mapDocumentTypeToApi(String uiType) {
  switch (uiType) {
    case 'Pasaporte':
    case 'PASSPORT':
      return 'PASSPORT';
    case 'DNI':
    case 'CE':
    case 'RUC':
      return uiType;
    default:
      return uiType;
  }
}

/// Titular recuperado por documento (E1-T35 / F-T44).
///
/// Forma de `POST /auth/kyc/document/lookup`:
/// `data: {document_type, first_name, last_name, business_name}`.
/// Persona natural: `first_name`/`last_name` con valores y `business_name`
/// vacio; RUC de persona juridica: `business_name` con la razon social y
/// `first_name`/`last_name` vacios.
///
/// Vive SOLO en memoria durante el flujo; no se persiste ni se registra en
/// logs (sin PII en logs, docs/16 §2.7).
class KycDocumentOwner {
  const KycDocumentOwner({
    required this.documentType,
    this.firstName = '',
    this.lastName = '',
    this.businessName = '',
  });

  /// Tipo normalizado por el servidor (`DNI`/`RUC`).
  final String documentType;

  /// Nombres de persona natural (vacios en RUC de persona juridica).
  final String firstName;

  /// Apellidos de persona natural (vacios en RUC de persona juridica).
  final String lastName;

  /// Razon social en RUC de persona juridica (vacia en persona natural).
  final String businessName;

  /// `true` cuando el documento es de persona juridica: la razon social se
  /// muestra donde iria el nombre y los apellidos quedan vacios.
  bool get isBusiness => businessName.trim().isNotEmpty;

  /// Parsea el `data` del envelope docs/05. Lanza [FormatException] si falta
  /// el tipo o si no trae ningun nombre (respuesta sin titular).
  factory KycDocumentOwner.fromData(Map<String, dynamic> data) {
    final type = data['document_type']?.toString() ?? '';
    final firstName = data['first_name']?.toString() ?? '';
    final lastName = data['last_name']?.toString() ?? '';
    final businessName = data['business_name']?.toString() ?? '';
    if (type.isEmpty) {
      throw const FormatException(
        'Respuesta de consulta de documento sin tipo',
      );
    }
    if (firstName.trim().isEmpty &&
        lastName.trim().isEmpty &&
        businessName.trim().isEmpty) {
      throw const FormatException(
        'Respuesta de consulta de documento sin titular',
      );
    }
    return KycDocumentOwner(
      documentType: type,
      firstName: firstName,
      lastName: lastName,
      businessName: businessName,
    );
  }
}

/// Datos del titular capturados en la pantalla de inicio del KYC.
///
/// Viven SOLO en memoria durante el flujo; no se persisten ni se registran en
/// logs (sin PII en logs, docs/16 §2.7).
///
/// F-T48 (RUC real, E1-T36): el titular puede ser persona juridica; en ese
/// caso la razon social viaja en [businessName] y `first_name`/`last_name`
/// pueden ir vacios (el lookup devuelve `business_name` y la pantalla lo
/// muestra donde iria el nombre).
class KycApplicant {
  const KycApplicant({
    required this.firstName,
    required this.lastName,
    required this.email,
    this.phone = '',
    this.businessName = '',
  });

  final String firstName;
  final String lastName;
  final String email;
  final String phone;

  /// Razon social (RUC de persona juridica). Vacia en persona natural;
  /// `DNI|CE|PASSPORT` la ignoran en el servidor.
  final String businessName;

  /// `true` cuando estan los datos minimos que el backend espera. La UI valida
  /// el formato del email; el servidor es la autoridad final. Un RUC juridico
  /// (`businessName` no vacio) es completo aunque no tenga nombres.
  bool get isComplete =>
      (firstName.trim().isNotEmpty && lastName.trim().isNotEmpty ||
          businessName.trim().isNotEmpty) &&
      email.trim().isNotEmpty;

  /// Forma del payload (`applicant` en `POST /auth/kyc/submit`).
  Map<String, dynamic> toJson() => {
        'first_name': firstName,
        'last_name': lastName,
        'business_name': businessName,
        'email': email,
        'phone': phone,
      };
}

/// Desafio KYC emitido por el servidor.
///
/// [steps] impone el orden de las tareas; el cliente nunca lo reordena ni
/// avanza sin `passed:true` (ver `KycFlowController`).
class KycChallenge {
  const KycChallenge({
    required this.token,
    required this.steps,
    required this.expiresIn,
  });

  /// Token opaco del desafio (`challenge_token` en el submit).
  final String token;

  /// Tareas ordenadas impuestas por el servidor (p. ej. `front`, `blink`).
  final List<String> steps;

  /// Segundos de vigencia del desafio.
  final int expiresIn;

  /// Parsea el `data` del envelope docs/05. Lanza [FormatException] si falta
  /// alguna pieza (el llamador lo convierte en error UI).
  factory KycChallenge.fromData(Map<String, dynamic> data) {
    final token = data['token']?.toString() ?? '';
    final rawSteps = data['steps'];
    final expiresIn = data['expires_in'];
    if (token.isEmpty || rawSteps is! List || expiresIn is! int) {
      throw const FormatException('Respuesta de challenge KYC invalida');
    }
    final steps = rawSteps.map((e) => e.toString()).toList();
    if (steps.isEmpty) {
      throw const FormatException('Respuesta de challenge KYC sin pasos');
    }
    return KycChallenge(token: token, steps: steps, expiresIn: expiresIn);
  }
}

/// Resultado de la validacion del documento (E1-T30 / F-T26).
///
/// Forma de `POST /auth/kyc/document/validate`:
/// `data: {is_valid, issues: [str], checks: {}}`.
///
/// `is_valid=false` NO es un error HTTP: es un 200 con los `issues`. El
/// cliente solo transporta la decision del servidor (cliente delgado, docs/19)
/// y nunca registra la imagen ni los motivos en logs.
class KycDocumentValidation {
  const KycDocumentValidation({
    required this.isValid,
    this.issues = const [],
    this.checks = const {},
  });

  /// `true` si el servidor acepta el documento; `false` = mostrar [issues].
  final bool isValid;

  /// Motivos devueltos por el servidor cuando `isValid` es `false`.
  final List<String> issues;

  /// Detalle opcional del servidor (`data.checks`); no se decide con el.
  final Map<String, dynamic> checks;

  /// Parsea el `data` del envelope docs/05. `issues`/`checks` toleran formas
  /// inesperadas (se normalizan a lista/mapa vacios) sin romper.
  factory KycDocumentValidation.fromData(Map<String, dynamic> data) {
    final valid = data['is_valid'];
    if (valid is! bool) {
      throw const FormatException(
        'Respuesta de validacion de documento invalida',
      );
    }
    final rawIssues = data['issues'];
    final issues = rawIssues is List
        ? rawIssues.map((e) => e.toString()).toList(growable: false)
        : const <String>[];
    final rawChecks = data['checks'];
    final checks = rawChecks is Map
        ? Map<String, dynamic>.from(rawChecks)
        : const <String, dynamic>{};
    return KycDocumentValidation(
      isValid: valid,
      issues: issues,
      checks: checks,
    );
  }
}

/// Resultado de la evaluacion de UN paso de liveness (E1-T29 / F-T23).
///
/// Forma de `POST /auth/kyc/evaluate`:
/// `data: {step, passed, reason, frames_analyzed, details}`.
/// El cliente solo transporta la decision del servidor (cliente delgado).
class KycTaskEvaluation {
  const KycTaskEvaluation({
    required this.step,
    required this.passed,
    this.reason,
    this.framesAnalyzed = 0,
  });

  /// Paso evaluado (el nombre impuesto por el servidor).
  final String step;

  /// `true` = la tarea paso; `false` = reintentar la MISMA tarea.
  final bool passed;

  /// Motivo del microservicio cuando `passed:false` (p. ej. `MIN_FRAMES`,
  /// `NO_BLINK`); `null`/vacio en exito.
  final String? reason;

  /// Cantidad de frames analizados por el servidor.
  final int framesAnalyzed;

  factory KycTaskEvaluation.fromData(
    Map<String, dynamic> data, {
    String? fallbackStep,
  }) {
    final passed = data['passed'];
    if (passed is! bool) {
      throw const FormatException('Respuesta de evaluate KYC invalida');
    }
    final step = data['step']?.toString() ?? fallbackStep ?? '';
    final reason = data['reason']?.toString() ?? '';
    return KycTaskEvaluation(
      step: step,
      passed: passed,
      reason: reason.isEmpty ? null : reason,
      framesAnalyzed: data['frames_analyzed'] is int
          ? data['frames_analyzed'] as int
          : 0,
    );
  }
}

/// Resultado de un paso dentro de `step_results` del submit (E1-T29).
class KycStepResult {
  const KycStepResult({
    required this.passed,
    this.reason,
  });

  final bool passed;

  /// Motivo del servidor para el paso (`null`/vacio si no informa).
  final String? reason;

  factory KycStepResult.fromJson(Object? value) {
    if (value is bool) return KycStepResult(passed: value);
    if (value is Map) {
      final map = Map<String, dynamic>.from(value);
      final reason = map['reason']?.toString() ?? '';
      return KycStepResult(
        passed: map['passed'] == true,
        reason: reason.isEmpty ? null : reason,
      );
    }
    return const KycStepResult(passed: false);
  }
}

/// Resultado del submit KYC.
///
/// F-T19 amplia el contrato: ademas de [overallResult]/[detailCode] expone
/// [userId], [status] y [accountId] que devuelve el backend (E1-T24) en el
/// exito. F-T23 suma el detalle por paso de E1-T29 ([stepsVerified],
/// [stepsTotal], [failedStep], [stepResults], [overallReason]) para mostrar
/// QUE paso fallo y por que. Fallback documentado: si el backend legacy no
/// los envia, quedan `null`/vacios y la UI sigue funcionando.
class KycSubmitResult {
  const KycSubmitResult({
    required this.overallResult,
    required this.detailCode,
    this.distance,
    this.userId,
    this.status,
    this.accountId,
    this.stepsVerified,
    this.stepsTotal,
    this.failedStep,
    this.stepResults = const {},
    this.overallReason,
  });

  /// `true` = verificado. `false` = ver [detailCode] (motivo).
  final bool overallResult;

  /// Codigo de detalle del servidor (`OK` en exito; motivo en fallo).
  final String detailCode;

  /// Distancia del match facial reportada por el servidor (opcional).
  final num? distance;

  /// Id del usuario creado/verificado (`data.user_id`), disponible para el
  /// flujo de alta (F-T20). `null` si el backend no lo devuelve.
  final String? userId;

  /// Estado del usuario/cuenta reportado por el servidor (opcional).
  final String? status;

  /// Id de la cuenta creada (`data.account_id`), si aplica.
  final String? accountId;

  /// Pasos de liveness verificados y totales (E1-T29). El microservicio los
  /// entrega como LISTAS de nombres de paso (no conteos).
  final List<String>? stepsVerified;
  final List<String>? stepsTotal;

  /// Paso exacto que fallo (`data.failed_step`), o `null` si no aplica.
  final String? failedStep;

  /// Detalle por paso (`data.step_results`: `{paso: {passed, reason}}`).
  final Map<String, KycStepResult> stepResults;

  /// Motivo integral del microservicio (`data.overall_reason`), si vino.
  final String? overallReason;

  factory KycSubmitResult.fromData(Map<String, dynamic> data) {
    final overall = data['overall_result'];
    final detail = data['detail_code']?.toString() ?? '';
    if (overall is! bool || detail.isEmpty) {
      throw const FormatException('Respuesta de submit KYC invalida');
    }
    final rawSteps = data['step_results'];
    final stepResults = <String, KycStepResult>{};
    if (rawSteps is Map) {
      rawSteps.forEach((key, value) {
        stepResults[key.toString()] = KycStepResult.fromJson(value);
      });
    }
    return KycSubmitResult(
      overallResult: overall,
      detailCode: detail,
      distance: data['distance'] is num ? data['distance'] as num : null,
      userId: _optionalString(data['user_id']),
      status: _optionalString(data['status']),
      accountId: _optionalString(data['account_id']),
      stepsVerified: _optionalStringList(data['steps_verified']),
      stepsTotal: _optionalStringList(data['steps_total']),
      failedStep: _optionalString(data['failed_step']),
      stepResults: stepResults,
      overallReason: _optionalString(data['overall_reason']),
    );
  }

  /// Motivo a mostrar cuando [overallResult] es `false`: prioriza
  /// [overallReason] y cae a [detailCode] (compatibilidad legacy).
  String get failureReason {
    final reason = overallReason;
    if (reason != null && reason.isNotEmpty) return reason;
    return detailCode;
  }

  /// Normaliza opcionales: `null`/vacio -> `null` (no rompe el parseo legacy).
  static String? _optionalString(Object? value) {
    final text = value?.toString() ?? '';
    return text.isEmpty ? null : text;
  }

  /// Normaliza una lista de nombres de paso (`steps_verified`/`steps_total`).
  /// Acepta `List`; cualquier otra forma (p. ej. un int legacy) -> `null`.
  static List<String>? _optionalStringList(Object? value) {
    if (value is! List) return null;
    return value.map((e) => e.toString()).toList(growable: false);
  }
}
