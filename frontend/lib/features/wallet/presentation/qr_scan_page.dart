import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';

/// Seam del lector para tests sin camara (E4-T22).
///
/// El builder recibe:
/// - [onCode]: llamar con el `rawValue` leido (`qr_content`).
/// - [onPermissionDenied]: llamar para simular permiso denegado / sin camara.
///
/// Si es `null`, la pagina usa el [MobileScanner] real (camara del
/// dispositivo). En `flutter test` siempre se inyecta un fake: el lector
/// nativo no renderiza sin hardware.
typedef QrScannerBuilder =
    Widget Function(
      BuildContext context,
      ValueChanged<String> onCode,
      VoidCallback onPermissionDenied,
    );

/// Escaner de QR para pagar (HU15, E4-T13/E4-T22; sin pagina en `pantallas.fig`
/// — fila pendiente `billetera-qr-pago.png`, G76; UI desde tokens
/// `docs/20`).
///
/// - **Camara/lector (E4-T22):** vista previa real con `mobile_scanner`.
///   `onDetect` toma el primer `barcode.rawValue` no vacio y llama
///   `_continueWithCode(qr_content)` -> resumen (`/wallet/pay-qr/confirm`),
///   **una sola vez** por lectura (guardia `_handledScan` + `stop()`).
/// - **Fallback manual:** el campo de texto (`qr-pay-scan-input`) se conserva
///   para entornos sin camara/tests e inyecta el mismo `qr_content`.
/// - **Permiso denegado / sin camara:** aviso en español (`qr-pay-camera-denied`)
///   y el campo de texto sigue usable (degradacion, no bloquea el pago).
/// - Al continuar, navega al resumen (`/wallet/pay-qr/confirm`) con el
///   contenido escaneado (o llama a [onScanned] si se provee, seam para
///   tests sin router).
/// - Cliente delgado: no interpreta el contenido (no valida firma ni
///   decodifica montos); solo lo transporta al resumen/pago.
///
/// Estados (docs/20 §7): contenido (campo + CTA), vacio (sin contenido +
/// error accionable al continuar), error inline en español. La carga
/// vive en el resumen/pago (el escaner no llama a red).
class QrScanPage extends StatefulWidget {
  const QrScanPage({
    super.key,
    this.onScanned,
    this.initialContent,
    this.scannerBuilder,
    this.cameraDenied = false,
  });

  /// Seam para tests: recibe el `qr_content` escaneado en vez de navegar.
  final ValueChanged<String>? onScanned;

  /// Contenido precargado (tests / re-escaneo).
  final String? initialContent;

  /// Seam del lector (E4-T22): si se provee, se usa en vez del
  /// [MobileScanner] real. Obligatorio en `flutter test` (sin hardware).
  final QrScannerBuilder? scannerBuilder;

  /// Fuerza el estado degradado sin camara (tests de permiso denegado).
  final bool cameraDenied;

  @override
  State<QrScanPage> createState() => _QrScanPageState();
}

class _QrScanPageState extends State<QrScanPage> {
  late final TextEditingController _controller;
  MobileScannerController? _scannerController;
  String? _error;
  bool _handledScan = false;
  bool _permissionDenied = false;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController(text: widget.initialContent ?? '');
    _permissionDenied = widget.cameraDenied;
    // El controlador nativo solo existe con el lector real y camara
    // disponible: en tests (scannerBuilder != null o cameraDenied) no se
    // crea para no tocar la plataforma sin hardware.
    if (widget.scannerBuilder == null && !widget.cameraDenied) {
      _scannerController = MobileScannerController(
        detectionSpeed: DetectionSpeed.noDuplicates,
        facing: CameraFacing.back,
      );
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    _scannerController?.dispose();
    super.dispose();
  }

  void _submitContent(String content) {
    final value = content.trim();
    if (value.isEmpty) {
      setState(
        () =>
            _error =
                'Escanea el QR con la cámara o pega el contenido leído para continuar.',
      );
      return;
    }
    final onScanned = widget.onScanned;
    if (onScanned != null) {
      onScanned(value);
      return;
    }
    context.push('/wallet/pay-qr/confirm', extra: value);
  }

  void _continue() => _submitContent(_controller.text);

  /// Mapeo `barcode.rawValue` -> `_continue(qr_content)` (E4-T22 CA-01):
  /// un solo disparo por deteccion.
  void _continueWithCode(String raw) {
    if (_handledScan) return;
    final value = raw.trim();
    if (value.isEmpty) return;
    _handledScan = true;
    _scannerController?.stop();
    _submitContent(value);
  }

  void _onDetect(BarcodeCapture capture) {
    for (final barcode in capture.barcodes) {
      final raw = barcode.rawValue;
      if (raw != null && raw.trim().isNotEmpty) {
        _continueWithCode(raw);
        break;
      }
    }
  }

  void _onPermissionDenied() {
    if (!_permissionDenied && mounted) {
      setState(() => _permissionDenied = true);
    }
  }

  Widget _deniedCopy() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        const Icon(
          Icons.videocam_off_outlined,
          size: 48,
          color: AppColors.primary,
        ),
        const SizedBox(height: 8),
        Text(
          'Sin acceso a la cámara',
          style: AppTypography.titleMd.copyWith(fontSize: 16),
        ),
        const SizedBox(height: 4),
        Text(
          'No pudimos acceder a la cámara. Puedes pegar el contenido '
          'del QR abajo para continuar con tu pago.',
          textAlign: TextAlign.center,
          style: AppTypography.bodyMd.copyWith(
            fontSize: 12,
            color: AppColors.secondaryText,
          ),
        ),
      ],
    );
  }

  Widget _buildScannerArea() {
    final denied = _permissionDenied || widget.cameraDenied;
    if (denied) {
      return Container(
        key: const Key('qr-pay-camera-denied'),
        width: double.infinity,
        padding: const EdgeInsets.all(20),
        decoration: BoxDecoration(
          color: AppColors.surfaceContainerLow,
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: AppColors.outlineVariant),
        ),
        child: _deniedCopy(),
      );
    }
    final scanner =
        widget.scannerBuilder != null
            ? widget.scannerBuilder!(
              context,
              _continueWithCode,
              _onPermissionDenied,
            )
            : MobileScanner(
              controller: _scannerController,
              onDetect: _onDetect,
              errorBuilder: (context, error) {
                if (error.errorCode ==
                        MobileScannerErrorCode.permissionDenied ||
                    error.errorCode == MobileScannerErrorCode.unsupported) {
                  WidgetsBinding.instance.addPostFrameCallback(
                    (_) => _onPermissionDenied(),
                  );
                  return ColoredBox(
                    color: AppColors.surfaceContainerLow,
                    child: Center(child: _deniedCopy()),
                  );
                }
                return const ColoredBox(
                  color: Colors.black,
                  child: Center(
                    child: Icon(
                      Icons.error_outline,
                      color: Colors.white,
                      size: 48,
                    ),
                  ),
                );
              },
            );
    return Container(
      key: const Key('qr-pay-camera-preview'),
      width: double.infinity,
      height: 220,
      decoration: BoxDecoration(
        color: Colors.black,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: AppColors.outlineVariant),
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(16),
        child: scanner,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Pagar con QR')),
      body: SingleChildScrollView(
        key: const Key('qr-pay-scan-scroll'),
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Apunta al QR del comercio. El comercio y el monto los '
              'confirma el servidor antes de mover tu dinero.',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
            ),
            const SizedBox(height: 12),
            // Vista previa del lector real (E4-T22): `MobileScanner` en el
            // dispositivo, fake inyectado en tests via `scannerBuilder`.
            _buildScannerArea(),
            const SizedBox(height: 16),
            AppCard(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Contenido leído (para pruebas sin cámara)',
                    style: AppTypography.labelMd.copyWith(
                      color: AppColors.primary,
                    ),
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    key: const Key('qr-pay-scan-input'),
                    controller: _controller,
                    maxLines: 3,
                    minLines: 1,
                    decoration: InputDecoration(
                      hintText: 'BO-QR1… (lo que leyó el escáner)',
                      filled: true,
                      fillColor: AppColors.surfaceContainerLowest,
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                    ),
                    onChanged: (_) {
                      if (_error != null) setState(() => _error = null);
                    },
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: 8),
                    Text(
                      _error!,
                      key: const Key('qr-pay-scan-error'),
                      style: AppTypography.labelMd.copyWith(
                        fontSize: 13,
                        color: AppColors.onErrorContainer,
                      ),
                    ),
                  ],
                  const SizedBox(height: 12),
                  SizedBox(
                    width: double.infinity,
                    height: 52,
                    child: FilledButton.icon(
                      key: const Key('qr-pay-scan-continue'),
                      onPressed: _continue,
                      icon: const Icon(Icons.arrow_forward, size: 20),
                      label: const Text(
                        'Continuar al resumen',
                        style: TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.w600,
                        ),
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
