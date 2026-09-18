import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:camera/camera.dart';

import 'kyc_frame_source.dart';

/// Fuente REAL de frames con la camara del dispositivo (resuelve el seam
/// E1-T05 de `kyc_frame_capture.dart`).
///
/// - Una foto por tarea a demanda (`takePicture()`), NO video continuo.
/// - Lente: frontal por defecto para liveness, trasera para documento (ver
///   [preferredLensForTask]); si el dispositivo no tiene la lente pedida se
///   usa la primera disponible.
/// - Tamaño razonable sin dependencia extra: `ResolutionPreset.medium`
///   (~720p, lado mayor < 1280px, JPEG de ~100-300 KB), por eso NO se agrega
///   el package `image` para comprimir/redimensionar (ver reporte §6).
/// - Solo memoria hasta el submit: los bytes se leen con
///   `XFile.readAsBytes()` y el temporal que `takePicture()` deja en cache
///   se borra de inmediato (best-effort); jamas se guarda en galeria.
/// - Permiso denegado -> [KycCameraPermissionDenied] (mensaje + reintentar).
/// - Camara no disponible -> [KycCameraUnavailable]; el controlador hace
///   fallback al mock (documentado en `KycFlowController`).
/// - Timeout anti-bloqueo: [openTimeout] (~15 s) acota `availableCameras()` e
///   `initialize()`; si no resuelven a tiempo se lanza [KycCameraUnavailable]
///   con mensaje claro para que el preview muestre el placeholder mock en
///   vez de un spinner infinito.
/// - CUALQUIER excepción no-[CameraException] (p. ej. `PlatformException`,
///   `MissingPluginException` en emuladores/CI) se envuelve en
///   [KycCameraUnavailable]: nunca escapan errores crudos al preview.
///
/// Permiso Android (verificado, sin `permission_handler`):
/// - `AndroidManifest.xml` declara `android.permission.CAMERA`.
/// - El plugin `camera` DELEGA el permiso al SO: el runtime dialog lo dispara
///   `controller.initialize()` (no hay llamada previa de permiso en este
///   archivo por diseño del plugin). Si el usuario niega, `initialize()` /
///   `takePicture()` lanzan `CameraException(code: 'CameraAccessDenied')`,
///   que aquí se mapea a [KycCameraPermissionDenied].
/// - Comportamiento esperado: primera vez → dialog del sistema → si concede,
///   preview en vivo; si niega, mensaje + reintentar (el reintento vuelve a
///   llamar a `initialize()`, que re-dispara el dialog mientras el SO lo
///   permita; si marcó "no volver a preguntar", el mensaje indica ir a los
///   ajustes del sistema).
/// - PENDIENTE (deliberado, no agregar ahora): `permission_handler` para
///   chequear `Permission.camera.status` ANTES de inicializar y abrir
///   ajustes con `openAppSettings()`. Por qué no se agrega: el flujo actual
///   ya es funcional solo con `camera` (el plugin pide el permiso), añadir
///   el package suma superficie de permisos/mantenimiento sin ser necesario
///   para corregir el spinner infinito; se retomará solo si UX exige
///   pre-chequeo o deep-link a ajustes.
///
/// No instanciar en tests/CI: no hay camara fisica. El E2E exige dispositivo
/// fisico (ver reporte §6).
class CameraFrameSource implements KycFrameSource {
  CameraFrameSource({this.resolution = ResolutionPreset.medium});

  /// Resolucion de captura; `medium` mantiene el lado mayor ~720px (< 1280px
  /// exigidos) sin post-procesado.
  final ResolutionPreset resolution;

  /// Cota anti-bloqueo para `availableCameras()` e `initialize()`.
  ///
  /// 15 s: suficiente para cámaras físicas lentas, corta para no dejar al
  /// usuario en "Iniciando cámara…" para siempre. Visible como campo (no
  /// `const`) para que los tests puedan inyectar una cota corta.
  Duration openTimeout = const Duration(seconds: 15);

  CameraController? _controller;
  KycCameraLens? _activeLens;
  bool _disposed = false;

  /// `true` cuando hay un controlador inicializado y listo.
  bool get isInitialized => _controller?.value.isInitialized ?? false;

  /// Controlador en vivo para el viewfinder ([CameraPreview]).
  ///
  /// ADITIVO viewfinder: expone el MISMO [CameraController] que usa
  /// [captureFramesForTask] (no se crea un segundo controller). `null` si
  /// aún no está inicializado; la página usa [previewControllerForTask]
  /// para inicializarlo por tarea y mostrar estados.
  CameraController? get previewController =>
      isInitialized ? _controller : null;

  Future<void> _ensureInitialized(KycCameraLens lens) async {
    final current = _controller;
    if (current != null && _activeLens == lens && current.value.isInitialized) {
      return;
    }
    await _open(lens);
  }

  Future<void> _open(KycCameraLens lens) async {
    await _controller?.dispose();
    _controller = null;
    _activeLens = null;

    late final List<CameraDescription> cameras;
    try {
      cameras = await availableCameras().timeout(
        openTimeout,
        onTimeout: () => throw KycCameraUnavailable(
          'La cámara tardó demasiado en responder '
          '(${openTimeout.inSeconds} s). Se usará captura simulada.',
        ),
      );
    } on KycCameraUnavailable {
      rethrow;
    } on CameraException catch (e) {
      throw KycCameraUnavailable(
        'No se pudieron enumerar las cámaras (${e.code}).',
      );
    } catch (e) {
      // PlatformException / MissingPluginException / cualquier error crudo
      // (típico en emuladores sin cámara): nunca escapa crudo al preview.
      throw KycCameraUnavailable(
        'No se pudieron enumerar las cámaras (${e.runtimeType}).',
      );
    }
    if (cameras.isEmpty) {
      throw KycCameraUnavailable('El dispositivo no tiene cámaras.');
    }
    final want = lens == KycCameraLens.front
        ? CameraLensDirection.front
        : CameraLensDirection.back;
    final description = cameras.firstWhere(
      (c) => c.lensDirection == want,
      orElse: () => cameras.first,
    );

    final controller = CameraController(
      description,
      resolution,
      enableAudio: false,
    );
    try {
      await controller.initialize().timeout(
            openTimeout,
            onTimeout: () => throw KycCameraUnavailable(
              'La cámara tardó demasiado en inicializarse '
              '(${openTimeout.inSeconds} s). Se usará captura simulada.',
            ),
          );
    } on KycCameraUnavailable {
      await controller.dispose();
      rethrow;
    } on CameraException catch (e) {
      await controller.dispose();
      if (e.code == 'CameraAccessDenied') {
        throw KycCameraPermissionDenied();
      }
      throw KycCameraUnavailable(
        'No se pudo inicializar la cámara (${e.code}).',
      );
    } catch (e) {
      // initialize() colgado que resolvió con error no-CameraException.
      await controller.dispose();
      throw KycCameraUnavailable(
        'No se pudo inicializar la cámara (${e.runtimeType}).',
      );
    }
    _controller = controller;
    _activeLens = lens;
  }

  /// Inicializa (si hace falta) y devuelve el controller en vivo para
  /// la lente de [task]. REUTILIZA el mismo controller de la captura.
  ///
  /// Lanza [KycCameraPermissionDenied] / [KycCameraUnavailable] para que la
  /// página muestre el estado correspondiente (permiso denegado /
  /// no disponible → fallback mock documentado).
  Future<CameraController> previewControllerForTask(String task) async {
    if (_disposed) {
      throw StateError('CameraFrameSource ya fue disposed.');
    }
    await _ensureInitialized(preferredLensForTask(task));
    final controller = _controller;
    if (controller == null || !controller.value.isInitialized) {
      throw KycCameraUnavailable('La cámara no quedó inicializada.');
    }
    return controller;
  }

  @override
  Future<List<Uint8List>> captureFramesForTask(String task) async {    if (_disposed) {
      throw StateError('CameraFrameSource ya fue disposed.');
    }
    await _ensureInitialized(preferredLensForTask(task));
    final controller = _controller!;

    late final XFile photo;
    try {
      photo = await controller.takePicture();
    } on CameraException catch (e) {
      if (e.code == 'CameraAccessDenied') {
        throw KycCameraPermissionDenied();
      }
      throw KycCameraUnavailable('Falló la captura de foto (${e.code}).');
    } catch (e) {
      throw KycCameraUnavailable(
        'Falló la captura de foto (${e.runtimeType}).',
      );
    }
    try {
      final bytes = await photo.readAsBytes();
      validateFrameBytes(bytes);
      // Un solo frame real por tarea: el submit usa el primero de la lista.
      return [bytes];
    } finally {
      // `takePicture()` deja un temporal en el cache de la app: se borra de
      // inmediato para cumplir "solo memoria hasta el submit". Best-effort:
      // si falla, el SO purga el cache; se marca como manejado.
      File(photo.path).delete().ignore();
    }
  }

  @override
  Future<void> dispose() async {
    _disposed = true;
    await _controller?.dispose();
    _controller = null;
    _activeLens = null;
  }
}
