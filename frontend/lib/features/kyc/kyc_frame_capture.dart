import 'dart:math';
import 'dart:typed_data';

/// Captura de frames del KYC: mock historico + seam de camara real RESUELTO.
///
/// Por defecto (y en todos los tests) se usan frames dummy en memoria via
/// [generateMockFrames]: nunca se escribe a disco ni a storage (docs/13
/// §1.2, docs/16 regla 7).
///
/// ```text
/// SEAM(camera) — RESUELTO con `CameraFrameSource`
/// (lib/features/kyc/camera_frame_source.dart, dependencia `camera`):
///   Fuente real: 1 foto por tarea a demanda con `takePicture()`
///   -> `XFile.readAsBytes()` (Uint8List en memoria; el temporal en cache
///   se borra de inmediato, sin guardar en galeria).
///   Lente: frontal por defecto (liveness/selfie), trasera para documento
///   (ver `preferredLensForTask` en kyc_frame_source.dart).
///   Inyeccion (sin tocar pantallas): el orquestador construye
///     KycFlowController(service: s, frameSource: CameraFrameSource())
///   Permiso denegado -> mensaje + reintentar (boton "Capturar").
///   Camara no disponible -> fallback al mock (documentado en el
///   controlador). E2E con camara exige dispositivo fisico.
/// ```
///
/// Los bytes dummy llevan el encabezado magico PNG para parecerse a una
/// imagen; NO pasan una validacion real de imagen del backend. Para el E2E
/// contra el backend se necesita la camara real (ver SEAM arriba).
/// Numero de frames por tarea (docs/13 §1.4: 8-10 por paso).
const int kMockFramesPerTask = 8;

/// Genera [count] frames dummy deterministas para [task], SOLO en memoria.
List<Uint8List> generateMockFrames({
  required String task,
  int count = kMockFramesPerTask,
}) {
  final random = Random(task.hashCode);
  return List<Uint8List>.generate(count, (i) {
    // Prefijo magico PNG + relleno pseudoaleatorio determinista.
    final filler = List<int>.generate(120, (_) => random.nextInt(256));
    return Uint8List.fromList([
      0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, // \x89PNG\r\n\x1a\n
      i & 0xFF,
      ...filler,
    ]);
  }, growable: false);
}
