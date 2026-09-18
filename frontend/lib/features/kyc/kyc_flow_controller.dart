import 'package:flutter/foundation.dart';

import '../../core/errors/api_exception.dart';
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
    KycTaskEvaluator? taskEvaluator,
    KycFrameSource? frameSource,
    this._maxAttemptsPerTask = KycErrorHandler.defaultMaxAttemptsPerTask,
  })  : _taskEvaluator = taskEvaluator ?? _mockPassEvaluator,
        _frameSource = frameSource ?? MockKycFrameSource();

  static Future<bool> _mockPassEvaluator(
    String task,
    List<Uint8List> frames,
  ) async =>
      true;

  final KycService _service;
  final KycTaskEvaluator _taskEvaluator;

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
  int _stepIndex = 0;
  final Map<String, List<Uint8List>> _framesByTask = {};
  final Set<String> _passedTasks = {};
  final Map<String, int> _attemptsByTask = {};
  bool _busy = false;
  String? _errorMessage;
  KycSubmitResult? _result;

  KycChallenge? get challenge => _challenge;
  String? get token => _challenge?.token;
  List<String> get steps => _challenge?.steps ?? const [];
  String get documentType => _documentType;
  String get documentNumber => _documentNumber;
  int get currentStepIndex => _stepIndex;

  String? get currentStep {
    final s = steps;
    if (s.isEmpty) return null;
    return s[_stepIndex.clamp(0, s.length - 1)];
  }

  bool get isLastStep => steps.isNotEmpty && _stepIndex >= steps.length - 1;
  bool get busy => _busy;
  String? get errorMessage => _errorMessage;
  KycSubmitResult? get result => _result;

  /// `true` cuando TODAS las tareas del servidor pasaron (listo para submit).
  bool get readyToSubmit =>
      steps.isNotEmpty && steps.every(_passedTasks.contains);

  int attemptsOf(String task) => _attemptsByTask[task] ?? 0;
  bool taskPassed(String task) => _passedTasks.contains(task);
  int framesCountOf(String task) => _framesByTask[task]?.length ?? 0;

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
      final passed = await _taskEvaluator(step, frames);
      if (passed) {
        _passedTasks.add(step);
        _lastError = null;
        if (!isLastStep) _stepIndex++;
      } else {
        // `passed:false` NO es error HTTP: es estado reintentable (E1-T06).
        final info = KycErrorHandler.forTaskNotPassed(
          task: step,
          attempts: attemptsOf(step),
          maxAttempts: _maxAttemptsPerTask,
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
      notifyListeners();
    }
  }

  /// Envia documento + segmentos. En fallo de red conserva todo el estado.
  Future<void> submit() async {
    final current = _challenge;
    if (current == null || _busy || !readyToSubmit) return;
    _busy = true;
    _errorMessage = null;
    notifyListeners();
    try {
      _result = await _service.submit(
        challengeToken: current.token,
        documentType: _documentType,
        documentNumber: _documentNumber,
        framesByTask: Map<String, List<Uint8List>>.unmodifiable(_framesByTask),
      );
    } on ApiException catch (e) {
      final info = KycErrorHandler.fromApiException(e);
      _lastError = info;
      if (info.action == KycErrorAction.manualReview) {
        _manualReviewFolio = info.folio;
      }
      _errorMessage = e.message;
    } finally {
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

  /// Reinicia el flujo (vuelve a empezar desde el tipo/número de documento).
  void reset() {
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
    notifyListeners();
  }
}
