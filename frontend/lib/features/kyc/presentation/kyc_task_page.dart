import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../camera_frame_source.dart';
import '../kyc_camera_preview.dart';import '../kyc_dependencies.dart';
import '../kyc_flow_controller.dart';
import '../kyc_frame_capture.dart';

/// Paso 2 del KYC: captura tarea por tarea, en el orden del servidor.
///
/// - Fuente real ([CameraFrameSource]): muestra el viewfinder en vivo
///   ([KycCameraPreview]) con el MISMO controller de la captura; el botón
///   «Capturar» se habilita solo cuando el preview está listo.
/// - Fuente mock (tests/CI): placeholder histórico sin preview; el botón
///   sigue habilitado de inmediato.
/// No se avanza sin `passed:true`; si falla, se reintenta la MISMA tarea.
class KycTaskPage extends StatefulWidget {
  const KycTaskPage({super.key, this.controller});

  /// Controlador inyectable (tests). Por defecto, el compartido de
  /// [KycDependencies].
  final KycFlowController? controller;

  /// Instruccion amigable por tarea; ante tareas desconocidas del servidor se
  /// muestra el nombre tal cual (el orden siempre lo impone el servidor).
  static String instructionFor(String task) {
    switch (task.toLowerCase()) {
      case 'front':
        return 'Mira de frente a la camara sin moverte.';
      case 'blink':
        return 'Parpadea lentamente dos veces.';
      case 'smile':
        return 'Sonrie de forma natural.';
      case 'turn_left':
        return 'Gira la cabeza lentamente a la izquierda.';
      case 'turn_right':
        return 'Gira la cabeza lentamente a la derecha.';
      case 'nod':
        return 'Asiente lentamente con la cabeza.';
      default:
        return 'Sigue la instruccion en pantalla: $task.';
    }
  }

  @override
  State<KycTaskPage> createState() => _KycTaskPageState();
}

class _KycTaskPageState extends State<KycTaskPage> {
  /// Preview listo para la tarea de [_readyStep]. Solo aplica con fuente
  /// real; con mock siempre se considera listo (sin preview).
  bool _previewReady = false;
  String? _readyStep;

  KycFlowController _resolve(BuildContext context) =>
      widget.controller ?? KycDependencies.controller;

  Future<void> _capture(KycFlowController c) =>
      c.captureAndResolveCurrentTask();

  Future<void> _submit(BuildContext context, KycFlowController c) async {
    await c.submit();
    if (!context.mounted) return;
    if (c.errorMessage != null) return; // fallo de red: conservar estado.
    if (c.result != null) {
      await context.push('/kyc/result');
    }
  }

  void _onPreviewReady(String step, bool ready) {
    if (!mounted) return;
    setState(() {
      _previewReady = ready;
      _readyStep = step;
    });
  }

  @override
  Widget build(BuildContext context) {
    final c = _resolve(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Prueba de vida')),
      body: ListenableBuilder(
        listenable: c,
        builder: (context, _) {
          final step = c.currentStep;
          if (step == null) {
            return EmptyView(
              message: 'Primero solicita un desafio de verificacion.',
              actionLabel: 'Volver al inicio del KYC',
              onAction: () => context.go('/kyc'),
            );
          }
          if (c.busy) {
            return const LoadingView(message: 'Procesando...');
          }
          final index = c.currentStepIndex;
          final total = c.steps.length;
          final attempts = c.attemptsOf(step);
          final frames = c.framesCountOf(step);
          final source = c.frameSource;
          final isLive = source is CameraFrameSource;
          // Con mock no hay preview: listo de inmediato. Con cámara real,
          // solo la tarea cuyo preview avisó estar lista habilita Capturar.
          final previewReady =
              !isLive || (_previewReady && _readyStep == step);
          final captureEnabled = !c.busy && previewReady;
          return SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  'Paso ${index + 1} de $total: $step',
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 8),
                LinearProgressIndicator(value: (index + 1) / total),
                const SizedBox(height: 16),
                Text(
                  KycTaskPage.instructionFor(step),
                  style: Theme.of(context).textTheme.bodyLarge,
                ),
                const SizedBox(height: 16),
                if (source is CameraFrameSource)
                  KycCameraPreview(
                    key: ValueKey(step),
                    task: step,
                    source: source,
                    onReadyChanged: (ready) => _onPreviewReady(step, ready),
                  )
                else
                  const KycPreviewPlaceholder(),
                const SizedBox(height: 16),
                Row(
                  children: [
                    const Icon(Icons.videocam_outlined),
                    const SizedBox(width: 8),
                    Text(
                      frames == 0
                          ? 'Sin capturas aun (0/$kMockFramesPerTask frames).'
                          : 'Capturados $frames frames (solo en memoria).',
                    ),
                  ],
                ),
                if (attempts > 0)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text(
                      c.taskPassed(step)
                          ? 'Tarea superada.'
                          : 'Intentos en esta tarea: $attempts.',
                    ),
                  ),
                const SizedBox(height: 24),
                FilledButton.icon(
                  onPressed: captureEnabled ? () => _capture(c) : null,
                  icon: const Icon(Icons.camera_alt_outlined),
                  label: Text(
                    attempts == 0 ? 'Capturar' : 'Reintentar captura',
                  ),
                ),
                if (c.errorMessage != null) ...[
                  const SizedBox(height: 16),
                  ErrorView(
                    message: c.errorMessage!,
                    onRetry: captureEnabled ? () => _capture(c) : null,
                  ),
                ],
                if (c.readyToSubmit) ...[
                  const SizedBox(height: 16),
                  FilledButton.tonal(
                    onPressed: () => _submit(context, c),
                    child: const Text('Enviar verificacion'),
                  ),
                ],
              ],
            ),
          );
        },
      ),
    );
  }
}
