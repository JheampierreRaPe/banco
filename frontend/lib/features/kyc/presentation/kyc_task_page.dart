import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_button.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../camera_frame_source.dart';
import '../kyc_camera_preview.dart';
import '../kyc_dependencies.dart';
import '../kyc_error_handler.dart';
import '../kyc_flow_controller.dart';
import '../kyc_frame_capture.dart';
import 'kyc_capture_overlay.dart';
import 'kyc_fig_widgets.dart';

/// Paso 2 del KYC: captura tarea por tarea, en el orden del servidor.
///
/// Diseño canónico (F-T38): fig `0:198` "Reconocimiento Facial" adaptado a
/// **modo claro** (docs/20 manda solo claro; el fig es dark y NO se
/// implementa tema oscuro): título `Centra tu rostro en el círculo`,
/// stepper `Paso 3 de 4 · Rostro`, instrucción destacada, viewport circular,
/// checklist (`Buena iluminación`, `Rostro descubierto`, `Prueba de vida`) y
/// `No cierres la app durante la verificación`. El error sigue el fig
/// `0:269` (`No pudimos verificarte`, motivo accionable, `Intento N de 3`,
/// reintento de la misma tarea). Solo presentación: la lógica de captura
/// (ráfaga, close-before-open, teardown) no cambia.
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
    // F-T33: salir de `/kyc/task` (pop o back del sistema) libera la sesión
    // de cámara en el punto único (`KycFlowController.releaseCamera`). No
    // borra el estado del flujo ni crea un controlador nuevo al salir.
    final c = widget.controller ?? KycDependencies.controllerIfExists;
    if (c != null) unawaited(c.releaseCamera());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final c = _resolve(context);
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: PreferredSize(
        preferredSize: const Size.fromHeight(AppSpacing.stackXl + AppSpacing.stackMd),
        child: KycTopBar(
          title: 'Reconocimiento facial',
          onBack: () => context.pop(),
        ),
      ),
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
          final hasError = c.errorMessage != null;
          return SingleChildScrollView(
            padding: const EdgeInsets.all(AppSpacing.stackMd),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                KycProgressStepper(
                  step: index + 1,
                  total: total,
                  stageLabel: 'Rostro',
                ),
                const SizedBox(height: AppSpacing.stackLg),
                Text(
                  'Centra tu rostro en el círculo',
                  textAlign: TextAlign.center,
                  style: AppTypography.titleMd.copyWith(
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(height: AppSpacing.stackMd),
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: AppSpacing.stackMd,
                    vertical: AppSpacing.stackSm + AppSpacing.unit,
                  ),
                  decoration: BoxDecoration(
                    color: AppColors.secondaryContainer.withValues(alpha: 0.35),
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                  child: Text(
                    KycTaskPage.instructionFor(step),
                    textAlign: TextAlign.center,
                    style: AppTypography.bodyLg.copyWith(
                      color: AppColors.accentText,
                    ),
                  ),
                ),
                const SizedBox(height: AppSpacing.stackLg),
                // F-T25: el preview (real o placeholder mock) permanece montado
                // durante `busy`; el overlay indica la fase por encima.
                // Área clara con anillo ocre (fig `0:198` sin tema oscuro).
                // Con cámara real el preview en vivo conserva su altura
                // natural (sus estados inicializando/denegado/no disponible
                // y su guía circular propia no se recortan); sin cámara
                // (mock/tests) se muestra un avatar compacto en círculo y
                // debajo el placeholder histórico con los consejos
                // (regresión F-T25).
                Container(
                  decoration: BoxDecoration(
                    color: AppColors.surfaceContainerLow,
                    borderRadius: BorderRadius.circular(AppRadii.md),
                    border: Border.all(
                      color: hasError
                          ? AppColors.errorCarmine
                          : AppColors.secondaryContainer,
                      width: 2,
                    ),
                  ),
                  clipBehavior: Clip.antiAlias,
                  child: Stack(
                    children: [
                      if (source is CameraFrameSource)
                        KycCameraPreview(
                          key: ValueKey(step),
                          task: step,
                          source: source,
                          onReadyChanged: (ready) =>
                              _onPreviewReady(step, ready),
                        )
                      else
                        Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            const SizedBox(
                              height: AppSpacing.stackSm + AppSpacing.unit,
                            ),
                            KycFaceViewportFrame(
                              diameter: 148,
                              hasError: hasError,
                              child: const _MockFaceAvatar(),
                            ),
                            const KycPreviewPlaceholder(),
                            const SizedBox(
                              height: AppSpacing.stackSm + AppSpacing.unit,
                            ),
                          ],
                        ),
                      if (c.busy)
                        Positioned.fill(
                          child: KycCaptureOverlay(phase: c.taskPhase),
                        ),
                    ],
                  ),
                ),
                const SizedBox(height: AppSpacing.stackMd),
                _LivenessChecklist(passed: c.taskPassed(step)),
                const SizedBox(height: AppSpacing.stackMd),
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    const Icon(
                      Icons.videocam_outlined,
                      size: AppSpacing.stackLg,
                      color: AppColors.secondaryText,
                    ),
                    const SizedBox(width: AppSpacing.stackSm),
                    Flexible(
                      child: Text(
                        frames == 0
                            ? 'Sin capturas aun (0/$kMockFramesPerTask frames).'
                            : 'Capturados $frames frames (solo en memoria).',
                        style: AppTypography.bodyMd.copyWith(
                          color: AppColors.secondaryText,
                        ),
                      ),
                    ),
                  ],
                ),
                if (attempts > 0)
                  Padding(
                    padding: const EdgeInsets.only(top: AppSpacing.stackSm),
                    child: Text(
                      c.taskPassed(step)
                          ? 'Tarea superada.'
                          : 'Intentos en esta tarea: $attempts.',
                      textAlign: TextAlign.center,
                      style: AppTypography.bodyMd.copyWith(
                        color: AppColors.onSurfaceVariant,
                      ),
                    ),
                  ),
                if (isLive && !prepDone) ...[
                  const SizedBox(height: AppSpacing.stackMd),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      const Icon(
                        Icons.timer_outlined,
                        size: AppSpacing.stackLg,
                        color: AppColors.secondaryText,
                      ),
                      const SizedBox(width: AppSpacing.stackSm),
                      Text(
                        'Preparate... $_prepSecondsLeft s',
                        style: AppTypography.bodyMd.copyWith(
                          color: AppColors.secondaryText,
                        ),
                      ),
                    ],
                  ),
                ],
                const SizedBox(height: AppSpacing.stackLg),
                AppPrimaryButton(
                  key: const Key('kycCaptureButton'),
                  label: attempts == 0 ? 'Capturar' : 'Reintentar captura',
                  icon: Icons.camera_alt_outlined,
                  loading: c.busy,
                  onPressed: captureEnabled ? () => _capture(c) : null,
                ),
                if (hasError) ...[
                  const SizedBox(height: AppSpacing.stackMd),
                  _FacialErrorCard(
                    controller: c,
                    step: step,
                    onRetry: captureEnabled ? () => _capture(c) : null,
                  ),
                ],
                if (c.readyToSubmit) ...[
                  const SizedBox(height: AppSpacing.stackMd),
                  AppPrimaryButton(
                    label: 'Enviar verificacion',
                    onPressed: () => _submit(context, c),
                  ),
                ],
                const SizedBox(height: AppSpacing.stackLg),
                Text(
                  'No cierres la app durante la verificación.',
                  textAlign: TextAlign.center,
                  style: AppTypography.labelSm.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
              ],
            ),
          );
        },
      ),
    );
  }
}

/// Avatar compacto del viewport circular sin cámara (mock/tests/CI).
///
/// Presentación pura: ocupa el círculo claro del fig sin desbordarlo (el
/// placeholder histórico con los consejos va debajo, fuera del círculo).
class _MockFaceAvatar extends StatelessWidget {
  const _MockFaceAvatar();

  @override
  Widget build(BuildContext context) {
    return const Center(
      child: Icon(
        Icons.person_outline,
        size: 96,
        color: AppColors.secondaryText,
      ),
    );
  }
}

/// Checklist de la prueba de vida (fig `0:198`, en claro).
///
/// Presentación pura: los checks de iluminación/rostro son guía estática del
/// fig; el estado real de la tarea lo da el servidor (cliente delgado).
class _LivenessChecklist extends StatelessWidget {
  const _LivenessChecklist({required this.passed});

  final bool passed;

  @override
  Widget build(BuildContext context) {
    return AppCard(
      child: Column(
        children: [
          _checkRow(
            done: true,
            label: 'Buena iluminación',
            doneColor: AppColors.success,
          ),
          const SizedBox(height: AppSpacing.stackMd),
          _checkRow(
            done: true,
            label: 'Rostro descubierto',
            doneColor: AppColors.success,
          ),
          const SizedBox(height: AppSpacing.stackMd),
          _checkRow(
            done: passed,
            label: passed ? 'Prueba de vida (Superada)' : 'Prueba de vida (En proceso)',
            doneColor: AppColors.success,
          ),
        ],
      ),
    );
  }

  Widget _checkRow({
    required bool done,
    required String label,
    required Color doneColor,
  }) {
    return Row(
      children: [
        Container(
          width: AppSpacing.stackLg,
          height: AppSpacing.stackLg,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: done
                ? doneColor.withValues(alpha: 0.15)
                : AppColors.surfaceContainerHigh,
          ),
          child: Icon(
            done ? Icons.check : Icons.hourglass_empty,
            size: AppSpacing.stackMd,
            color: done ? doneColor : AppColors.secondaryText,
          ),
        ),
        const SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
        Expanded(
          child: Text(
            label,
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSurface,
            ),
          ),
        ),
      ],
    );
  }
}

/// Error facial accionable (fig `0:269`, en claro).
///
/// Conserva las claves de regresión (`kycFailedStep`, `kycReasonMessage`) y
/// el [ErrorView] con reintento de la misma tarea. Sin lógica de negocio:
/// solo presenta el paso fallido, el motivo traducido del servidor y el
/// contador de intentos.
class _FacialErrorCard extends StatelessWidget {
  const _FacialErrorCard({
    required this.controller,
    required this.step,
    required this.onRetry,
  });

  final KycFlowController controller;
  final String step;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    final c = controller;
    return Container(
      padding: const EdgeInsets.all(AppSpacing.stackMd),
      decoration: BoxDecoration(
        color: AppColors.errorContainer.withValues(alpha: 0.35),
        borderRadius: BorderRadius.circular(AppRadii.md),
        border: Border.all(color: AppColors.errorCarmine.withValues(alpha: 0.5)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const Icon(
                Icons.error_outline,
                size: AppSpacing.stackLg,
                color: AppColors.errorCarmine,
              ),
              const SizedBox(width: AppSpacing.stackSm),
              Flexible(
                child: Text(
                  'No pudimos verificarte',
                  style: AppTypography.labelMd.copyWith(
                    color: AppColors.errorCarmine,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Asegúrate de estar en un lugar bien iluminado y de seguir el '
            'movimiento que te indicamos.',
            textAlign: TextAlign.center,
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSurfaceVariant,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          ErrorView(
            message: c.errorMessage!,
            onRetry: onRetry,
          ),
          // F-T23: al fallar el paso se indica CUAL y POR QUE (motivo
          // del servidor traducido) para que el reintento sea
          // accionable. No se avanza de tarea sin `passed:true`.
          if ((c.lastError?.serverReason ?? '').isNotEmpty) ...[
            const SizedBox(height: AppSpacing.stackSm),
            Text(
              'Paso fallido: $step',
              key: const Key('kycFailedStep'),
              textAlign: TextAlign.center,
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.onSurface,
              ),
            ),
            const SizedBox(height: AppSpacing.unit),
            Text(
              'Motivo: ${kycReasonMessage(c.lastError!.serverReason)}',
              key: const Key('kycReasonMessage'),
              textAlign: TextAlign.center,
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.onSurfaceVariant,
              ),
            ),
          ],
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Intento ${c.attemptsOf(step)} de ${c.maxAttemptsPerTask}',
            textAlign: TextAlign.center,
            style: AppTypography.labelSm.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
        ],
      ),
    );
  }
}
