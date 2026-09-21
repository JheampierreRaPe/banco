import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../core/errors/api_exception.dart';
import 'camera_frame_source.dart';
import 'kyc_error_handler.dart';
import 'kyc_frame_capture.dart';
import 'kyc_frame_source.dart';
import 'kyc_models.dart';
import 'kyc_service.dart';

/// Decide si una tarea capturada pasa (`true`) o debe reintentarse (`false`).
///
/// Implementacion por defecto: mock que siempre pasa. El evaluador real
/// (endpoint de evaluacion por tarea del backend) se inyecta aqui sin tocar
/// las pantallas. Si lanza [ApiException] (fallo de red), el controlador
/// conserva token, paso actual y frames.
typedef KycTaskEvaluator = Future<bool> Function(
  String task,
  List<Uint8List> frames,
);

/// Evaluador que devuelve el detalle REAL del servidor (F-T23):
/// `passed` + `reason` de `POST /auth/kyc/evaluate`.
typedef KycDetailedTaskEvaluator = Future<KycTaskEvaluation> Function(
  String task,
  List<Uint8List> frames,
);

/// Validador del documento inyectable (tests): recibe la imagen en memoria y
/// devuelve la decision del servidor (F-T26). En produccion se usa
/// [KycEvaluationService.validateDocument] (`HttpKycService`).
typedef KycDocumentValidator = Future<KycDocumentValidation> Function(
  Uint8List image,
);

/// Fase de presentacion de la tarea vigente (F-T25).
///
/// Solo informa a la UI que parte del trabajo esta en curso: capturar la
/// rafaga ([capturing]) vs. esperar la evaluacion del servidor ([evaluating]).
/// Es estado de presentacion para el overlay; NO decide nada (cliente delgado,
/// docs/19) ni altera el resultado, que siempre lo da el backend.
enum KycTaskPhase { idle, capturing, evaluating }

/// Estado del flujo KYC guiado por el servidor (E1-T05).
///
/// Reglas (docs/13 §1.4 + brief):
/// - El orden de [steps] lo impone el servidor; no se avanza sin `passed:true`.
/// - Si `passed:false`, se reintenta la MISMA tarea ([attemptsOf] crece).
/// - Un fallo de red NO pierde el desafio: conserva token, paso y frames y
///   solo expone [errorMessage].
/// - Los frames viven solo en memoria durante el submit.
///
/// Extensión E1-T06 (aditiva, no cambia el comportamiento E1-T05 cuando no se
/// agotan intentos ni expira el token):
/// - [maxAttemptsPerTask] (default
///   [KycErrorHandler.defaultMaxAttemptsPerTask]): al agotarse en una tarea,
///   el controlador genera [manualReviewFolio] y expone [lastError] con acción
///   `manualReview` (pantalla de derivación).
/// - Token expirado (401 / `code` con EXPIRED / TTL local): acción sugerida
///   `refreshChallenge`; [refreshChallengePreservingProgress] pide un challenge
///   nuevo conservando las tareas ya pasadas que coincidan con los pasos
///   nuevos. Si el servidor emite pasos distintos, solo se conserva la
///   intersección (documentado: sin coincidencia equivale a reinicio).
class KycFlowController extends ChangeNotifier {
  KycFlowController({
    required this._service,
    this._taskEvaluator,
    this._detailedTaskEvaluator,
    this._evaluationService,
    this._documentValidator,
    KycFrameSource? frameSource,
    this._maxAttemptsPerTask = KycErrorHandler.defaultMaxAttemptsPerTask,
  }) : _frameSource = frameSource ?? MockKycFrameSource();

  final KycService _service;

  /// Evaluador booleano inyectado por tests legacy; `null` en produccion.
  final KycTaskEvaluator? _taskEvaluator;

  /// Evaluador detallado (tests y cables que ya traen `reason`).
  final KycDetailedTaskEvaluator? _detailedTaskEvaluator;

  /// Servicio real de evaluacion/submit (E1-T29). En produccion lo provee
  /// `HttpKycService` (via `KycDependencies`); `null` en los tests legacy.
  final KycEvaluationService? _evaluationService;

  /// Validador del documento inyectado por tests (F-T26). En produccion se
  /// resuelve con [_evaluationService] (`HttpKycService.validateDocument`).
  final KycDocumentValidator? _documentValidator;

  /// Fuente de frames inyectable (seam E1-T05).
  ///
  /// Por defecto [MockKycFrameSource] (tests y CI sin camara). En produccion
  /// el orquestador inyecta `CameraFrameSource()` para fotos reales:
  /// `KycFlowController(service: s, frameSource: CameraFrameSource())`.
  final KycFrameSource _frameSource;

  /// Fuente vigente (ADITIVO viewfinder): la página la inspecciona para
  /// decidir entre preview en vivo ([CameraFrameSource]) o placeholder mock.
  /// Solo lectura; no cambia el comportamiento de captura/submit.
  KycFrameSource get frameSource => _frameSource;

  /// Límite de reintentos por tarea antes de derivar a revisión manual (E1-T06).
  final int _maxAttemptsPerTask;
  int get maxAttemptsPerTask => _maxAttemptsPerTask;

  KycChallenge? _challenge;

  /// Momento de emisión del desafío vigente (TTL local, E1-T06).
  DateTime? _challengeIssuedAt;
  DateTime? get challengeIssuedAt => _challengeIssuedAt;

  /// Último error clasificado (E1-T06). `null` si no hay error vigente.
  KycErrorInfo? _lastError;
  KycErrorInfo? get lastError => _lastError;

  /// Folio de derivación a revisión manual (E1-T06). `null` si no se derivó.
  String? _manualReviewFolio;
  String? get manualReviewFolio => _manualReviewFolio;
  String _documentType = 'DNI';
  String _documentNumber = '';
  KycApplicant? _applicant;
  int _stepIndex = 0;
  final Map<String, List<Uint8List>> _framesByTask = {};
  final Set<String> _passedTasks = {};
  final Map<String, int> _attemptsByTask = {};
  bool _busy = false;
  KycTaskPhase _taskPhase = KycTaskPhase.idle;
  String? _errorMessage;
  KycSubmitResult? _result;

  /// Foto real del documento (cámara trasera, F-T23). Solo en memoria.
  Uint8List? _documentImage;

  /// Resultado de la validacion del documento (E1-T30 / F-T26).
  KycDocumentValidation? _documentValidation;
  List<String> _documentIssues = const [];
  String? _documentValidationError;
  bool _validatingDocument = false;

  KycChallenge? get challenge => _challenge;
  String? get token => _challenge?.token;
  List<String> get steps => _challenge?.steps ?? const [];
  String get documentType => _documentType;
  String get documentNumber => _documentNumber;

  /// Datos del titular capturados en la pantalla de inicio (F-T19). Viven solo
  /// en memoria; nunca se registran en logs.
  KycApplicant? get applicant => _applicant;
  int get currentStepIndex => _stepIndex;

  String? get currentStep {
    final s = steps;
    if (s.isEmpty) return null;
    return s[_stepIndex.clamp(0, s.length - 1)];
  }

  bool get isLastStep => steps.isNotEmpty && _stepIndex >= steps.length - 1;
  bool get busy => _busy;

  /// Fase vigente de la tarea para el overlay de la pantalla (F-T25).
  KycTaskPhase get taskPhase => _taskPhase;
  String? get errorMessage => _errorMessage;
  KycSubmitResult? get result => _result;

  /// `true` cuando TODAS las tareas del servidor pasaron (listo para submit).
  bool get readyToSubmit =>
      steps.isNotEmpty && steps.every(_passedTasks.contains);

  int attemptsOf(String task) => _attemptsByTask[task] ?? 0;
  bool taskPassed(String task) => _passedTasks.contains(task);
  int framesCountOf(String task) => _framesByTask[task]?.length ?? 0;

  /// Foto del documento capturada (cámara trasera) o `null` si aun no.
  Uint8List? get documentImage => _documentImage;

  /// `true` cuando ya se capturo la foto del documento (F-T23).
  bool get hasDocumentImage => _documentImage != null;

  /// Resultado de la validacion del documento (E1-T30), o `null` si aun no.
  KycDocumentValidation? get documentValidation => _documentValidation;

  /// `true` mientras el servidor valida el documento (indicador en UI).
  bool get validatingDocument => _validatingDocument;

  /// Motivos devueltos por el servidor cuando el documento es invalido.
  List<String> get documentIssues => _documentIssues;

  /// Mensaje de error de transporte al validar (reintentable, sin perder la
  /// captura). `null` si no hubo fallo de red/servicio.
  String? get documentValidationError => _documentValidationError;

  /// `true` si el ultimo documento validado fue aceptado por el servidor.
  bool get documentIsValid => _documentValidation?.isValid == true;

  /// Intentos restantes para [task] antes de derivar a revisión (E1-T06).
  int attemptsLeftOf(String task) =>
      (_maxAttemptsPerTask - attemptsOf(task)).clamp(0, _maxAttemptsPerTask);

  /// `true` si [task] agotó sus reintentos y debe ir a revisión manual.
  bool needsManualReview(String task) =>
      attemptsOf(task) >= _maxAttemptsPerTask;

  /// `true` si alguna tarea agotó sus reintentos.
  bool get needsManualReviewAny => steps.any(needsManualReview);

  /// `true` si el TTL local del desafío ya venció (el servidor es la
  /// autoridad final; esto solo adelanta la renovación, E1-T06).
  bool get challengeExpiredLocally =>
      KycErrorHandler.isChallengeExpiredByTtl(
        issuedAt: _challengeIssuedAt,
        expiresInSeconds: _challenge?.expiresIn,
      );

  void setDocument({required String type, required String number}) {
    _documentType = type;
    _documentNumber = number;
    notifyListeners();
  }

  /// Guarda los datos del titular (F-T19). Se propagan hasta el submit.
  void setApplicant(KycApplicant applicant) {
    _applicant = applicant;
    notifyListeners();
  }

  /// Pide (o renueva) el desafio. En fallo de red MANTIENE el desafio previo.
  Future<void> loadChallenge() async {
    if (_busy) return;
    _busy = true;
    _errorMessage = null;
    notifyListeners();
    try {
      final fresh = await _service.challenge();
      // Solo se reemplaza en exito: el estado previo sobrevive al error.
      _challenge = fresh;
      _challengeIssuedAt = DateTime.now();
      _lastError = null;
      _manualReviewFolio = null;
      _stepIndex = 0;
      _framesByTask.clear();
      _passedTasks.clear();
      _attemptsByTask.clear();
      _result = null;
    } on ApiException catch (e) {
      _lastError = KycErrorHandler.fromApiException(e);
      _errorMessage = e.message;
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  /// Captura UNA foto real del documento con la camara trasera (F-T23).
  ///
  /// Deja los bytes en [documentImage] (solo memoria). Sin camara cae al mock
  /// documentado ([generateMockFrames]) para no bloquear el flujo en CI. El
  /// permiso denegado expone el mensaje y permite reintentar con el boton.
  Future<void> captureDocument() async {
    if (_busy) return;
    _busy = true;
    _errorMessage = null;
    // Una captura nueva descarta la validacion anterior (evita mostrar issues
    // obsoletos mientras se recaptura).
    _documentValidation = null;
    _documentIssues = const [];
    _documentValidationError = null;
    notifyListeners();
    try {
      try {
        _documentImage = await _frameSource.captureDocumentFrame();
      } on KycCameraPermissionDenied catch (e) {
        _errorMessage = e.userMessage;
        return;
      } on KycCameraUnavailable {
        // Fallback documentado: sin camara se sigue con el mock en memoria.
        _documentImage =
            generateMockFrames(task: 'document', count: 1).first;
      }
    } on FormatException catch (e) {
      _errorMessage = 'La foto del documento no es valida: ${e.message}';
    } on ArgumentError catch (e) {
      _errorMessage = 'La foto del documento no es valida: ${e.message}';
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  /// Inicia la re-captura del documento (F-T27).
  ///
  /// Descarta de forma explicita la captura y la validacion previas (foto,
  /// `is_valid`, `issues` y error de transporte) y deja el flujo listo para
  /// volver a montar el preview en vivo y tomar una foto nueva. No toca la
  /// camara: el controller vigente se reutiliza al re-montar el preview.
  ///
  /// Se limpia la foto previa (documentado) para que [hasDocumentImage] vuelva
  /// a `false` y la pagina vuelva al estado "Capturar documento". Si la nueva
  /// captura falla (p. ej. permiso denegado) el usuario puede reintentar.
  void startDocumentRecapture() {
    if (_busy) return;
    _documentImage = null;
    _documentValidation = null;
    _documentIssues = const [];
    _documentValidationError = null;
    _errorMessage = null;
    notifyListeners();
  }

  /// Valida la foto capturada contra el servidor (E1-T30 / F-T26).
  ///
  /// Devuelve `true` SOLO si el servidor acepta el documento (`is_valid`).
  /// `is_valid=false` no es un error: deja los motivos en [documentIssues] y
  /// no avanza. Un fallo de red conserva [documentImage] y expone
  /// [documentValidationError] para reintentar la validacion sin recapturar.
  Future<bool> validateDocument() async {
    final image = _documentImage;
    if (image == null || _busy) return false;
    _busy = true;
    _validatingDocument = true;
    _documentValidationError = null;
    notifyListeners();
    try {
      final validator = _documentValidator;
      final KycDocumentValidation outcome;
      if (validator != null) {
        outcome = await validator(image);
      } else {
        final service = _evaluationService;
        // Tests legacy sin validador real: se preserva el avance directo.
        outcome = service == null
            ? const KycDocumentValidation(isValid: true)
            : await service.validateDocument(image: image);
      }
      _documentValidation = outcome;
      _documentIssues = outcome.issues;
      return outcome.isValid;
    } on ApiException catch (e) {
      // Fallo de transporte: no se pierde la captura; se puede reintentar.
      _lastError = KycErrorHandler.fromApiException(e);
      _documentValidation = null;
      _documentIssues = const [];
      _documentValidationError = e.message;
      return false;
    } finally {
      _busy = false;
      _validatingDocument = false;
      notifyListeners();
    }
  }

  /// Resuelve el resultado de [step] con la cadena de evaluadores (F-T23):
  /// detallado explicito > booleano explicito (tests legacy) > servicio real
  /// `evaluate` > mock que pasa (compatibilidad).
  Future<KycTaskEvaluation> _evaluate(
    String step,
    List<Uint8List> frames,
  ) async {
    final detailed = _detailedTaskEvaluator;
    if (detailed != null) return detailed(step, frames);
    final evaluator = _taskEvaluator;
    if (evaluator != null) {
      return KycTaskEvaluation(
        step: step,
        passed: await evaluator(step, frames),
      );
    }
    final service = _evaluationService;
    final token = _challenge?.token;
    if (service != null && token != null) {
      return service.evaluate(
        challengeToken: token,
        step: step,
        frames: frames,
      );
    }
    // Sin evaluador real configurado (tests legacy): se mantiene el mock.
    return KycTaskEvaluation(step: step, passed: true);
  }

  /// Captura frames de la tarea actual (fuente inyectada) y la resuelve:
  /// - `passed:true` -> avanza (o queda listo para submit si era la ultima).
  /// - `passed:false` -> MISMA tarea, crece [attemptsOf], mensaje de reintento.
  ///   Al agotar [maxAttemptsPerTask] se genera [manualReviewFolio] y
  ///   [lastError] pide derivación a revisión manual (E1-T06).
  /// - permiso de camara denegado -> expone el mensaje y conserva el paso;
  ///   el boton "Capturar" reintenta (no cuenta como intento de la tarea).
  /// - camara no disponible -> FALLBACK documentado al mock
  ///   ([generateMockFrames]); el E2E contra backend exige dispositivo fisico.
  /// - fallo de red -> conserva token, paso y frames; solo expone el error.
  /// - token expirado (401/EXPIRED) -> [lastError] pide challenge nuevo
  ///   ([KycErrorAction.refreshChallenge], E1-T06).
  Future<void> captureAndResolveCurrentTask() async {
    final step = currentStep;
    if (step == null || _busy) return;
    _busy = true;
    _taskPhase = KycTaskPhase.capturing;
    _errorMessage = null;
    notifyListeners();
    try {
      List<Uint8List>? frames;
      try {
        frames = await _frameSource.captureFramesForTask(step);
      } on KycCameraPermissionDenied catch (e) {
        _errorMessage = e.userMessage;
        return;
      } on KycCameraUnavailable {
        // Fallback documentado: sin camara se sigue con el mock en memoria.
        frames = generateMockFrames(task: step);
      }
      _framesByTask[step] = frames;
      _attemptsByTask[step] = attemptsOf(step) + 1;
      // F-T25: la rafaga ya termino; ahora el servidor evalua. Se notifica
      // ANTES de esperar para que el overlay cambie a "Verificando..." sin
      // ocultar el viewfinder.
      _taskPhase = KycTaskPhase.evaluating;
      notifyListeners();
      final evaluation = await _evaluate(step, frames);
      if (evaluation.passed) {
        _passedTasks.add(step);
        _lastError = null;
        if (!isLastStep) _stepIndex++;
      } else {
        // `passed:false` NO es error HTTP: es estado reintentable (E1-T06).
        // Se conserva el motivo real del servidor (F-T23) para mostrarlo.
        final info = KycErrorHandler.forTaskNotPassed(
          task: step,
          attempts: attemptsOf(step),
          maxAttempts: _maxAttemptsPerTask,
          serverReason: evaluation.reason,
          folio: _manualReviewFolio,
        );
        _lastError = info;
        if (info.action == KycErrorAction.manualReview) {
          _manualReviewFolio = info.folio;
        }
        _errorMessage = info.message;
      }
    } on ApiException catch (e) {
      final info = KycErrorHandler.fromApiException(
        e,
        task: step,
        attempts: attemptsOf(step),
        maxAttempts: _maxAttemptsPerTask,
        folio: _manualReviewFolio,
      );
      _lastError = info;
      if (info.action == KycErrorAction.manualReview) {
        _manualReviewFolio = info.folio;
      }
      _errorMessage = e.message;
    } finally {
      _busy = false;
      _taskPhase = KycTaskPhase.idle;
      notifyListeners();
    }
  }

  /// Envia documento + segmentos. En fallo de red conserva todo el estado.
  Future<void> submit() async {
    final current = _challenge;
    final applicant = _applicant;
    if (current == null || _busy || !readyToSubmit) return;
    if (applicant == null || !applicant.isComplete) {
      _errorMessage = 'Completa los datos del titular antes de enviar.';
      notifyListeners();
      return;
    }
    _busy = true;
    _errorMessage = null;
    notifyListeners();
    final framesByTask =
        Map<String, List<Uint8List>>.unmodifiable(_framesByTask);
    try {
      final service = _evaluationService;
      if (service != null) {
        // Produccion: envia `frames_b64` (todos) + `document.image_b64`.
        _result = await service.submitWithDocument(
          challengeToken: current.token,
          documentType: _documentType,
          documentNumber: _documentNumber,
          applicant: applicant,
          framesByTask: framesByTask,
          documentImage: _documentImage,
        );
      } else {
        _result = await _service.submit(
          challengeToken: current.token,
          documentType: _documentType,
          documentNumber: _documentNumber,
          applicant: applicant,
          framesByTask: framesByTask,
        );
      }
    } on ApiException catch (e) {
      final info = KycErrorHandler.fromApiException(e);
      _lastError = info;
      if (info.action == KycErrorAction.manualReview) {
        _manualReviewFolio = info.folio;
      }
      _errorMessage = e.message;
    } finally {
      // F-T33: el submit exitoso navega con `push('/kyc/result')`, así que el
      // `dispose` de la página NO corre; la sesión se libera aquí mismo.
      if (_result != null) {
        await releaseCamera();
      }
      _busy = false;
      notifyListeners();
    }
  }

  /// Renueva el desafío tras expirar el token conservando el progreso (E1-T06).
  ///
  /// Pide un challenge nuevo y conserva las tareas ya pasadas que existan en
  /// los pasos nuevos (con sus frames e intentos). Si el servidor emite pasos
  /// distintos, solo se conserva la intersección; si no hay coincidencia,
  /// equivale a un reinicio (documentado aquí y visible en que
  /// [taskPassed] queda vacío). En fallo de red conserva todo el estado.
  Future<void> refreshChallengePreservingProgress() async {
    if (_busy) return;
    _busy = true;
    _errorMessage = null;
    notifyListeners();
    try {
      final fresh = await _service.challenge();
      final newSteps = fresh.steps.toSet();
      _passedTasks.retainWhere(newSteps.contains);
      _framesByTask.removeWhere((task, _) => !newSteps.contains(task));
      _attemptsByTask.removeWhere((task, _) => !newSteps.contains(task));
      _challenge = fresh;
      _challengeIssuedAt = DateTime.now();
      _lastError = null;
      final pending = fresh.steps.indexWhere((s) => !_passedTasks.contains(s));
      _stepIndex = pending == -1 ? fresh.steps.length - 1 : pending;
    } on ApiException catch (e) {
      _lastError = KycErrorHandler.fromApiException(e);
      _errorMessage = e.message;
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  void clearError() {
    if (_errorMessage == null && _lastError == null) return;
    _errorMessage = null;
    _lastError = null;
    notifyListeners();
  }

  /// Punto único de teardown de la sesión de cámara (F-T33).
  ///
  /// Cierra la sesión vigente (`CameraFrameSource.closeSession`, con espera
  /// acotada H-03) SIN borrar el estado del flujo (`result`, tareas,
  /// documento): `/kyc/result` sigue leyendo `result` tras el submit, y el
  /// reintento (`reset()` + `/kyc`) reabre una sesión nueva al capturar (la
  /// fuente es reutilizable; ningún controller cerrado se reutiliza, F-T24).
  /// Con fuente mock es no-op. Nunca lanza (best-effort de recursos).
  Future<void> releaseCamera() async {
    final source = _frameSource;
    if (source is CameraFrameSource) {
      try {
        await source.closeSession();
      } catch (_) {
        // Best-effort: liberar la cámara nunca rompe el flujo ni la
        // navegación (el siguiente `open` reintenta de todos modos).
      }
    }
  }

  /// Reinicia el flujo (vuelve a empezar desde el tipo/número de documento).
  ///
  /// F-T33: también libera la sesión de cámara vigente (best-effort, sin
  /// await: `reset` es síncrono) para que el reintento no deje la sesión
  /// previa viva; la siguiente captura reabre una sesión nueva.
  void reset() {
    unawaited(releaseCamera());
    _challenge = null;
    _challengeIssuedAt = null;
    _lastError = null;
    _manualReviewFolio = null;
    _stepIndex = 0;
    _framesByTask.clear();
    _passedTasks.clear();
    _attemptsByTask.clear();
    _errorMessage = null;
    _result = null;
    _documentImage = null;
    _documentValidation = null;
    _documentIssues = const [];
    _documentValidationError = null;
    _validatingDocument = false;
    notifyListeners();
  }

  /// F-T33: al destruir el controlador se libera la sesión de cámara
  /// (best-effort, sin await: `dispose` es síncrono). No borra el estado.
  @override
  void dispose() {
    unawaited(releaseCamera());
    super.dispose();
  }
}
