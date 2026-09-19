import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:go_router/go_router.dart';

import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../camera_frame_source.dart';
import '../kyc_camera_preview.dart';
import '../kyc_dependencies.dart';
import '../kyc_error_handler.dart';
import '../kyc_flow_controller.dart';
import '../kyc_frame_capture.dart';
import 'kyc_capture_overlay.dart';

/// Paso 2 del KYC: captura tarea por tarea, en el orden del servidor.
///
/// - Fuente real ([CameraFrameSource]): muestra el viewfinder en vivo
///   ([KycCameraPreview]) con el MISMO controller de la captura; el botón
///   «Capturar» se habilita solo cuando el preview está listo.
/// - Fuente mock (tests/CI): placeholder histórico sin preview; el botón
///   sigue habilitado de inmediato.
/// No se avanza sin `passed:true`; si falla, se reintenta la MISMA tarea.
class KycTaskPage extends StatefulWidget {
  const KycTaskPage({
    super.key,
    this.controller,
    this.prepDuration = const Duration(seconds: 3),
  });

  /// Controlador inyectable (tests). Por defecto, el compartido de
  /// [KycDependencies].
  final KycFlowController? controller;

  /// Tiempo de "preparacion" por tarea con cámara real: el usuario ve una
  /// cuenta regresiva antes de que «Capturar» se habilite, para alcanzar a
  /// realizar la instruccion (parpadeo/giros) sin que la captura sea
  /// instantánea. Inyectable para tests (`Duration.zero` lo desactiva).
  final Duration prepDuration;

  /// Instruccion amigable por tarea; ante tareas desconocidas del servidor se
  /// muestra el nombre tal cual (el orden siempre lo impone el servidor).
  static String instructionFor(String task) {
    switch (task.toLowerCase()) {
      case 'front':
        return 'Mira de frente a la camara sin moverte.';
      case 'blink':
      case 'parpadeo':
        return 'Parpadea lentamente dos veces.';
      case 'smile':
        return 'Sonrie de forma natural.';
      case 'turn_left':
      case 'izquierda':
        return 'Gira la cabeza lentamente a la izquierda.';
      case 'turn_right':
      case 'derecha':
        return 'Gira la cabeza lentamente a la derecha.';
      case 'arriba':
        return 'Levanta la cabeza y mira hacia arriba.';
      case 'abajo':
        return 'Baja la cabeza y mira hacia abajo.';
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
  /// Preparacion por tarea (contador visible) antes de habilitar Capturar.
  Timer? _prepTimer;
  String? _prepStep;
  int _prepSecondsLeft = 0;

  /// Preview listo POR TAREA. Solo aplica con fuente real; con mock siempre se
  /// considera listo (sin preview).
  ///
  /// La clave es la tarea (no un unico `_readyStep`), de modo que un aviso
  /// atrasado de la tarea anterior no pueda pisar a la vigente.
  final Map<String, bool> _previewReadyByStep = <String, bool>{};

  /// Revision del ultimo aviso recibido por tarea. Gana el de mayor revision:
  /// el `false` diferido de `initState` no puede pisar el `true` que el mismo
  /// preview emite despues cuando el controller ya estaba inicializado.
  final Map<String, int> _readyRevision = <String, int>{};
  int _revision = 0;

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

  /// Actualiza el estado del preview de forma segura ante avisos del hijo.
  ///
  /// El hijo ya no notifica durante build (ver `KycCameraPreview._notifyReady`),
  /// pero por robustez: si el aviso llega con el scheduler no idle se difiere a
  /// post-frame; siempre se verifica [mounted] antes del `setState`.
  ///
  /// Cada aviso lleva una revision (mayor = mas nuevo). Un `false` diferido a
  /// post-frame que corre DESPUES de un `true` inmediato (controller ya
  /// inicializado) queda descartado por tener menor revision; antes, ese
  /// `false` pisaba al `true` y dejaba «Capturar» deshabilitado en el 2.º step.
  void _onPreviewReady(String step, bool ready) {
    if (!mounted) return;
    final revision = ++_revision;
    _readyRevision[step] = revision;
    if (WidgetsBinding.instance.schedulerPhase != SchedulerPhase.idle) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _applyPreviewReady(step, ready, revision);
      });
      return;
    }
    _applyPreviewReady(step, ready, revision);
  }

  void _applyPreviewReady(String step, bool ready, int revision) {
    if (!mounted) return;
    if (_readyRevision[step] != revision) return; // aviso posterior manda.
    if (_previewReadyByStep[step] == ready) return; // sin rebuild innecesario.
    setState(() => _previewReadyByStep[step] = ready);
  }

  /// Arranca la cuenta regresiva de preparacion al cambiar de tarea (solo con
  /// camara real). Idempotente por [step]; no toca el estado durante build mas
  /// que fijar el instante de arranque (el tick que redibuja llega por Timer).
  void _syncPrep(String step, bool isLive) {
    if (!isLive) {
      _prepTimer?.cancel();
      _prepTimer = null;
      _prepStep = null;
      _prepSecondsLeft = 0;
      return;
    }
    if (_prepStep == step) return;
    _prepTimer?.cancel();
    _prepStep = step;
    _prepSecondsLeft = widget.prepDuration.inSeconds;
    if (_prepSecondsLeft <= 0) return;
    _prepTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
      if (!mounted) {
        timer.cancel();
        return;
      }
      setState(() {
        _prepSecondsLeft = (_prepSecondsLeft - 1).clamp(0, 999);
        if (_prepSecondsLeft <= 0) timer.cancel();
      });
    });
  }

  @override
  void dispose() {
    _prepTimer?.cancel();
    super.dispose();
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
          // F-T25: `busy` NO reemplaza la pantalla; el viewfinder sigue vivo y
          // el estado se muestra como overlay (mas abajo) para que el usuario
          // siga moviendose durante la rafaga y la evaluacion.
          final index = c.currentStepIndex;
          final total = c.steps.length;
          final attempts = c.attemptsOf(step);
          final frames = c.framesCountOf(step);
          final source = c.frameSource;
          final isLive = source is CameraFrameSource;
          // Preparacion solo con camara real: da tiempo a ejecutar la
          // instruccion antes de habilitar la captura (no instantanea).
          _syncPrep(step, isLive);
          final prepDone = !isLive || _prepSecondsLeft <= 0;
          // Con mock no hay preview: listo de inmediato. Con cámara real,
          // solo la tarea cuyo preview avisó estar lista habilita Capturar.
          final previewReady = !isLive || (_previewReadyByStep[step] ?? false);
          final captureEnabled = !c.busy && previewReady && prepDone;
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
                // F-T25: el preview (real o placeholder mock) permanece montado
                // durante `busy`; el overlay indica la fase por encima.
                Stack(
                  children: [
                    if (source is CameraFrameSource)
                      KycCameraPreview(
                        key: ValueKey(step),
                        task: step,
                        source: source,
                        onReadyChanged: (ready) => _onPreviewReady(step, ready),
                      )
                    else
                      const KycPreviewPlaceholder(),
                    if (c.busy)
                      Positioned.fill(
                        child: KycCaptureOverlay(phase: c.taskPhase),
                      ),
                  ],
                ),
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
                if (isLive && !prepDone) ...[
                  const SizedBox(height: 16),
                  Row(
                    children: [
                      const Icon(Icons.timer_outlined),
                      const SizedBox(width: 8),
                      Text('Preparate... $_prepSecondsLeft s'),
                    ],
                  ),
                ],
                const SizedBox(height: 24),
                FilledButton.icon(
                  key: const Key('kycCaptureButton'),
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
                  // F-T23: al fallar el paso se indica CUAL y POR QUE (motivo
                  // del servidor traducido) para que el reintento sea
                  // accionable. No se avanza de tarea sin `passed:true`.
                  if ((c.lastError?.serverReason ?? '').isNotEmpty) ...[
                    const SizedBox(height: 8),
                    Text(
                      'Paso fallido: $step',
                      key: const Key('kycFailedStep'),
                      textAlign: TextAlign.center,
                    ),
                    const SizedBox(height: 4),
                    Text(
                      'Motivo: ${kycReasonMessage(c.lastError!.serverReason)}',
                      key: const Key('kycReasonMessage'),
                      textAlign: TextAlign.center,
                    ),
                  ],
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
