import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../camera_frame_source.dart';
import '../kyc_camera_preview.dart';
import '../kyc_dependencies.dart';
import '../kyc_flow_controller.dart';

/// Paso previo a las tareas de liveness (F-T23): captura UNA foto real del
/// documento con la camara trasera.
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
      appBar: AppBar(title: const Text('Captura tu documento')),
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
            padding: const EdgeInsets.all(24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  'Fotografia tu documento',
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 8),
                const Text(
                  'Usa la camara trasera, apoya el documento sobre una '
                  'superficie plana y evita reflejos. La foto solo vive en '
                  'memoria durante la verificacion.',
                ),
                const SizedBox(height: 16),
                // F-T24: una vez capturado el documento se retira el preview
                // en vivo. Asi, al navegar a `/kyc/task` (lente frontal), la
                // pagina ya no referencia el controller trasero que sera
                // reemplazado, evitando el uso tras dispose.
                // F-T27: "Volver a capturar" limpia la captura previa
                // ([KycFlowController.startDocumentRecapture]) y con ello
                // `captured` vuelve a false, de modo que este bloque
                // re-monta el preview en vivo y el usuario puede tomar una
                // foto nueva.
                if (isLive && !captured)
                  KycCameraPreview(
                    key: const ValueKey('document-preview'),
                    task: 'document',
                    source: source,
                  )
                else if (!isLive)
                  const KycPreviewPlaceholder(),
                if (captured)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Row(
                      children: [
                        const Icon(Icons.check_circle_outline),
                        const SizedBox(width: 8),
                        Text('Documento capturado (${c.documentImage!.length} '
                            'bytes, solo en memoria).'),
                      ],
                    ),
                  ),
                if (c.validatingDocument) ...[
                  const SizedBox(height: 12),
                  const Row(
                    key: Key('kycDocumentValidating'),
                    children: [
                      SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      ),
                      SizedBox(width: 12),
                      Text('Validando documento...'),
                    ],
                  ),
                ],
                const SizedBox(height: 24),
                // F-T27: UNA sola accion de captura. Sin foto -> captura
                // normal. Con foto (valida o invalida) -> re-captura, que
                // limpia el estado ([KycFlowController.startDocumentRecapture])
                // y re-monta el preview en vivo (captured vuelve a false).
                // Se elimino el CTA duplicado que capturaba sin resetear el
                // estado y por eso no mostraba la camara.
                FilledButton.icon(
                  key: Key(
                    captured
                        ? 'recaptureDocumentButton'
                        : 'captureDocumentButton',
                  ),
                  onPressed: c.busy
                      ? null
                      : (captured
                          ? () => c.startDocumentRecapture()
                          : () => _capture(c)),
                  icon: const Icon(Icons.photo_camera_back_outlined),
                  label: Text(
                    captured ? 'Volver a capturar' : 'Capturar documento',
                  ),
                ),
                if (isInvalid) ...[
                  const SizedBox(height: 16),
                  _DocumentIssuesCard(issues: issues),
                ],
                if (c.documentValidationError != null) ...[
                  const SizedBox(height: 16),
                  ErrorView(
                    key: const Key('kycDocumentValidationError'),
                    message: c.documentValidationError!,
                    retryLabel: 'Reintentar validacion',
                    onRetry: c.busy ? null : () => _retryValidation(c),
                  ),
                ],
                if (c.errorMessage != null) ...[
                  const SizedBox(height: 16),
                  ErrorView(
                    message: c.errorMessage!,
                    onRetry: () => _capture(c),
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
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Icon(
                  Icons.error_outline,
                  color: Theme.of(context).colorScheme.error,
                ),
                const SizedBox(width: 8),
                const Text('El documento no es valido'),
              ],
            ),
            const SizedBox(height: 8),
            for (final message in messages)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('- '),
                    Expanded(child: Text(message)),
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
