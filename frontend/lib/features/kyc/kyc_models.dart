/// Modelos del flujo KYC (E1-T05, HU01).
///
/// Formas tomadas UNICAMENTE del contrato observado en
/// `backend/tests/test_kyc_proxy.py` (envelope docs/05 `{data, meta}`):
/// - challenge -> `data: {token, steps, expires_in}`
/// - submit    -> `data: {overall_result, detail_code}`
library;

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

/// Resultado del submit KYC.
class KycSubmitResult {
  const KycSubmitResult({
    required this.overallResult,
    required this.detailCode,
  });

  /// `true` = verificado. `false` = ver `detailCode` (motivo).
  final bool overallResult;

  /// Codigo de detalle del servidor (`OK` en exito; motivo en fallo).
  final String detailCode;

  factory KycSubmitResult.fromData(Map<String, dynamic> data) {
    final overall = data['overall_result'];
    final detail = data['detail_code']?.toString() ?? '';
    if (overall is! bool || detail.isEmpty) {
      throw const FormatException('Respuesta de submit KYC invalida');
    }
    return KycSubmitResult(overallResult: overall, detailCode: detail);
  }
}
