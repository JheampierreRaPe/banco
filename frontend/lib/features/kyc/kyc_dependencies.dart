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
  static KycDocumentLookupService? _lookupService;
  static KycEmailCheckService? _emailCheckService;
  static KycFrameSource? _frameSource;
  static KycFlowController? _controller;

  /// Configura el servicio real. Lo llama el orquestador (`app_router.dart`).
  /// [frameSource] es opcional: sin ella se usa el mock (tests); en el APK
  /// físico el orquestador inyecta `CameraFrameSource()` (fotos reales).
  ///
  /// [evaluationService] (F-T23) es opcional: por defecto se usa el propio
  /// [service] cuando implementa [KycEvaluationService] (es el caso de
  /// `HttpKycService`), de modo que `app_router.dart` no cambia. Lo mismo
  /// vale para [lookupService] (F-T44, `KycDocumentLookupService`).
  static void configure({
    required KycService service,
    KycFrameSource? frameSource,
    KycEvaluationService? evaluationService,
    KycDocumentLookupService? lookupService,
    KycEmailCheckService? emailCheckService,
  }) {
    _service = service;
    // F-T33 (H-02): si se reemplaza la fuente, la anterior se cierra para no
    // dejar una sesión de cámara viva. Nunca se cierra la que se acaba de
    // inyectar (guardia `identical`).
    final previousSource = _frameSource;
    _frameSource = frameSource;
    // `KycService` y `KycEvaluationService` son interfaces no relacionadas:
    // Dart no promueve el tipo, por eso el cast explicito (seguro dentro del
    // `is`). En produccion `HttpKycService` implementa ambas (F-T44: tambien
    // `KycDocumentLookupService`).
    final inferred = service is KycEvaluationService
        ? service as KycEvaluationService
        : null;
    _evaluationService = evaluationService ?? inferred;
    final inferredLookup = service is KycDocumentLookupService
        ? service as KycDocumentLookupService
        : null;
    _lookupService = lookupService ?? inferredLookup;
    final inferredEmailCheck = service is KycEmailCheckService
        ? service as KycEmailCheckService
        : null;
    _emailCheckService = emailCheckService ?? inferredEmailCheck;
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

  /// Servicio de consulta del titular por documento (F-T44). Por defecto el
  /// propio `service` cuando implementa [KycDocumentLookupService] (es el
  /// caso de `HttpKycService`).
  static KycDocumentLookupService get lookupService {
    final existing = _lookupService;
    if (existing != null) return existing;
    throw StateError(
      'KycDependencies.configure(service:) debe llamarlo el orquestador '
      'antes de validar un documento.',
    );
  }

  /// Servicio de prechequeo de email (F-T50, E1-T40). Por defecto el
  /// propio `service` cuando implementa [KycEmailCheckService] (es el
  /// caso de `HttpKycService`).
  static KycEmailCheckService get emailCheckService {
    final existing = _emailCheckService;
    if (existing != null) return existing;
    throw StateError(
      'KycDependencies.configure(service:) debe llamarlo el orquestador '
      'antes de prechequear un email.',
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
    _lookupService = null;
    _emailCheckService = null;
  }
}
