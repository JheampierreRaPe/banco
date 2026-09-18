/// Manejo de errores del flujo KYC (E1-T06, HU01 CA-04).
///
/// Traduce [ApiException] (envelope docs/05 §4 `{error: {code, message, ...}}`)
/// y el caso `passed:false` a un [KycErrorInfo] accionable en español con la
/// acción sugerida.
///
/// Decisiones (docs/13 §1.4 + brief E1-T06):
/// - `passed:false` NO es un error HTTP: es un estado de la tarea que permite
///   reintentar la MISMA tarea. Por eso [forTaskNotPassed] no recibe un
///   [ApiException] sino el contador de intentos.
/// - Token de desafío con TTL: al expirar (401, `code` con `EXPIRED`/token, o
///   TTL local agotado) la acción es pedir un challenge nuevo
///   ([KycErrorAction.refreshChallenge]), conservando el progreso ya pasado
///   (ver `KycFlowController.refreshChallengePreservingProgress`).
/// - Límite de reintentos configurable por tarea (default 3). Al agotarse, la
///   acción es derivar a revisión manual con folio
///   ([KycErrorAction.manualReview]).
/// - El motivo del servidor nunca se oculta: viaja en
///   [KycErrorInfo.serverReason] y las pantallas de estado lo muestran
///   (no se reemplaza por un genérico).
library;

import '../../core/errors/api_exception.dart';

/// Acción sugerida que la UI debe ofrecer ante un error de KYC.
enum KycErrorAction {
  /// Reintentar la MISMA tarea (frames nuevos, mismo challenge).
  retryTask,

  /// El token expiró: pedir un challenge nuevo (conservando lo ya pasado).
  refreshChallenge,

  /// Agotados los reintentos o bloqueo: derivar a revisión manual con folio.
  manualReview,

  /// Reintentar el submit (fallo de red/servidor sin agotar intentos).
  retrySubmit,
}

/// Error de KYC listo para mostrar en la UI.
class KycErrorInfo {
  const KycErrorInfo({
    required this.code,
    required this.message,
    required this.action,
    this.serverReason,
    this.requestId,
    this.attempts = 0,
    this.maxAttempts = KycErrorHandler.defaultMaxAttemptsPerTask,
    this.folio,
  });

  /// `error.code` del servidor, o el seudocódigo `TASK_NOT_PASSED` cuando
  /// `passed:false` (que no es un error HTTP).
  final String code;

  /// Mensaje accionable en español para la UI.
  final String message;

  /// Qué debe ofrecer la UI.
  final KycErrorAction action;

  /// Motivo original del servidor (nunca PII en logs). Se muestra tal cual
  /// para que el fallo persistente no quede en un genérico.
  final String? serverReason;

  /// `request_id` para correlación, si vino en la respuesta.
  final String? requestId;

  /// Intentos consumidos en la tarea y límite vigente.
  final int attempts;
  final int maxAttempts;

  /// Folio de derivación (solo cuando [action] es `manualReview`).
  final String? folio;

  /// `true` cuando ya no quedan reintentos para la tarea.
  bool get attemptsExhausted => attempts >= maxAttempts;
}

/// Clasificador de errores del flujo KYC (puro Dart, sin widgets).
class KycErrorHandler {
  KycErrorHandler._();

  /// Reintentos máximos por tarea antes de derivar a revisión manual.
  static const int defaultMaxAttemptsPerTask = 3;

  /// Códigos de negocio que significan "el challenge ya no vale".
  static const Set<String> expiredTokenCodes = {
    'TOKEN_EXPIRED',
    'CHALLENGE_EXPIRED',
    'CHALLENGE_TOKEN_EXPIRED',
    'KYC_CHALLENGE_EXPIRED',
    'EXPIRED',
  };

  /// Códigos que derivan directo a revisión manual (no tiene sentido reintentar
  /// la captura: el caso quedó retenido en el servidor).
  static const Set<String> manualReviewCodes = {
    'RISK_BLOCKED',
    'SCREENING_HIT',
    'ACCOUNT_BLOCKED',
  };

  /// ¿El error significa "token de desafío expirado"?
  ///
  /// Detecta: HTTP 401 (sin sesión/challenge vigente), `error.code` de
  /// expiración, o cualquier código que contenga `EXPIRED`.
  static bool isTokenExpired(ApiException e) {
    if (e.statusCode == 401) return true;
    final code = e.code.toUpperCase();
    if (expiredTokenCodes.contains(code)) return true;
    return code.contains('EXPIRED');
  }

  /// ¿Venció el TTL local del challenge? (reloj del dispositivo; el servidor
  /// es la autoridad final, esto solo adelanta la renovación).
  static bool isChallengeExpiredByTtl({
    required DateTime? issuedAt,
    required int? expiresInSeconds,
    DateTime? now,
  }) {
    if (issuedAt == null || expiresInSeconds == null) return false;
    final current = now ?? DateTime.now();
    return current.isAfter(
      issuedAt.add(Duration(seconds: expiresInSeconds)),
    );
  }

  /// Clasifica un [ApiException] del challenge/evaluate/submit.
  ///
  /// [task] identifica la tarea en curso (para el contador); [attempts] son
  /// los intentos ya consumidos. Si los intentos se agotaron, cualquier error
  /// recuperable pasa a `manualReview` con [folio].
  static KycErrorInfo fromApiException(
    ApiException e, {
    String? task,
    int attempts = 0,
    int maxAttempts = defaultMaxAttemptsPerTask,
    String? folio,
  }) {
    final reason = e.serverMessage ?? e.message;
    if (isTokenExpired(e)) {
      return KycErrorInfo(
        code: e.code,
        message: 'Tu sesión de verificación venció. Pediremos un nuevo '
            'desafío conservando las tareas que ya pasaste.',
        action: KycErrorAction.refreshChallenge,
        serverReason: reason,
        requestId: e.requestId,
        attempts: attempts,
        maxAttempts: maxAttempts,
      );
    }
    if (manualReviewCodes.contains(e.code.toUpperCase())) {
      return KycErrorInfo(
        code: e.code,
        message: 'Tu verificación quedó en revisión manual. Te avisaremos '
            'el resultado; no necesitas reintentar la captura.',
        action: KycErrorAction.manualReview,
        serverReason: reason,
        requestId: e.requestId,
        attempts: attempts,
        maxAttempts: maxAttempts,
        folio: folio ?? newManualReviewFolio(),
      );
    }
    if (attempts >= maxAttempts) {
      return KycErrorInfo(
        code: e.code,
        message: 'Agotaste los $maxAttempts intentos'
            '${task == null ? '' : ' de "$task"'}. Te derivamos a revisión '
            'manual con el siguiente folio.',
        action: KycErrorAction.manualReview,
        serverReason: reason,
        requestId: e.requestId,
        attempts: attempts,
        maxAttempts: maxAttempts,
        folio: folio ?? newManualReviewFolio(),
      );
    }
    if (e.isNetworkError) {
      return KycErrorInfo(
        code: e.code,
        message: e.message,
        action: task == null
            ? KycErrorAction.retrySubmit
            : KycErrorAction.retryTask,
        serverReason: reason,
        requestId: e.requestId,
        attempts: attempts,
        maxAttempts: maxAttempts,
      );
    }
    return KycErrorInfo(
      code: e.code,
      message: '${e.message} Vuelve a intentarlo.',
      action: task == null
          ? KycErrorAction.retrySubmit
          : KycErrorAction.retryTask,
      serverReason: reason,
      requestId: e.requestId,
      attempts: attempts,
      maxAttempts: maxAttempts,
    );
  }

  /// Clasifica un `passed:false` de la evaluación de tarea.
  ///
  /// No hay [ApiException] aquí: `passed:false` es un estado, no un error
  /// HTTP. Con intentos restantes la acción es reintentar la MISMA tarea;
  /// agotados, derivar a revisión manual con folio. [serverReason] conserva
  /// el motivo del servidor (p. ej. `LOW_QUALITY`) cuando viene informado.
  static KycErrorInfo forTaskNotPassed({
    required String task,
    required int attempts,
    int maxAttempts = defaultMaxAttemptsPerTask,
    String? serverReason,
    String? folio,
  }) {
    if (attempts >= maxAttempts) {
      return KycErrorInfo(
        code: 'TASK_NOT_PASSED',
        message: 'No pudimos validar "$task" tras $maxAttempts intentos. '
            'Te derivamos a revisión manual con el siguiente folio.',
        action: KycErrorAction.manualReview,
        serverReason: serverReason,
        attempts: attempts,
        maxAttempts: maxAttempts,
        folio: folio ?? newManualReviewFolio(),
      );
    }
    return KycErrorInfo(
      code: 'TASK_NOT_PASSED',
      message: 'No pudimos validar "$task" (intento $attempts de '
          '$maxAttempts). Reintenta la misma tarea con buena luz y '
          'movimientos lentos.',
      action: KycErrorAction.retryTask,
      serverReason: serverReason,
      attempts: attempts,
      maxAttempts: maxAttempts,
    );
  }

  /// Folio de derivación a revisión manual (correlación en soporte).
  static String newManualReviewFolio({DateTime? now}) {
    final stamp = (now ?? DateTime.now()).toUtc();
    final compact = stamp
        .toIso8601String()
        .replaceAll(RegExp(r'[-:.]'), '')
        .replaceAll('T', '')
        .substring(0, 14);
    final suffix = (stamp.millisecondsSinceEpoch % 10000)
        .toString()
        .padLeft(4, '0');
    return 'KYC-$compact-$suffix';
  }
}
