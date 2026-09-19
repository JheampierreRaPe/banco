import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:camera/camera.dart';

import 'kyc_frame_source.dart';

/// Fuente REAL de frames con la camara del dispositivo (resuelve el seam
/// E1-T05 de `kyc_frame_capture.dart`).
///
/// - Rafaga por tarea (F-T23): 10-15 fotos con intervalos de ~150-200 ms
///   (~2.5 s), a demanda (`takePicture()`), NO video continuo. El conteo y el
///   intervalo son inyectables ([burstFrames]/[frameInterval]/[burstDelay])
///   para tests sin camara ni tiempo real.
/// - Documento (F-T23): [captureDocumentFrame] toma UNA foto con la lente
///   trasera antes de las tareas de liveness.
/// - Lente: frontal por defecto para liveness, trasera para documento (ver
///   [preferredLensForTask]); si el dispositivo no tiene la lente pedida se
///   usa la primera disponible.
/// - Resolucion por lente (F-T27): el liveness usa `medium` (~480p) mientras
///   que el DOCUMENTO usa [documentResolution] (`high`, 720p) para que en
///   retrato el ancho supere los 600 px que exige el microservicio. Ambos
///   presets son inyectables; el JPEG resultante sigue muy por debajo de
///   [kMaxFrameBytes] (5 MB), por eso NO se agrega el package `image` para
///   comprimir/redimensionar (ver reporte §6).
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
/// Fabrica de [CameraController] inyectable en tests (F-T24).
///
/// En produccion es `null` y la fuente construye un `CameraController` real;
/// en tests se inyecta un doble que registra `takePicture`/`dispose` y falla
/// si se le usa despues de `dispose`, reproduciendo el defecto sin camara.
typedef KycCameraControllerFactory = CameraController Function(
  CameraDescription description,
  ResolutionPreset resolution,
);

class CameraFrameSource implements KycFrameSource {
  CameraFrameSource({
    this.resolution = ResolutionPreset.medium,
    this.documentResolution = ResolutionPreset.high,
    this.burstFrames = kKycBurstFrames,
    this.frameInterval = kKycFrameInterval,
    KycBurstDelay? burstDelay,
    this.camerasLoader,
    this.controllerFactory,
  }) : burstDelay = burstDelay ?? defaultBurstDelay;

  /// Resolucion de captura de LIVENESS (lente frontal); `medium` (~480p).
  final ResolutionPreset resolution;

  /// Resolucion de captura del DOCUMENTO (lente trasera, F-T27).
  ///
  /// `high` (720p) por defecto: en retrato el lado corto queda `>=600` px,
  /// cubriendo el minimo que exige el microservicio (`600x400`). Inyectable
  /// para tests; el liveness conserva [resolution] (`medium`).
  final ResolutionPreset documentResolution;

  /// Cantidad de frames de la ráfaga por tarea (10-15; default 12).
  final int burstFrames;

  /// Intervalo entre fotos de la ráfaga (~150-200 ms; default 180 ms).
  final Duration frameInterval;

  /// Espera entre frames; inyectable en tests para no usar tiempo real.
  final KycBurstDelay burstDelay;

  /// Cota anti-bloqueo para `availableCameras()` e `initialize()`.
  ///
  /// 15 s: suficiente para cámaras físicas lentas, corta para no dejar al
  /// usuario en "Iniciando cámara…" para siempre. Visible como campo (no
  /// `const`) para que los tests puedan inyectar una cota corta.
  Duration openTimeout = const Duration(seconds: 15);

  /// Enumerador de cámaras inyectable en tests (F-T24); `null` = real.
  final Future<List<CameraDescription>> Function()? camerasLoader;

  /// Fábrica de controladores inyectable en tests (F-T24); `null` = real.
  final KycCameraControllerFactory? controllerFactory;

  CameraController? _controller;
  KycCameraLens? _activeLens;
  bool _disposed = false;

  /// Token de generación: se incrementa cada vez que se publica un controller
  /// nuevo. Los consumidores (preview/rafaga) quedan ligados a una generación;
  /// un consumidor de una generación vieja NUNCA toca un controller cerrado:
  /// o lo reutiliza (misma lente) o el cierre queda diferido hasta que no haya
  /// capturas en curso.
  int _generation = 0;
  int get generation => _generation;

  /// Mutex de apertura: serializa [_open] para que dos llamadas concurrentes
  /// (p. ej. `initState` del preview + `Capturar`) no creen dos controllers ni
  /// disposeen el vigente.
  Future<void> _openChain = Future<void>.value();

  /// Capturas (rafaga/documento) en curso. Mientras haya alguna no se cierra el
  /// controller que las atiende: se retira y se cierra al drenar.
  int _activeCaptures = 0;

  /// Controllers retirados pero aún no cerrados (captura en curso), y set de
  /// todos los ya cerrados, para no volver a tocarlos ni cerrarlos dos veces.
  final List<CameraController> _retired = <CameraController>[];
  final Set<CameraController> _closed = <CameraController>{};

  /// `true` cuando hay un controlador inicializado, vigente y no cerrado.
  bool get isInitialized {
    final controller = _controller;
    return controller != null && _isUsable(controller);
  }

  /// Controlador en vivo para el viewfinder ([CameraPreview]).
  ///
  /// ADITIVO viewfinder: expone el MISMO [CameraController] que usa
  /// [captureFramesForTask] (no se crea un segundo controller). `null` si
  /// aún no está inicializado; la página usa [previewControllerForTask]
  /// para inicializarlo por tarea y mostrar estados.
  CameraController? get previewController =>
      isInitialized ? _controller : null;

  /// `true` si [controller] sigue vigente, inicializado y sin cerrar.
  bool _isUsable(CameraController controller) =>
      !_closed.contains(controller) && controller.value.isInitialized;

  /// `true` si [controller] es el controller vigente de la fuente.
  ///
  /// El preview lo usa para no renderizar ([CameraPreview]) un controller que
  /// ya fue reemplazado/cerrado por un cambio de lente (F-T24).
  bool isControllerCurrent(CameraController controller) =>
      identical(_controller, controller) && _isUsable(controller);

  /// Inicializa (si hace falta) la lente [lens] de forma serializada.
  ///
  /// Reutiliza el controller vigente si ya está en la lente pedida; si no,
  /// encola una apertura en [_openChain] (nunca dos `_open` a la vez).
  Future<void> _ensureInitialized(KycCameraLens lens) {
    final current = _controller;
    if (current != null && _activeLens == lens && _isUsable(current)) {
      return Future<void>.value();
    }
    return _serializeOpen(lens);
  }

  Future<void> _serializeOpen(KycCameraLens lens) {
    final next = _openChain.then((_) => _open(lens));
    // El encadenado sobrevive a fallos de una apertura previa.
    _openChain = next.then<void>((_) {}, onError: (_) {});
    return next;
  }

  /// Apertura real, siempre bajo el mutex [_openChain].
  ///
  /// NO disposa el controller vigente al inicio; abre el nuevo y solo despues
  /// retira el anterior (diferido si hay capturas en curso). Así el preview o
  /// la rafaga que todavía lo usan no quedan apuntando a un disposed.
  Future<void> _open(KycCameraLens lens) async {
    if (_disposed) {
      throw KycCameraUnavailable(
        'La fuente de cámara ya fue cerrada.',
      );
    }
    // Revalidar DENTRO del mutex: otra apertura pudo dejarla lista.
    final current = _controller;
    if (current != null && _activeLens == lens && _isUsable(current)) {
      return;
    }

    final cameras = await _loadCameras();
    final want = lens == KycCameraLens.front
        ? CameraLensDirection.front
        : CameraLensDirection.back;
    final description = cameras.firstWhere(
      (c) => c.lensDirection == want,
      orElse: () => cameras.first,
    );

    final factory = controllerFactory;
    // F-T27: el documento (lente trasera) captura en alta resolucion; el
    // liveness conserva `medium`. El controller sigue siendo compartido por
    // lente (F-T24): al cambiar de lente se recrea solo una vez y el anterior
    // se retira de forma diferida.
    final preset =
        lens == KycCameraLens.back ? documentResolution : resolution;
    final controller = factory != null
        ? factory(description, preset)
        : CameraController(description, preset, enableAudio: false);
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

    if (_disposed) {
      // La fuente se cerró mientras se abría: no publicar.
      await controller.dispose();
      throw KycCameraUnavailable('La fuente de cámara ya fue cerrada.');
    }

    final previous = _controller;
    _controller = controller;
    _activeLens = lens;
    _generation++;
    if (previous != null && !identical(previous, controller)) {
      _retire(previous);
    }
  }

  Future<List<CameraDescription>> _loadCameras() async {
    late final List<CameraDescription> cameras;
    try {
      final loader = camerasLoader;
      final future = loader != null ? loader() : availableCameras();
      cameras = await future.timeout(
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
    return cameras;
  }

  /// Retira [controller]: lo cierra ya si no hay capturas, o lo difiere al
  /// drenar la última captura en curso (nunca se cierra en uso).
  void _retire(CameraController controller) {
    if (_closed.contains(controller)) return;
    if (_activeCaptures > 0) {
      if (!_retired.contains(controller)) _retired.add(controller);
      return;
    }
    unawaited(_close(controller));
  }

  Future<void> _close(CameraController controller) async {
    if (!_closed.add(controller)) return;
    _retired.remove(controller);
    await controller.dispose();
  }

  /// Cierra los controllers retirados cuando ya no hay capturas en curso.
  void _drainRetired() {
    if (_activeCaptures > 0) return;
    for (final controller in List<CameraController>.of(_retired)) {
      unawaited(_close(controller));
    }
  }

  /// Inicializa (si hace falta) y devuelve el controller en vivo para
  /// la lente de [task]. REUTILIZA el mismo controller de la captura.
  ///
  /// Lanza [KycCameraPermissionDenied] / [KycCameraUnavailable] para que la
  /// página muestre el estado correspondiente (permiso denegado /
  /// no disponible → fallback mock documentado). Si la fuente ya fue
  /// disposeada lanza [KycCameraUnavailable] (error tipado de KYC).
  Future<CameraController> previewControllerForTask(String task) async {
    if (_disposed) {
      throw KycCameraUnavailable('La fuente de cámara ya fue cerrada.');
    }
    await _ensureInitialized(preferredLensForTask(task));
    final controller = _controller;
    if (controller == null || !_isUsable(controller)) {
      throw KycCameraUnavailable('La cámara no quedó inicializada.');
    }
    return controller;
  }

  @override
  Future<List<Uint8List>> captureFramesForTask(String task) async {
    if (_disposed) {
      throw KycCameraUnavailable('La fuente de cámara ya fue cerrada.');
    }
    await _ensureInitialized(preferredLensForTask(task));
    final controller = _controller;
    if (controller == null || !_isUsable(controller)) {
      throw KycCameraUnavailable('La cámara no quedó inicializada.');
    }
    // La rafaga queda ligada a [controller]; aunque el preview cambie de
    // generacion, la captura en curso lo mantiene vivo hasta terminar.
    _activeCaptures++;
    try {
      // Ráfaga inyectable: `captureBurstFrames` valida cada frame y
      // [burstDelay] permite tests sin tiempo real.
      return await captureBurstFrames(
        captureOne: () => _takeSinglePicture(controller),
        frames: burstFrames,
        interval: frameInterval,
        delay: burstDelay,
      );
    } finally {
      _activeCaptures--;
      _drainRetired();
    }
  }

  @override
  Future<Uint8List> captureDocumentFrame() async {
    if (_disposed) {
      throw KycCameraUnavailable('La fuente de cámara ya fue cerrada.');
    }
    // Lente trasera explícita para el documento.
    await _ensureInitialized(KycCameraLens.back);
    final controller = _controller;
    if (controller == null || !_isUsable(controller)) {
      throw KycCameraUnavailable('La cámara no quedó inicializada.');
    }
    _activeCaptures++;
    try {
      return await _takeSinglePicture(controller);
    } finally {
      _activeCaptures--;
      _drainRetired();
    }
  }

  /// Toma UNA foto con [controller] y devuelve los bytes EN MEMORIA.
  ///
  /// El temporal que `takePicture()` deja en el cache se borra de inmediato
  /// (best-effort): nunca se guarda en disco ni galeria. Si el controller ya
  /// fue cerrado se lanza [KycCameraUnavailable] en vez de tocarlo.
  Future<Uint8List> _takeSinglePicture(CameraController controller) async {
    if (_closed.contains(controller)) {
      throw KycCameraUnavailable(
        'La cámara fue liberada antes de completar la captura.',
      );
    }
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
      return await photo.readAsBytes();
    } finally {
      // Best-effort: si falla, el SO purga el cache; se marca como manejado.
      File(photo.path).delete().ignore();
    }
  }

  @override
  Future<void> dispose() async {
    if (_disposed) return;
    _disposed = true;
    final controller = _controller;
    _controller = null;
    _activeLens = null;
    if (controller != null) {
      // Si hay una captura en curso, se difiere el cierre al drenar.
      _retire(controller);
    }
    if (_activeCaptures == 0) {
      for (final retired in List<CameraController>.of(_retired)) {
        await _close(retired);
      }
    }
  }
}
