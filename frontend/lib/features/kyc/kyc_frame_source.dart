import 'dart:convert';
import 'dart:typed_data';

import 'kyc_frame_capture.dart';

/// Lente logica de la camara, sin depender del plugin `camera`.
///
/// Se modela aqui (Dart puro) para que la eleccion de lente y las
/// validaciones sean testeables en CI, donde no hay camara fisica.
/// La traduccion a [CameraLensDirection] vive en `camera_frame_source.dart`.
enum KycCameraLens {
  /// Frontal: selfies y pruebas de vida (liveness).
  front,

  /// Trasera: captura de documentos (mas resolucion y enfoque).
  back,
}

/// `true` si [task] es captura de documento (requiere camara trasera).
///
/// Heuristica sobre el nombre del paso que impone el servidor: cualquier
/// tarea con `document`, `doc_*`, `dni`, `passport`/`pasaporte`, `id_card`
/// o `cedula` se considera documento. Todo lo demas (front, blink, smile,
/// turn_*, nod, ...) es liveness y usa la frontal.
bool isDocumentTask(String task) {
  final t = task.toLowerCase();
  return t.contains('document') ||
      t == 'doc' ||
      t.contains('doc_') ||
      t.contains('dni') ||
      t.contains('passport') ||
      t.contains('pasaporte') ||
      t.contains('id_card') ||
      t.contains('cedula');
}

/// Lente recomendada para [task]: trasera para documento, frontal (defecto)
/// para liveness.
///
/// Eleccion documentada: el liveness necesita ver la cara del usuario
/// (selfie) mientras que el documento se apoya en una superficie y se
/// fotografia con la trasera, de mayor calidad optica.
KycCameraLens preferredLensForTask(String task) =>
    isDocumentTask(task) ? KycCameraLens.back : KycCameraLens.front;

/// Fuente de frames del flujo KYC (seam E1-T05 resuelto).
///
/// Las implementaciones entregan bytes de imagen EN MEMORIA; nunca escriben
/// a disco ni a galeria (docs/13 §1.2, docs/16 regla 7: sin PII en logs).
/// El [KycFlowController] recibe la fuente por constructor; por defecto usa
/// [MockKycFrameSource] para que los tests existentes sigan verdes.
abstract class KycFrameSource {
  /// Captura a demanda los frames de [task] (foto por tarea, NO video).
  Future<List<Uint8List>> captureFramesForTask(String task);

  /// Libera recursos (controlador de camara, si aplica).
  Future<void> dispose();
}

/// Fuente simulada historica: ~8 frames dummy en memoria por tarea.
///
/// Es la fuente por defecto en tests y el fallback documentado cuando la
/// camara no esta disponible en el dispositivo.
class MockKycFrameSource implements KycFrameSource {
  @override
  Future<List<Uint8List>> captureFramesForTask(String task) async =>
      generateMockFrames(task: task);

  @override
  Future<void> dispose() async {}
}

/// El usuario denego el permiso de camara: reintentable.
///
/// El controlador expone [userMessage] y conserva el paso actual para que el
/// boton "Capturar" sirva como reintentar (tras otorgar el permiso).
class KycCameraPermissionDenied implements Exception {
  KycCameraPermissionDenied([
    this.userMessage =
        'Permiso de cámara denegado. Otórgalo en los ajustes del sistema '
            'y pulsa «Capturar» para reintentar.',
  ]);

  final String userMessage;

  @override
  String toString() => 'KycCameraPermissionDenied: $userMessage';
}

/// Camara no disponible (sin camaras, fallo de inicializacion o de captura).
///
/// NO es reintentable contra la camara: el controlador hace fallback al mock
/// ([generateMockFrames]) y lo documenta en el estado. El E2E contra backend
/// exige dispositivo fisico con camara.
class KycCameraUnavailable implements Exception {
  KycCameraUnavailable([
    this.detail = 'Cámara no disponible en este dispositivo.',
  ]);

  final String detail;

  @override
  String toString() => 'KycCameraUnavailable: $detail';
}

/// Tamaño maximo aceptado por frame en memoria (5 MB).
///
/// Cota de seguridad antes del submit; la foto real con
/// `ResolutionPreset.medium` queda muy por debajo (~100-300 KB JPEG).
const int kMaxFrameBytes = 5 * 1024 * 1024;

/// `true` si [bytes] parece JPEG o PNG por magic bytes.
///
/// Espeja la validacion del backend (verificado: JPEG `FF D8 FF`,
/// PNG `89 50 4E 47`). Los frames mock historicos llevan encabezado PNG,
/// por eso tambien devuelven `true`.
bool isSupportedImageBytes(Uint8List bytes) {
  if (bytes.length < 4) return false;
  final isJpeg = bytes[0] == 0xFF && bytes[1] == 0xD8 && bytes[2] == 0xFF;
  final isPng =
      bytes[0] == 0x89 &&
      bytes[1] == 0x50 &&
      bytes[2] == 0x4E &&
      bytes[3] == 0x47;
  return isJpeg || isPng;
}

/// Valida un frame en memoria antes de aceptarlo al flujo.
///
/// Lanza [ArgumentError] si esta vacio o excede [maxBytes], y
/// [FormatException] si no es JPEG/PNG por magic bytes.
void validateFrameBytes(Uint8List bytes, {int maxBytes = kMaxFrameBytes}) {
  if (bytes.isEmpty) {
    throw ArgumentError.value(bytes, 'bytes', 'Frame vacío: sin bytes.');
  }
  if (bytes.length > maxBytes) {
    throw ArgumentError.value(
      bytes.length,
      'bytes.length',
      'Frame de ${bytes.length} bytes excede el máximo de $maxBytes.',
    );
  }
  if (!isSupportedImageBytes(bytes)) {
    throw const FormatException(
      'Frame sin magic bytes JPEG/PNG; no pasaría la validación del backend.',
    );
  }
}

/// Codifica un frame validado a base64 (formato que espera `KycService`).
///
/// El submit (`HttpKycService.submit`) ya codifica con `base64Encode`; este
/// helper existe para la fuente real y para tests de la conversion.
String encodeFrameToBase64(Uint8List bytes) {
  validateFrameBytes(bytes);
  return base64Encode(bytes);
}
