import 'package:camera/camera.dart';
import 'package:flutter/material.dart';

import 'camera_frame_source.dart';
import 'kyc_frame_source.dart';

/// Viewfinder en vivo del KYC (ADITIVO, sin segundo controller).
///
/// - Usa el MISMO [CameraController] de [CameraFrameSource] vía
///   [CameraFrameSource.previewControllerForTask] (lente según tarea).
/// - Solo se instancia cuando la fuente es real; con mock la página muestra
///   el placeholder histórico (tests/CI sin cámara siguen verdes).
/// - Estados: inicializando → permiso denegado → no disponible (fallback
///   mock documentado) → listo ([CameraPreview] + marco de guía).
/// - Anti-bloqueo: el `catch (e)` genérico final garantiza que CUALQUIER
///   error (PlatformException, MissingPluginException, timeout convertido a
///   [KycCameraUnavailable] por la fuente, futures que nunca resuelven
///   acotados por [CameraFrameSource.openTimeout]) termine en `denied` o
///   `unavailable`. El spinner infinito es INALCANZABLE por construcción:
///   todo camino sale de `initializing`.
/// - Botón «Reintentar» en los estados `denied`/`unavailable`: re-ejecuta
///   [_initForCurrentTask] (tras otorgar el permiso o conectar cámara).
/// - Permiso: lo pide el SO al llamar `initialize()` dentro de la fuente
///   (ver [CameraFrameSource]); este widget solo mapea el resultado.
/// - Overlay mínimo (solo formas): rectángulo apaisado para documento,
///   óvalo vertical para selfie/liveness.
/// - Nunca guarda fotos en disco; solo muestra el stream en vivo. La captura
///   la sigue haciendo el controlador con `takePicture()` en memoria.
///
/// Prueba en físico (no testeable en CI):
/// 1. `KycDependencies.configure(service: s, frameSource: CameraFrameSource())`.
/// 2. `flutter run` en Android/iOS físico, ir a `/kyc`, completar documento.
/// 3. En `/kyc/task` debe verse el rostro/documento en vivo con su marco;
///    el botón «Capturar» se habilita solo cuando el preview está listo.
/// 4. Negar el permiso de cámara → mensaje claro + reintentar con «Capturar».
/// 5. Tapar/ausencia de cámara → placeholder mock (el submit usa el fallback
///    documentado del controlador).
class KycCameraPreview extends StatefulWidget {
  const KycCameraPreview({
    super.key,
    required this.task,
    required this.source,
    this.onReadyChanged,
  });

  /// Tarea actual (define la lente y el marco: documento vs selfie).
  final String task;

  /// Fuente REAL (el widget no crea controllers propios).
  final CameraFrameSource source;

  /// Avisos de «preview listo» para habilitar el botón Capturar de la página.
  final ValueChanged<bool>? onReadyChanged;

  @override
  State<KycCameraPreview> createState() => _KycCameraPreviewState();
}

enum _PreviewStatus { initializing, denied, unavailable, ready }

class _KycCameraPreviewState extends State<KycCameraPreview> {
  _PreviewStatus _status = _PreviewStatus.initializing;
  CameraController? _controller;
  String _deniedMessage = '';

  @override
  void initState() {
    super.initState();
    _initForCurrentTask();
  }

  @override
  void didUpdateWidget(KycCameraPreview oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.task != widget.task ||
        oldWidget.source != widget.source) {
      _initForCurrentTask();
    }
  }

  Future<void> _initForCurrentTask() async {
    setState(() {
      _status = _PreviewStatus.initializing;
      _controller = null;
    });
    widget.onReadyChanged?.call(false);
    try {
      final controller =
          await widget.source.previewControllerForTask(widget.task);
      if (!mounted) return;
      setState(() {
        _controller = controller;
        _status = _PreviewStatus.ready;
      });
      widget.onReadyChanged?.call(true);
    } on KycCameraPermissionDenied catch (e) {
      if (!mounted) return;
      setState(() {
        _status = _PreviewStatus.denied;
        _deniedMessage = e.userMessage;
      });
      widget.onReadyChanged?.call(false);
    } on KycCameraUnavailable {
      // Fallback documentado: sin cámara se muestra el placeholder mock;
      // el submit usa generateMockFrames (ver KycFlowController).
      if (!mounted) return;
      setState(() => _status = _PreviewStatus.unavailable);
      widget.onReadyChanged?.call(true);
    } catch (_) {
      // Anti-bloqueo: cualquier error no contemplado (PlatformException,
      // MissingPluginException, StateError, ...) cae aquí y muestra el
      // placeholder mock en vez de dejar el spinner infinito.
      if (!mounted) return;
      setState(() => _status = _PreviewStatus.unavailable);
      widget.onReadyChanged?.call(true);
    }
  }

  /// Reintento manual: vuelve a `initializing` y re-ejecuta la apertura de
  /// cámara para la tarea actual.
  void _retry() => _initForCurrentTask();

  @override
  Widget build(BuildContext context) {
    return switch (_status) {
      _PreviewStatus.initializing => const SizedBox(
          height: 240,
          child: Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                CircularProgressIndicator(),
                SizedBox(height: 8),
                Text('Iniciando cámara…'),
              ],
            ),
          ),
        ),
      _PreviewStatus.denied => Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    const Icon(Icons.no_photography_outlined),
                    const SizedBox(width: 8),
                    Expanded(child: Text(_deniedMessage)),
                  ],
                ),
                const SizedBox(height: 12),
                FilledButton.tonal(
                  onPressed: _retry,
                  child: const Text('Reintentar'),
                ),
              ],
            ),
          ),
        ),
      // Cámara no disponible → mismo placeholder que el flujo mock +
      // reintento (p. ej. tras conectar/otorgar cámara o por timeout).
      _PreviewStatus.unavailable => Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const KycPreviewPlaceholder.unavailable(),
            const SizedBox(height: 8),
            FilledButton.tonal(
              onPressed: _retry,
              child: const Text('Reintentar'),
            ),
          ],
        ),
      _PreviewStatus.ready => _LivePreview(
          controller: _controller!,
          documentTask: isDocumentTask(widget.task),
        ),
    };
  }
}

/// Placeholder histórico del flujo mock (sin cámara / tests / CI).
class KycPreviewPlaceholder extends StatelessWidget {
  const KycPreviewPlaceholder({super.key, this.unavailable = false});

  const KycPreviewPlaceholder.unavailable({super.key}) : unavailable = true;

  final bool unavailable;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Consejos para una buena captura:'),
            const SizedBox(height: 8),
            const Text('• Busca buena iluminacion de frente.'),
            const Text('• Muevete lento durante la tarea.'),
            Text(
              unavailable
                  ? '• Cámara no disponible: se usará captura simulada.'
                  : '• Vista previa simulada: sin camara real en esta version.',
            ),
          ],
        ),
      ),
    );
  }
}

/// Preview en vivo + marco de encuadre mínimo (solo formas).
class _LivePreview extends StatelessWidget {
  const _LivePreview({required this.controller, required this.documentTask});

  final CameraController controller;
  final bool documentTask;

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(12),
      child: AspectRatio(
        aspectRatio: 3 / 4,
        child: Stack(
          fit: StackFit.expand,
          children: [
            CameraPreview(controller),
            // Marco de guía: rectángulo apaisado (documento) u óvalo (selfie).
            Center(
              child: documentTask
                  ? Container(
                      width: 260,
                      height: 160,
                      decoration: BoxDecoration(
                        border: Border.all(color: Colors.white, width: 3),
                        borderRadius: BorderRadius.circular(8),
                      ),
                    )
                  : Container(
                      width: 200,
                      height: 260,
                      decoration: BoxDecoration(
                        border: Border.all(color: Colors.white, width: 3),
                        shape: BoxShape.circle,
                      ),
                    ),
            ),
          ],
        ),
      ),
    );
  }
}
