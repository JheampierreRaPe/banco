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

/// Paso previo a las tareas de liveness (F-T23): captura UNA foto real del
/// documento con la camara trasera.
///
/// Diseño canónico (F-T38): fig `0:354` "Captura DNI" (título
/// `Escanea tu DNI`, tarjeta `Frente del DNI`, botón `Tomar foto`, pie de
/// seguridad) y fig `0:121` "DNI no legible" (borde `error-carmine`, badge
/// `No legible`, `No pudimos leer tu DNI`, consejos y `Volver a tomar`).
/// Solo presentación: la lógica de captura/validación no cambia.
///
/// Cliente delgado (docs/19): la app solo captura y envia; el backend valida
/// el documento. La foto vive solo en memoria ([KycFlowController.documentImage])
/// y viaja en `document.image_b64` del submit. No se guarda en disco.
///
/// La ruta `/kyc/document` se intercala entre `/kyc` y `/kyc/task` sin romper
/// los demas pasos. Con fuente mock (tests/CI) el placeholder permite avanzar.
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

  Future<void> _capture(KycFlowController c) async {
    await c.captureDocument();
    if (!mounted) return;
    if (c.errorMessage != null) return; // permiso/formato: reintentar aqui.
    if (!c.hasDocumentImage) return;
    await _validateAndContinue(c);
  }

  /// Reintenta la validacion SIN recapturar (fallo de red/servicio). La foto
  /// capturada sigue en memoria.
  Future<void> _retryValidation(KycFlowController c) async {
    if (!c.hasDocumentImage) return;
    await _validateAndContinue(c);
  }

  /// Valida el documento en el servidor; solo navega al challenge si el
  /// servidor lo acepta. Si es invalido o hay fallo de red, permanece en la
  /// pantalla con los motivos/error (cliente delgado, docs/19).
  Future<void> _validateAndContinue(KycFlowController c) async {
    final isOk = await c.validateDocument();
    if (!mounted) return;
    if (isOk) {
      await context.push('/kyc/task');
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = _resolve(context);
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: PreferredSize(
        preferredSize: const Size.fromHeight(AppSpacing.stackXl + AppSpacing.stackMd),
        child: KycTopBar(
          title: 'Verifica tu identidad',
          onBack: () => context.pop(),
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
          final issues = c.documentIssues;
          final isInvalid = c.documentValidation?.isValid == false;
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
                  captured ? '1 de 2 capturas' : '0 de 2 capturas',
                  style: AppTypography.labelSm.copyWith(
                    color: AppColors.secondaryText,
                  ),
                ),
                const SizedBox(height: AppSpacing.stackMd),
                _DocumentCaptureCard(
                  isLive: isLive,
                  captured: captured,
                  isInvalid: isInvalid,
                  issues: issues,
                  validating: c.validatingDocument,
                  busy: c.busy,
                  documentBytes: c.documentImage?.length,
                  source: source,
                  onCapture: () => _capture(c),
                  onRecapture: () => c.startDocumentRecapture(),
                ),
                if (c.documentValidationError != null) ...[
                  const SizedBox(height: AppSpacing.stackMd),
                  ErrorView(
                    key: const Key('kycDocumentValidationError'),
                    message: c.documentValidationError!,
                    retryLabel: 'Reintentar validacion',
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
                AppPrimaryButton(
                  label: 'Continuar',
                  onPressed: !captured || c.busy || isInvalid
                      ? null
                      : () => _validateAndContinue(c),
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
    );
  }
}

/// Tarjeta de captura del fig (`0:354` válida, `0:121` no legible).
///
/// La lógica vive en la página; aquí solo cambia el borde a
/// `error-carmine`, el badge `No legible` y los consejos cuando el servidor
/// rechaza el documento. Conserva las claves de regresión
/// (`captureDocumentButton`, `recaptureDocumentButton`, `kycDocumentIssues`).
class _DocumentCaptureCard extends StatelessWidget {
  const _DocumentCaptureCard({
    required this.isLive,
    required this.captured,
    required this.isInvalid,
    required this.issues,
    required this.validating,
    required this.busy,
    required this.documentBytes,
    required this.source,
    required this.onCapture,
    required this.onRecapture,
  });

  final bool isLive;
  final bool captured;
  final bool isInvalid;
  final List<String> issues;
  final bool validating;
  final bool busy;
  final int? documentBytes;
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
        border: isInvalid
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
              if (isInvalid)
                const AppStatusChip.error(label: 'No legible'),
            ],
          ),
          const SizedBox(height: AppSpacing.stackMd),
          _PreviewArea(
            isLive: isLive,
            captured: captured,
            isInvalid: isInvalid,
            source: source,
          ),
          if (captured && !isInvalid) ...[
            const SizedBox(height: AppSpacing.stackSm),
            Row(
              children: [
                const Icon(
                  Icons.check_circle_outline,
                  size: AppSpacing.stackLg,
                  color: AppColors.success,
                ),
                const SizedBox(width: AppSpacing.stackSm),
                Expanded(
                  child: Text(
                    'Documento capturado (${documentBytes ?? 0} '
                    'bytes, solo en memoria).',
                    style: AppTypography.bodyMd.copyWith(
                      color: AppColors.onSurfaceVariant,
                    ),
                  ),
                ),
              ],
            ),
          ],
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
          if (isInvalid) ...[
            const SizedBox(height: AppSpacing.stackMd),
            _DocumentIssuesCard(issues: issues),
          ],
          const SizedBox(height: AppSpacing.stackMd),
          // F-T27: UNA sola accion de captura. Sin foto -> captura
          // normal. Con foto (valida o invalida) -> re-captura, que
          // limpia el estado ([KycFlowController.startDocumentRecapture])
          // y re-monta el preview en vivo (captured vuelve a false).
          // Textos del fig: `Tomar foto` / `Volver a tomar`.
          AppPrimaryButton(
            key: Key(
              captured ? 'recaptureDocumentButton' : 'captureDocumentButton',
            ),
            label: captured ? 'Volver a tomar' : 'Tomar foto',
            icon: Icons.photo_camera_back_outlined,
            loading: busy && !validating,
            onPressed: busy
                ? null
                : (captured ? onRecapture : onCapture),
          ),
        ],
      ),
    );
  }
}

/// Área del viewfinder o del placeholder mock.
class _PreviewArea extends StatelessWidget {
  const _PreviewArea({
    required this.isLive,
    required this.captured,
    required this.isInvalid,
    required this.source,
  });

  final bool isLive;
  final bool captured;
  final bool isInvalid;
  final Object source;

  @override
  Widget build(BuildContext context) {
    final frameColor =
        isInvalid ? AppColors.errorCarmine : AppColors.primaryContainer;
    Widget inner;
    // F-T24: una vez capturado el documento se retira el preview
    // en vivo. Asi, al navegar a `/kyc/task` (lente frontal), la
    // pagina ya no referencia el controller trasero que sera
    // reemplazado, evitando el uso tras dispose.
    // F-T27: "Volver a tomar" limpia la captura previa
    // ([KycFlowController.startDocumentRecapture]) y con ello
    // `captured` vuelve a false, de modo que este bloque
    // re-monta el preview en vivo y el usuario puede tomar una
    // foto nueva.
    if (isLive && !captured) {
      inner = KycCameraPreview(
        key: const ValueKey('document-preview'),
        task: 'document',
        source: source as CameraFrameSource,
      );
    } else {
      inner = const KycPreviewPlaceholder();
    }
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
            const SizedBox(height: AppSpacing.stackSm),
            Text(
              'Evita reflejos y sombras sobre el documento. Apoya el DNI '
              'en una superficie plana y encuadra las cuatro esquinas '
              'dentro del marco.',
              style: AppTypography.labelSm.copyWith(
                color: AppColors.secondaryText,
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
