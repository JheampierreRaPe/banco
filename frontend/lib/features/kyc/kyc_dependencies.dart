import 'dart:async';

import 'package:flutter/foundation.dart';

import 'camera_frame_source.dart';
import 'kyc_flow_controller.dart';
import 'kyc_frame_source.dart';
import 'kyc_service.dart';

/// Cableado del feature `kyc` (el feature NUNCA toca `core/router`).
///
/// El orquestador llama [configure] una vez (con `HttpKycService` sobre el
/// `ApiClient` compartido) antes de montar `...kycRoutes`. Las paginas usan
/// ese controlador compartido salvo que reciban uno por constructor (tests).
class KycDependencies {
  KycDependencies._();

  static KycService? _service;
  static KycEvaluationService? _evaluationService;
  static KycFrameSource? _frameSource;
  static KycFlowController? _controller;

  /// Configura el servicio real. Lo llama el orquestador (`app_router.dart`).
  /// [frameSource] es opcional: sin ella se usa el mock (tests); en el APK
  /// físico el orquestador inyecta `CameraFrameSource()` (fotos reales).
  ///
  /// [evaluationService] (F-T23) es opcional: por defecto se usa el propio
  /// [service] cuando implementa [KycEvaluationService] (es el caso de
  /// `HttpKycService`), de modo que `app_router.dart` no cambia.
  static void configure({
    required KycService service,
    KycFrameSource? frameSource,
    KycEvaluationService? evaluationService,
  }) {
    _service = service;
    // F-T33 (H-02): si se reemplaza la fuente, la anterior se cierra para no
    // dejar una sesión de cámara viva. Nunca se cierra la que se acaba de
    // inyectar (guardia `identical`).
    final previousSource = _frameSource;
    _frameSource = frameSource;
    // `KycService` y `KycEvaluationService` son interfaces no relacionadas:
    // Dart no promueve el tipo, por eso el cast explicito (seguro dentro del
    // `is`). En produccion `HttpKycService` implementa ambas.
    final inferred = service is KycEvaluationService
        ? service as KycEvaluationService
        : null;
    _evaluationService = evaluationService ?? inferred;
    _controller?.dispose();
    _controller = null;
    if (previousSource is CameraFrameSource &&
        !identical(previousSource, _frameSource)) {
      // Best-effort (sin await: `configure` es síncrono); idempotente con el
      // `dispose` del controlador, que ya liberó la sesión si la usaba.
      unawaited(previousSource.dispose());
    }
  }

  /// Controlador compartido del flujo (misma instancia entre `/kyc`,
  /// `/kyc/task` y `/kyc/result`).
  static KycFlowController get controller {
    final existing = _controller;
    if (existing != null) return existing;
    final service = _service;
    if (service == null) {
      throw StateError(
        'KycDependencies.configure(service:) debe llamarlo el orquestador '
        'antes de navegar a /kyc.',
      );
    }
    return _controller = KycFlowController(
      service: service,
      frameSource: _frameSource,
      evaluationService: _evaluationService,
    );
  }

  /// Controlador vigente SIN crearlo (las páginas lo usan en `dispose()` para
  /// liberar la cámara al salir sin instanciar un flujo nuevo).
  static KycFlowController? get controllerIfExists => _controller;

  @visibleForTesting
  static void debugReset() {
    _controller?.dispose();
    _controller = null;
    // F-T33 (H-02): el reset de tests también cierra la fuente anterior para
    // no dejar una sesión viva entre pruebas.
    final source = _frameSource;
    _frameSource = null;
    if (source is CameraFrameSource) {
      unawaited(source.dispose());
    }
    _service = null;
    _evaluationService = null;
  }
}
