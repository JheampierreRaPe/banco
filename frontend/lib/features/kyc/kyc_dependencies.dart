import 'package:flutter/foundation.dart';

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
  static KycFrameSource? _frameSource;
  static KycFlowController? _controller;

  /// Configura el servicio real. Lo llama el orquestador (`app_router.dart`).
  /// [frameSource] es opcional: sin ella se usa el mock (tests); en el APK
  /// físico el orquestador inyecta `CameraFrameSource()` (fotos reales).
  static void configure({required KycService service, KycFrameSource? frameSource}) {
    _service = service;
    _frameSource = frameSource;
    _controller?.dispose();
    _controller = null;
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
    return _controller =
        KycFlowController(service: service, frameSource: _frameSource);
  }

  @visibleForTesting
  static void debugReset() {
    _controller?.dispose();
    _controller = null;
    _service = null;
  }
}
