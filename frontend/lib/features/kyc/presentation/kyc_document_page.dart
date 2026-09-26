import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_button.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../camera_frame_source.dart';
import '../kyc_camera_preview.dart';
import '../kyc_dependencies.dart';
import '../kyc_flow_controller.dart';
import 'kyc_fig_widgets.dart';

/// Paso de documento con los 5 estados del `.fig` (F-T45, pagina
/// `crear-cuenta-2documento`): sin captura (`0:1312`), Capturado (`0:1384`),
/// Validando (`0:1419`), No legible (`0:1457`) y Error de red (`0:1498`).
///
/// FAIL-CLOSED: solo se navega a `/kyc/task` cuando el backend responde
/// `is_valid: true`. `Tomar foto` solo captura (estado Capturado); la
/// validacion la dispara `Continuar`. `Continuar` habilitado SOLO en
/// Capturado/Validado. Maximo 2 intentos: al agotarlos sin `is_valid: true`
/// el paso queda BLOQUEADO en No legible, sin avance.
///
/// Cliente delgado (docs/19): la app solo captura y muestra; la decision
/// (`is_valid` + `issues`) es del servidor. La foto vive solo en memoria y
/// nunca se loggea ni se persiste.
class KycDocumentPage extends StatefulWidget {
  const KycDocumentPage({super.key, this.controller});

  /// Controlador inyectable (tests). Por defecto, el compartido de
  /// [KycDependencies].
  final KycFlowController? controller;

  @override
  State<KycDocumentPage> createState() => _KycDocumentPageState();
}

class _KycDocumentPageState extends State<KycDocumentPage> {
  KycFlowController _resolve(BuildContext context) =>
      widget.controller ?? KycDependencies.controller;

  @override
  void dispose() {
    // F-T33: salir de `/kyc/document` (pop o back del sistema) libera la
    // sesión de cámara en el punto único (`KycFlowController.releaseCamera`).
    // El avance a `/kyc/task` es `push` (esta página no se dispone), así que
    // no hay liberación prematura: el cambio de lente lo cubre F-T32.
    final c = widget.controller ?? KycDependencies.controllerIfExists;
    if (c != null) unawaited(c.releaseCamera());
    super.dispose();
  }

  /// F-T45: la captura SOLO deja el estado Capturado. No valida ni navega.
  Future<void> _capture(KycFlowController c) async {
    await c.captureDocument();
  }

  /// Reintenta la validacion SIN recapturar (fallo de red/servicio). La foto
  /// capturada sigue en memoria. Solo navega si el servidor acepta.
  Future<void> _retryValidation(KycFlowController c) async {
    if (!c.hasDocumentImage) return;
    await _validateAndContinue(c);
  }

  /// Valida el documento en el servidor; solo navega al challenge si el
  /// servidor responde `is_valid: true`. Cualquier `false`, error, estado
  /// intermedio o paso bloqueado se queda en la pantalla (fail-closed).
  Future<void> _validateAndContinue(KycFlowController c) async {
    if (c.documentBlocked) return;
    final isOk = await c.validateDocument();
    if (!mounted) return;
    if (isOk && c.documentIsValid) {
      await context.push('/kyc/task');
    }
  }
  @override
  Widget build(BuildContext context) {
    final c = _resolve(context);
    // F-T47: el retroceso VOLUNTARIO (botón de la barra o sistema) pide
    // confirmación porque reinicia TODO el registro. El botón llama
    // directo al flujo de confirmación (`context.pop()` con go_router es
    // declarativo y no consultaría al `PopScope`); el `PopScope` cubre el
    // botón del sistema. El reinicio por fallo final usa `go('/kyc')` y NO
    // pasa por aquí (silencioso, sin popup).
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        unawaited(requestKycBackRestart(context, c));
      },
      child: Scaffold(
        backgroundColor: AppColors.surface,
        appBar: PreferredSize(
          preferredSize:
              const Size.fromHeight(AppSpacing.stackXl + AppSpacing.stackMd),
          child: KycTopBar(
            title: 'Verifica tu identidad',
            onBack: () => unawaited(requestKycBackRestart(context, c)),
          ),
        ),
        body: ListenableBuilder(
        listenable: c,
        builder: (context, _) {
          if (c.challenge == null) {
            return EmptyView(
              message: 'Primero solicita un desafio de verificacion.',
              actionLabel: 'Volver al inicio del KYC',
              onAction: () => context.go('/kyc'),
            );
          }
          // Solo la captura bloquea la pantalla; durante la validacion se
          // conserva el preview/estado y se muestra un indicador inline.
          if (c.busy && !c.validatingDocument) {
            return const LoadingView(message: 'Capturando documento...');
          }
          final source = c.frameSource;
          final isLive = source is CameraFrameSource;
          final captured = c.hasDocumentImage;
          final validating = c.validatingDocument;
          final validation = c.documentValidation;
          final validationError = c.documentValidationError;
          final attempts = c.documentAttempts.clamp(
            0,
            KycFlowController.maxDocumentAttempts,
          );
          final blocked = c.documentBlocked;
          final issues = c.documentIssues;
          // Bloqueado tras 2 fallos: fuerza el estado No legible aunque el
          // ultimo fallo haya sido de red (CA-07).
          final isNoLegible = captured &&
              !validating &&
              (validation?.isValid == false || blocked) &&
              (validationError == null || blocked);
          final isErrorRed =
              captured && !validating && validationError != null && !blocked;
          final isValidated = captured &&
              !validating &&
              validation?.isValid == true &&
              !blocked &&
              validationError == null;
          final isCaptured = captured &&
              !validating &&
              validation == null &&
              validationError == null &&
              !blocked;
          // Gate F-T45: Continuar habilitado SOLO en Capturado/Validado.
          final canContinue =
              (isCaptured || isValidated) && !c.busy && !blocked;
          return SingleChildScrollView(
            padding: const EdgeInsets.all(AppSpacing.stackMd),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const KycProgressStepper(
                  step: 2,
                  total: 4,
                  stageLabel: 'Documento',
                ),
                const SizedBox(height: AppSpacing.stackLg),
                const KycSectionHeader(
                  title: 'Escanea tu DNI',
                  subtitle:
                      'Coloca el documento sobre una superficie plana, sin '
                      'reflejos y con buena luz.',
                ),
                const SizedBox(height: AppSpacing.stackSm),
                Text(
                  '$attempts de 2 capturas',
                  style: AppTypography.labelSm.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
                const SizedBox(height: AppSpacing.stackMd),
                _DocumentCaptureCard(
                  isLive: isLive,
                  captured: captured,
                  isCaptured: isCaptured,
                  isValidated: isValidated,
                  isNoLegible: isNoLegible,
                  isErrorRed: isErrorRed,
                  validating: validating,
                  busy: c.busy,
                  blocked: blocked,
                  issues: issues,
                  source: source,
                  onCapture: () => _capture(c),
                  onRecapture: () => c.startDocumentRecapture(),
                ),
                if (isErrorRed) ...[
                  const SizedBox(height: AppSpacing.stackMd),
                  ErrorView(
                    key: const Key('kycDocumentValidationError'),
                    message:
                        'Sin conexión. Revisa tu internet e inténtalo de nuevo.',
                    retryLabel: 'Reintentar validación',
                    onRetry: c.busy ? null : () => _retryValidation(c),
                  ),
                ],
                if (c.errorMessage != null) ...[
                  const SizedBox(height: AppSpacing.stackMd),
                  ErrorView(
                    message: c.errorMessage!,
                    onRetry: () => _capture(c),
                  ),
                ],
                const SizedBox(height: AppSpacing.stackLg),
                // Sin captura no existe Continuar (fig 0:1312). En el resto
                // de estados se muestra, bloqueado salvo Capturado/Validado.
                if (captured)
                  AppPrimaryButton(
                    key: const Key('kycDocumentContinue'),
                    label: 'Continuar',
                    onPressed:
                        canContinue ? () => _validateAndContinue(c) : null,
                  ),
                const SizedBox(height: AppSpacing.stackLg),
                const KycSecurityFooter(
                  message:
                      'Tus documentos se cifran y solo se usan para validar '
                      'tu identidad.',
                ),
              ],
            ),
          );
        },
        ),
      ),
    );
  }
}

/// Tarjeta de captura con los 5 estados del fig (F-T45).
///
/// - Sin captura (`0:1312`): `Tomar foto`, sin `Continuar` (lo oculta la pagina).
/// - Capturado (`0:1384`): preview `✔ Foto capturada`, sin captura ni bytes.
/// - Validando (`0:1419`): preview `✔` + `Validando documento...`.
/// - No legible (`0:1457`): borde `error-carmine`, badge `No legible`,
///   preview `⚠ Foto con problemas de lectura`, `IssuesCard` y
///   `Volver a tomar` (deshabilitado si el paso quedo BLOQUEADO).
/// - Error de red (`0:1498`): preview `✔`, sin captura ni `Volver a tomar`
///   (el `ErrorBox` + `Reintentar validación` los pinta la pagina).
class _DocumentCaptureCard extends StatelessWidget {
  const _DocumentCaptureCard({
    required this.isLive,
    required this.captured,
    required this.isCaptured,
    required this.isValidated,
    required this.isNoLegible,
    required this.isErrorRed,
    required this.validating,
    required this.busy,
    required this.blocked,
    required this.issues,
    required this.source,
    required this.onCapture,
    required this.onRecapture,
  });

  final bool isLive;
  final bool captured;
  final bool isCaptured;
  final bool isValidated;
  final bool isNoLegible;
  final bool isErrorRed;
  final bool validating;
  final bool busy;
  final bool blocked;
  final List<String> issues;
  final Object source;
  final VoidCallback onCapture;
  final VoidCallback onRecapture;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(AppSpacing.stackMd),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLowest,
        borderRadius: BorderRadius.circular(AppRadii.lg),
        border: isNoLegible
            ? Border.all(color: AppColors.errorCarmine, width: 1.5)
            : null,
        // Sombra canonica `shadow.card` (docs/20 §5): tinte de
        // `primary-container` al 5% via el token [AppShadows.cardList].
        boxShadow: AppShadows.cardList,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Frente del DNI',
                      style: AppTypography.labelMd.copyWith(
                        color: AppColors.primary,
                      ),
                    ),
                    const SizedBox(height: AppSpacing.unit),
                    Text(
                      'Foto y datos personales',
                      style: AppTypography.labelSm.copyWith(
                        color: AppColors.secondaryText,
                      ),
                    ),
                  ],
                ),
              ),
              if (isNoLegible)
                const AppStatusChip.error(label: 'No legible'),
            ],
          ),
          const SizedBox(height: AppSpacing.stackMd),
          _PreviewArea(
            isLive: isLive,
            captured: captured,
            isNoLegible: isNoLegible,
            source: source,
          ),
          if (validating) ...[
            const SizedBox(height: AppSpacing.stackSm),
            const Row(
              key: Key('kycDocumentValidating'),
              children: [
                SizedBox(
                  width: 20,
                  height: 20,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
                SizedBox(width: AppSpacing.stackSm + AppSpacing.unit),
                Text('Validando documento...'),
              ],
            ),
          ],
          if (isNoLegible) ...[
            const SizedBox(height: AppSpacing.stackMd),
            _DocumentIssuesCard(issues: issues),
          ],
          const SizedBox(height: AppSpacing.stackMd),
          // F-T45: acciones por estado. Sin captura -> `Tomar foto`.
          // No legible (no bloqueado) -> `Volver a tomar`. Capturado,
          // Validando y Error de red no muestran accion de captura.
          if (!captured)
            AppPrimaryButton(
              key: const Key('captureDocumentButton'),
              label: 'Tomar foto',
              icon: Icons.photo_camera_back_outlined,
              loading: busy && !validating,
              onPressed: busy ? null : onCapture,
            )
          else if (isNoLegible)
            AppPrimaryButton(
              key: const Key('recaptureDocumentButton'),
              label: 'Volver a tomar',
              icon: Icons.refresh_outlined,
              loading: false,
              onPressed: (busy || blocked) ? null : onRecapture,
            ),
        ],
      ),
    );
  }
}

/// Área del viewfinder según el estado (F-T45).
///
/// - Sin captura: preview en vivo o `Vista previa de la cámara`.
/// - Capturado/Validando/ErrorRed/Validado: `✔ Foto capturada`.
/// - No legible: `⚠ Foto con problemas de lectura` (reemplaza los consejos).
class _PreviewArea extends StatelessWidget {
  const _PreviewArea({
    required this.isLive,
    required this.captured,
    required this.isNoLegible,
    required this.source,
  });

  final bool isLive;
  final bool captured;
  final bool isNoLegible;
  final Object source;

  @override
  Widget build(BuildContext context) {
    final frameColor =
        isNoLegible ? AppColors.errorCarmine : AppColors.primaryContainer;
    Widget inner;
    if (isNoLegible) {
      inner = Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(
            Icons.warning_amber_outlined,
            size: AppSpacing.stackLg,
            color: AppColors.errorCarmine,
          ),
          const SizedBox(width: AppSpacing.stackSm),
          Flexible(
            child: Text(
              'Foto con problemas de lectura',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.errorCarmine,
              ),
            ),
          ),
        ],
      );
    } else if (captured) {
      inner = Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(
            Icons.check_circle_outline,
            size: AppSpacing.stackLg,
            color: AppColors.success,
          ),
          const SizedBox(width: AppSpacing.stackSm),
          Flexible(
            child: Text(
              'Foto capturada',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.onSurfaceVariant,
              ),
            ),
          ),
        ],
      );
    } else if (isLive) {
      // F-T24: una vez capturado se retira el preview en vivo; con
      // `startDocumentRecapture` (`captured=false`) se re-monta.
      inner = KycCameraPreview(
        key: const ValueKey('document-preview'),
        task: 'document',
        source: source as CameraFrameSource,
      );
    } else {
      inner = Text(
        'Vista previa de la cámara',
        textAlign: TextAlign.center,
        style: AppTypography.bodyMd.copyWith(
          color: AppColors.secondaryText,
        ),
      );
    }
    // El preview en vivo necesita su altura natural; los estados de texto
    // se centran con padding amplio.
    if (inner is KycCameraPreview) {
      return Container(
        decoration: BoxDecoration(
          color: AppColors.surface,
          borderRadius: BorderRadius.circular(AppRadii.def),
          border: Border.all(color: frameColor.withValues(alpha: 0.4)),
        ),
        padding: const EdgeInsets.all(AppSpacing.stackSm),
        child: inner,
      );
    }
    return Container(
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(AppRadii.def),
        border: Border.all(color: frameColor.withValues(alpha: 0.4)),
      ),
      padding: const EdgeInsets.symmetric(
        horizontal: AppSpacing.stackSm,
        vertical: AppSpacing.stackLg,
      ),
      child: Center(child: inner),
    );
  }
}

/// Muestra los motivos de rechazo del documento (servidor) en espanol.
///
/// El cliente NO decide: solo presenta lo que responde el servidor. Los
/// codigos conocidos se traducen con [documentIssueMessage]; cualquier motivo
/// desconocido se muestra tal cual (sin suavizar, docs/19).
class _DocumentIssuesCard extends StatelessWidget {
  const _DocumentIssuesCard({required this.issues});

  final List<String> issues;

  @override
  Widget build(BuildContext context) {
    final messages = issues.isEmpty
        ? const ['El documento no paso la validacion. Vuelve a capturarlo.']
        : [for (final issue in issues) documentIssueMessage(issue)];
    return Card(
      key: const Key('kycDocumentIssues'),
      color: AppColors.errorContainer.withValues(alpha: 0.35),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadii.md - AppSpacing.unit),
      ),
      child: Padding(
        padding: const EdgeInsets.all(AppSpacing.stackMd),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                const Icon(
                  Icons.error_outline,
                  color: AppColors.errorCarmine,
                ),
                const SizedBox(width: AppSpacing.stackSm),
                Expanded(
                  child: Text(
                    'No pudimos leer tu DNI',
                    style: AppTypography.labelMd.copyWith(
                      color: AppColors.errorCarmine,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: AppSpacing.stackSm),
            for (final message in messages)
              Padding(
                padding: const EdgeInsets.only(top: AppSpacing.unit),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '• ',
                      style: AppTypography.bodyMd.copyWith(
                        color: AppColors.onSurfaceVariant,
                      ),
                    ),
                    Expanded(
                      child: Text(
                        message,
                        style: AppTypography.bodyMd.copyWith(
                          color: AppColors.onSurfaceVariant,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// Traduce un `issue` de la validacion de documento a un mensaje en espanol.
///
/// Solo es presentacion: el fallo lo determina el servidor. Si el motivo no se
/// reconoce se devuelve el texto original sin modificarlo.
String documentIssueMessage(String issue) {
  final raw = issue.trim();
  if (raw.isEmpty) {
    return 'El documento no paso la validacion. Vuelve a capturarlo.';
  }
  final code = raw.toUpperCase();
  bool has(List<String> keys) => keys.any(code.contains);
  if (has(['VALIDATOR_UNAVAILABLE'])) {
    return 'No pudimos validar tu documento. Vuelve a intentarlo.';
  }
  if (has(['BLUR', 'BORROS', 'SHARP', 'FOCUS'])) {
    return 'La imagen esta borrosa. Apoya el documento y evita mover la camara.';
  }
  if (has(['DARK', 'OSCUR', 'LIGHTING', 'LIGHT', 'GLARE', 'REFLEJ', 'BRIGHT'])) {
    return 'La iluminacion no es adecuada. Busca luz uniforme y evita reflejos.';
  }
  if (has(['CROP', 'INCOMPLETE', 'PARTIAL', 'CUT'])) {
    return 'El documento no se ve completo. Incluye sus cuatro esquinas.';
  }
  if (has(['NO_DOCUMENT', 'NOT_FOUND', 'EMPTY'])) {
    return 'No detectamos un documento en la foto. Vuelve a capturarla.';
  }
  if (has(['FORMAT', 'UNSUPPORTED', 'MIME', 'SIZE', 'SMALL', 'LARGE'])) {
    return 'El formato o tamano de la imagen no es valido. Intenta de nuevo.';
  }
  return raw;
}
